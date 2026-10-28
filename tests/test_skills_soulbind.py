"""Skill training sweep and soul binding.

Both exist because the first pass missed them:

* skill_targets only checked six named skills, all of which passed, so the
  engine said nothing while plenty of training remained.
* soulBindRate was recorded as "always 100" after looking at two sample
  characters who happened to be fully bound. Our own Ranger's bow is 13.3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aion2.models import (
    SKILL_TRAINED_CAP,
    STIGMA_TRAINED_CAP,
    Character,
    Item,
    Skill,
)
from aion2.recommend import Engine, Priority, load_config
from aion2.replay import load_capture

CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "epitet"

needs_capture = pytest.mark.skipif(
    not CAPTURE.is_dir(),
    reason="run scripts/discover.ps1 to create captures/epitet",
)


@pytest.fixture(scope="module")
def engine():
    return Engine(load_config())


def _char(**kw) -> Character:
    return Character(
        name="T", class_name="Ranger", level=45, race="Elyos",
        server_name="S", region="eu", server_id=1302,
        combat_power=1, item_level=1, **kw,
    )


# --------------------------------------------------------------------------
# skill in-use semantics
# --------------------------------------------------------------------------


def test_passives_count_as_in_use_without_being_equipped():
    """Every passive reports equip=0, so requiring equip would silently skip
    all ten of them."""
    passive = Skill("Rooting Eye", "Passive", 18, acquired=True, equipped=False)
    assert passive.in_use


def test_unequipped_active_is_not_in_use():
    active = Skill("Defiance", "Active", 1, acquired=True, equipped=False)
    assert not active.in_use


def test_unacquired_skill_is_never_in_use():
    for category in ("Active", "Passive", "Dp"):
        s = Skill("X", category, 0, acquired=False, equipped=True)
        assert not s.in_use


def test_displayed_level_already_contains_the_grants():
    """THE bug this model replaced.

    The API's skillLevel is what the game shows and already includes board
    and gear grants. Treating it as a base and adding the bonuses reported
    Deadshot as 30 when the game shows 20.
    """
    deadshot = Skill(
        "Deadshot", "Active", 20, daevanion_bonus=4, gear_bonus=6,
        acquired=True, equipped=True,
    )
    assert deadshot.displayed_level == 20, "what the game shows"
    assert deadshot.trained_level == 10, "20 - 4 - 6"
    assert deadshot.trained_level + deadshot.daevanion_bonus + deadshot.gear_bonus == 20


def test_active_and_passive_train_to_ten():
    for category in ("Active", "Passive"):
        s = Skill("X", category, 10, daevanion_bonus=0, gear_bonus=0)
        assert s.trained_cap == SKILL_TRAINED_CAP == 10
        assert s.at_cap
        assert s.levels_to_cap == 0


def test_stigmas_train_to_twenty_and_take_no_grants():
    """Stigmas receive no board or gear grants, so displayed is trained."""
    s = Skill("Vaizel's Authority", "Dp", 20, acquired=True, equipped=True)
    assert s.trained_cap == STIGMA_TRAINED_CAP == 20
    assert s.trained_level == 20
    assert s.at_cap

    short = Skill("Griffon Arrow", "Dp", 15, acquired=True, equipped=True)
    assert short.trained_level == 15
    assert short.levels_to_cap == 5


def test_trained_level_never_goes_negative():
    weird = Skill("X", "Active", 2, daevanion_bonus=5, gear_bonus=5)
    assert weird.trained_level == 0


# --------------------------------------------------------------------------
# skill sweep
# --------------------------------------------------------------------------


def test_skill_sweep_reports_active_gap(engine):
    char = _char(skills=[
        # shows 2 with no grants, so trained 2 of a cap of 10
        Skill("Explosion Trap", "Active", 2, acquired=True, equipped=True),
        # shows 20 but that is 10 trained plus 10 of grants: already capped
        Skill("Deadshot", "Active", 20, daevanion_bonus=4, gear_bonus=6,
              acquired=True, equipped=True),
    ])
    recs = engine.skill_levels(char)
    active = [r for r in recs if "active" in r.title]
    assert len(active) == 1
    assert "8 levels" in active[0].title, "10 cap minus 2 trained"
    assert "Explosion Trap 2/10" in active[0].detail
    assert "Deadshot" not in active[0].detail, "capped skills are not a gap"


def test_skill_sweep_ignores_unequipped_actives(engine):
    char = _char(skills=[
        Skill("Defiance", "Active", 1, acquired=True, equipped=False),
    ])
    assert [r for r in engine.skill_levels(char) if "active" in r.title] == []


def test_learned_but_unslotted_stigmas_are_flagged(engine):
    char = _char(skills=[
        Skill("Arrow Storm", "Dp", 0, acquired=True, equipped=False),
        Skill("Vaizel's Authority", "Dp", 20, acquired=True, equipped=True),
    ])
    recs = engine.skill_levels(char)
    idle = [r for r in recs if "not slotted" in r.title]
    assert len(idle) == 1
    assert "Arrow Storm" in idle[0].detail
    assert "Vaizel" not in idle[0].detail


def test_unlearned_stigmas_are_a_separate_lower_priority(engine):
    char = _char(skills=[
        Skill("Stealth", "Dp", 0, acquired=False, equipped=False),
        Skill("Arrow Storm", "Dp", 0, acquired=True, equipped=False),
    ])
    recs = engine.skill_levels(char)
    unlearned = [r for r in recs if "not yet learned" in r.title]
    idle = [r for r in recs if "not slotted" in r.title]
    assert unlearned and idle
    assert int(unlearned[0].priority) < int(idle[0].priority)


def test_no_skill_noise_when_everything_is_capped(engine):
    char = _char(skills=[
        Skill("Deadshot", "Active", 20, daevanion_bonus=4, gear_bonus=6,
              acquired=True, equipped=True),
        Skill("Rooting Eye", "Passive", 18, daevanion_bonus=4, gear_bonus=4,
              acquired=True),
    ])
    assert engine.skill_levels(char) == []


@needs_capture
def test_skill_sweep_against_real_character(engine):
    """Key skills are capped; the remaining training is all on other skills."""
    char = load_capture(CAPTURE)

    recs = engine.skill_levels(char)
    titles = " ".join(r.title for r in recs)

    # Every key skill is trained to its cap of 10, so the sweep must stay
    # quiet about them rather than inventing a gap.
    assert all(s.at_cap for s in engine.key_skills(char))
    assert "key skills" not in titles

    # Under-trained non-key skills are still reported, at low priority.
    assert "other active skills" in titles
    other = next(r for r in recs if "other active" in r.title)
    assert int(other.priority) <= int(Priority.LOW)


# --------------------------------------------------------------------------
# soul binding
# --------------------------------------------------------------------------


def _item(slot: int, slot_name: str, rate: float, name: str | None = None) -> Item:
    return Item(
        slot_pos=slot, slot_name=slot_name, item_id=1,
        name=name or slot_name, grade="Unique",
        enchant_level=0, max_enchant_level=0, soul_bind_rate=rate,
    )


def test_soul_bind_rate_parses_as_a_float():
    item = Item.parse(
        {"id": 1, "name": "Bow", "soulBindRate": "13.3"}, 1, "MainHand"
    )
    assert item.soul_bind_rate == pytest.approx(13.3)


def test_soul_bind_rate_defaults_to_full_when_absent():
    item = Item.parse({"id": 1, "name": "X"}, 1, "MainHand")
    assert item.soul_bind_rate == 100.0


def test_fully_bound_gear_produces_nothing(engine):
    char = _char(items=[_item(1, "MainHand", 100, "Bow"), _item(5, "Torso", 100)])
    assert engine.soul_binding(char) == []


def test_weapon_is_called_out_separately(engine):
    char = _char(items=[_item(1, "MainHand", 13.3, "Bow"), _item(5, "Torso", 20.0)])
    recs = engine.soul_binding(char)
    assert len(recs) == 2

    weapon = recs[0]
    assert "Bow" in weapon.title and "13.3" in weapon.title
    assert weapon.slot_name == "MainHand"
    # The weapon should outrank the grouped remainder.
    assert weapon.score > recs[1].score


def test_soul_binding_claims_no_stat_gain(engine):
    """What soul binding does mechanically is not published, so the engine
    must not invent a number."""
    char = _char(items=[_item(1, "MainHand", 13.3, "Bow"), _item(5, "Torso", 20.0)])
    for rec in engine.soul_binding(char):
        assert rec.gains == {}


@needs_capture
def test_real_gear_is_not_uniformly_bound():
    """Guards the wrong assumption that shipped first."""
    char = load_capture(CAPTURE)
    rates = {it.soul_bind_rate for it in char.items}
    assert len(rates) > 1, "soulBindRate is not a constant"
    assert min(rates) < 50, "our Ranger has genuinely low-bound gear"
