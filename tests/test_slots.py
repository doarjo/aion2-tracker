"""Sub-stat and skill slots share one pool per item.

Regression test for a wrong reading that shipped: comparing `subSkills`
against `subSkillCountMax` while ignoring `subStats` reported 48 empty slots
across the character. Every item was actually full. The bow in particular
carries five rolled offensive stats and was reported as five empty slots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aion2.models import Item
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


# --------------------------------------------------------------------------
# the shared pool
# --------------------------------------------------------------------------


def test_rolled_stats_occupy_skill_slots():
    """A bow with five rolled stats and no skill grants is FULL."""
    bow = Item.parse(
        {
            "id": 1, "name": "Bow",
            "subStatCount": 5, "subSkillCountMax": 5, "subStatRandom": True,
            "subStats": [
                {"id": "AmplifyWeaponDamage", "name": "Weapon Damage Boost", "value": "8.3%"},
                {"id": "AdditionalHitRate", "name": "Multi-hit Chance", "value": "8.3%"},
                {"id": "STR", "name": "Might", "value": "28"},
                {"id": "CombatSpeed", "name": "Combat Speed", "value": "11.2%"},
                {"id": "AmplifyAllDamage", "name": "Damage Boost", "value": "6.3%"},
            ],
        },
        1, "MainHand",
    )
    assert bow.slot_capacity == 5
    assert bow.slots_used == 5
    assert bow.empty_slots == 0, "the bug reported 5 empty here"


def test_mixed_stats_and_skills_fill_the_same_pool():
    ring = Item.parse(
        {
            "id": 1, "name": "Ring",
            "subStatCount": 4, "subSkillCountMax": 4, "subStatRandom": True,
            "subStats": [
                {"id": "HPRegen", "name": "Natural HP Regen", "value": "39"},
                {"id": "ArmorDefense", "name": "Defense", "value": "218"},
            ],
            "subSkills": [
                {"id": 1, "name": "Snipe", "level": 1},
                {"id": 2, "name": "Burst Arrow", "level": 1},
            ],
        },
        13, "Ring1",
    )
    assert ring.slot_capacity == 4
    assert ring.slots_used == 4
    assert ring.empty_slots == 0


def test_capacity_takes_the_larger_of_the_two_counts():
    """Arcana report subStatCount 0 with subSkillCountMax 4."""
    arcana = Item.parse(
        {
            "id": 1, "name": "Chalice",
            "subStatCount": 0, "subSkillCountMax": 4,
            "subSkills": [{"id": i, "name": f"S{i}", "level": 1} for i in range(4)],
        },
        41, "Arcana1",
    )
    assert arcana.slot_capacity == 4
    assert arcana.empty_slots == 0


# --------------------------------------------------------------------------
# fixed rolls
# --------------------------------------------------------------------------


def test_fixed_roll_items_are_not_rerollable():
    """Bracelets report subStatRandom false with minValue == value. Telling
    the player to reroll them is advice they cannot act on."""
    bracelet = Item.parse(
        {
            "id": 1, "name": "Ascension Bracelet",
            "subStatCount": 0, "subSkillCountMax": 0, "subStatRandom": False,
            "subStats": [
                {"id": "Evasion", "name": "Evasion", "minValue": "12", "value": "12"},
                {"id": "Critical", "name": "Critical Hit", "minValue": "9", "value": "9"},
            ],
        },
        15, "Bracelet1",
    )
    assert not bracelet.rerollable
    assert bracelet.reclaimable_slots == []


def test_random_roll_items_are_rerollable():
    helm = Item.parse(
        {
            "id": 1, "name": "Helm",
            "subStatCount": 4, "subSkillCountMax": 4, "subStatRandom": True,
            "subStats": [{"id": "Block", "name": "Block", "value": "25"}],
        },
        3, "Helmet",
    )
    assert helm.rerollable
    assert len(helm.reclaimable_slots) == 1


def test_reroll_advice_skips_fixed_items(engine):
    from aion2.models import Character

    fixed = Item.parse(
        {
            "id": 1, "name": "Ascension Bracelet",
            "subStatCount": 0, "subSkillCountMax": 0, "subStatRandom": False,
            "subStats": [
                {"id": "Evasion", "name": "Evasion", "minValue": "12", "value": "12"}
            ],
        },
        15, "Bracelet1",
    )
    char = Character(
        name="T", class_name="Ranger", level=45, race="Elyos", server_name="S",
        region="eu", server_id=1302, combat_power=1, item_level=1, items=[fixed],
    )
    assert engine.reroll_candidates(char) == []


# --------------------------------------------------------------------------
# against the real character
# --------------------------------------------------------------------------


@needs_capture
def test_real_gear_has_no_empty_slots():
    char = load_capture(CAPTURE)
    assert sum(it.empty_slots for it in char.items) == 0, (
        "every item is full; the 48-empty-slots claim was wrong"
    )
    assert sum(it.slot_capacity for it in char.items) > 0


@needs_capture
def test_the_bow_is_already_optimal():
    """Five slots, all offensive, nothing reclaimable."""
    char = load_capture(CAPTURE)
    bow = char.item_at(1)
    assert bow is not None
    assert bow.slot_capacity == 5
    assert bow.empty_slots == 0
    assert bow.reclaimable_slots == [], "the bow's rolls are all useful"


@needs_capture
def test_wasted_slots_are_the_real_opportunity(engine):
    char = load_capture(CAPTURE)
    wasted = sum(len(it.reclaimable_slots) for it in char.items)
    assert wasted > 0

    recs = engine.skill_slots(char)
    titles = " ".join(r.title for r in recs)
    assert "wasted on defensive stats" in titles
    assert "empty" not in titles, "nothing is genuinely empty"
