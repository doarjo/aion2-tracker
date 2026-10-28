"""Goal profiles and enhancement headroom.

The engine ranks by a weighting heuristic, deliberately. PLAYNC publishes
combatPower only for your current state and never explains the formula, so a
calculated CP delta would be a fabrication. These tests pin down that the
ranking responds to the chosen goal, and that enhancement figures are the
exact ones PLAYNC returned rather than anything we invented.
"""

from __future__ import annotations

import pytest

from aion2.models import Character, Item
from aion2.recommend import Engine, UnknownProfile, load_config


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# --------------------------------------------------------------------------
# profile resolution
# --------------------------------------------------------------------------


def test_default_profile_is_used_when_none_given(cfg):
    engine = Engine(cfg)
    assert engine.profile == cfg["default_profile"]
    assert engine.has_config


def test_explicit_profile_overrides_default(cfg):
    engine = Engine(cfg, profile="pve_dps")
    assert engine.profile == "pve_dps"
    assert engine.profile_label == "PvE Damage"


def test_unknown_profile_raises_and_lists_options(cfg):
    with pytest.raises(UnknownProfile) as exc:
        Engine(cfg, profile="nonsense")
    message = str(exc.value)
    assert "nonsense" in message
    for name in cfg["profiles"]:
        assert name in message


def test_profiles_weight_differently(cfg):
    """The whole point of profiles: the same stat is worth different amounts."""
    cp = Engine(cfg, profile="combat_power")
    dps = Engine(cfg, profile="pve_dps")

    # Defence counts toward Combat Power but is worthless for pure damage.
    assert cp.weight("ArmorDefense") > 0
    assert dps.weight("ArmorDefense") == 0
    assert cp.weight("HPMax") > 0
    assert dps.weight("HPMax") == 0

    # Accuracy matters far more when chasing the accuracy floor.
    rush = Engine(cfg, profile="accuracy_rush")
    assert rush.weight("WeaponAccuracy") > dps.weight("WeaponAccuracy")
    assert dps.weight("WeaponAccuracy") > cp.weight("WeaponAccuracy")


def test_available_profiles_lists_labels(cfg):
    profiles = Engine.available_profiles(cfg)
    assert profiles["combat_power"] == "Combat Power"
    assert "pve_dps" in profiles


def test_wasted_stats_are_wasted_regardless_of_profile(cfg):
    """A manastone slot is a fixed budget, so filling it with Block instead of
    Attack is a loss whichever goal you pick."""
    for name in cfg["profiles"]:
        engine = Engine(cfg, profile=name)
        assert engine.is_wasted("Block")
        assert engine.is_wasted("Evasion")
        assert not engine.is_wasted("Critical")


# --------------------------------------------------------------------------
# accuracy escalation is profile-aware
# --------------------------------------------------------------------------


def _bare_character(**kw) -> Character:
    return Character(
        name="T", class_name="Ranger", level=45, race="Elyos",
        server_name="S", region="eu", server_id=1302,
        combat_power=1, item_level=1, **kw,
    )


def test_accuracy_is_only_urgent_when_the_goal_cares(cfg):
    """Under a Combat Power goal a low gear-accuracy reading is context, not
    a call to action. It used to always report Critical."""
    char = _bare_character()
    assert char.gear_accuracy == 0

    cp = Engine(cfg, profile="combat_power").accuracy_summary(char)
    rush = Engine(cfg, profile="accuracy_rush").accuracy_summary(char)

    assert int(rush.priority) > int(cp.priority)


def test_accuracy_summary_never_claims_an_in_game_total(cfg):
    char = _bare_character()
    rec = Engine(cfg).accuracy_summary(char)
    assert "not your in-game total" in rec.detail.lower()


# --------------------------------------------------------------------------
# enhancement headroom
# --------------------------------------------------------------------------


def _item(slot: int, name: str, level: int, cap: int) -> Item:
    return Item(
        slot_pos=slot, slot_name=name, item_id=1, name=name,
        grade="Unique", enchant_level=level, max_enchant_level=cap,
    )


def test_no_enhancement_recommendations_without_scan_data(cfg):
    char = _bare_character(items=[_item(23, "Rune1", 2, 10)])
    assert not char.has_enhancement_data
    assert Engine(cfg).enhancement_upgrades(char) == []
    assert Engine(cfg).enhancement_summary(char) is None


def test_enhancement_uses_stored_exact_values(cfg):
    char = _bare_character(
        items=[_item(23, "Rune1", 2, 10)],
        enhancement_headroom={"23": {"DefensePierce": 400.0}},
    )
    recs = Engine(cfg).enhancement_upgrades(char)
    assert len(recs) == 1
    assert recs[0].gains == {"DefensePierce": 400.0}
    assert "+2 to +10" in recs[0].title
    assert "not an estimate" in recs[0].detail


def test_paired_items_are_distinguishable(cfg):
    """Two Clash Runes previously produced two identical titles."""
    char = _bare_character(
        items=[_item(23, "Rune1", 2, 10), _item(24, "Rune2", 2, 10)],
        enhancement_headroom={
            "23": {"DefensePierce": 400.0},
            "24": {"DefensePierce": 400.0},
        },
    )
    titles = [r.title for r in Engine(cfg).enhancement_upgrades(char)]
    assert len(titles) == 2
    assert len(set(titles)) == 2, "paired items must be distinguishable"


def test_levels_available_counts_every_item(cfg):
    char = _bare_character(
        items=[_item(23, "Rune1", 2, 10), _item(17, "Belt", 0, 10)]
    )
    assert char.enhancement_levels_available == 18


def test_total_enhancement_gain_sums_across_slots(cfg):
    char = _bare_character(
        enhancement_headroom={
            "23": {"DefensePierce": 400.0, "CombatSpeed": 4.0},
            "24": {"DefensePierce": 400.0},
        },
    )
    totals = char.total_enhancement_gain()
    assert totals["DefensePierce"] == 800.0
    assert totals["CombatSpeed"] == 4.0


def test_enhancement_outranks_a_small_manastone_swap(cfg):
    """A 400-point Penetration gain must sort above a 7-point stone swap."""
    engine = Engine(cfg, profile="combat_power")
    big = engine.score_gains({"DefensePierce": 400.0})
    small = engine.score_gains({"Critical": 7.0})
    assert big > small * 10
