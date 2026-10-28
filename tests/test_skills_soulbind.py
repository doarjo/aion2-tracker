"""Skill training sweep and soul binding.

Both exist because the first pass missed them:

* skill_targets only checked six named skills, all of which passed, so the
  engine said nothing while 20 of 35 skills sat below the cap.
* soulBindRate was recorded as "always 100" after looking at two sample
  characters who happened to be fully bound. Our own Ranger's bow is 13.3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aion2.models import SKILL_LEVEL_CAP, Character, Item, Skill
from aion2.recommend import Engine, load_config
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


def test_levels_to_cap():
    assert Skill("A", "Active", 9).levels_to_cap == SKILL_LEVEL_CAP - 9
    assert Skill("A", "Active", SKILL_LEVEL_CAP).levels_to_cap == 0
    assert Skill("A", "Active", 99).levels_to_cap == 0


# --------------------------------------------------------------------------
# skill sweep
# --------------------------------------------------------------------------


def test_skill_sweep_reports_active_gap(engine):
    char = _char(skills=[
        Skill("Explosion Trap", "Active", 2, acquired=True, equipped=True),
        Skill("Deadshot", "Active", 20, acquired=True, equipped=True),
    ])
    recs = engine.skill_levels(char)
    active = [r for r in recs if "active" in r.title]
    assert len(active) == 1
    assert "18 levels" in active[0].title
    assert "Explosion Trap 2/20" in active[0].detail
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
        Skill("Deadshot", "Active", 20, acquired=True, equipped=True),
        Skill("Rooting Eye", "Passive", 20, acquired=True),
    ])
    assert engine.skill_levels(char) == []


@needs_capture
def test_skill_sweep_against_real_character(engine):
    """The named-target check passes entirely, which is exactly why the sweep
    was needed."""
    char = load_capture(CAPTURE)
    assert engine.skill_targets(char) == [], "named targets all met"

    recs = engine.skill_levels(char)
    assert recs, "but there is still plenty of training left"
    titles = " ".join(r.title for r in recs)
    assert "active skill training" in titles


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
