"""Best-in-slot comparisons.

The roll pool per item is not published and Global has no item catalog, so
the engine never invents what COULD roll. It compares against gear already
equipped, in two tiers:

* an identical item in another slot -- proof, since that exact item rolled
  that way
* a different item of the same category -- plausible, explicitly not proven
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aion2.models import Character, Item, Skill
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


def _ring(slot_pos: int, slot_name: str, stats, skills, item_id: int = 999):
    return Item.parse(
        {
            "id": item_id, "name": "Kromede Ring", "categoryName": "Ring",
            "subStatCount": 4, "subSkillCountMax": 4, "subStatRandom": True,
            "subStats": [
                {"id": sid, "name": sid, "value": str(v)} for sid, v in stats
            ],
            "subSkills": [
                {"id": i, "name": n, "level": lv}
                for i, (n, lv) in enumerate(skills)
            ],
        },
        slot_pos, slot_name,
    )


#: key_skills() resolves configured names against the character's own skill
#: list, so a bare Character has no key skills and everything drops a
#: priority band. Give the fixture the real ones.
KEY_SKILL_NAMES = (
    "Deadshot", "Snipe", "Gale Arrow", "Tempest Shot", "Drill Dart",
    "Rooting Eye", "Focused Eye", "Concentrated Fire", "Hunter's Soul",
    "Vigilant Eye",
)


def _char(items, skills=None):
    if skills is None:
        skills = [
            Skill(n, "Active", 10, acquired=True, equipped=True)
            for n in KEY_SKILL_NAMES
        ]
    return Character(
        name="T", class_name="Ranger", level=45, race="Elyos", server_name="S",
        region="eu", server_id=1302, combat_power=1, item_level=1,
        items=items, skills=skills,
    )


# --------------------------------------------------------------------------
# tier 1: identical items are proof
# --------------------------------------------------------------------------


def test_identical_item_rolled_better_is_proof(engine):
    """The rings case: one Kromede Ring grants two skills, so the other one
    demonstrably can."""
    good = _ring(13, "Ring1", [("HPRegen", 39)], [("Snipe", 1), ("Burst Arrow", 1)])
    bad = _ring(14, "Ring2", [("ArmorDefense", 220), ("CriticalResist", 41)], [])

    recs = engine.best_in_slot(_char([good, bad]))
    proof = [r for r in recs if "identical" in r.title]
    assert len(proof) == 1

    rec = proof[0]
    assert "Ring2" in rec.title
    assert "same item" in rec.detail
    assert "Snipe" in rec.detail
    assert rec.priority == Priority.HIGH, "missing a key skill is high priority"


def test_missing_a_non_key_skill_is_only_medium(engine):
    """Priority tracks whether the missing grant is one you actually want."""
    good = _ring(13, "Ring1", [("HPRegen", 39)], [("Explosion Trap", 1)])
    bad = _ring(14, "Ring2", [("ArmorDefense", 220)], [])

    recs = engine.best_in_slot(_char([good, bad]))
    proof = [r for r in recs if "identical" in r.title]
    assert proof and proof[0].priority == Priority.MEDIUM


def test_identical_items_rolled_the_same_say_nothing(engine):
    a = _ring(13, "Ring1", [("Critical", 30)], [("Snipe", 1)])
    b = _ring(14, "Ring2", [("Critical", 30)], [("Snipe", 1)])
    assert [r for r in engine.best_in_slot(_char([a, b])) if "identical" in r.title] == []


def test_different_item_ids_are_not_treated_as_proof(engine):
    a = _ring(13, "Ring1", [("Critical", 30)], [("Snipe", 1)], item_id=111)
    b = _ring(14, "Ring2", [("ArmorDefense", 220)], [], item_id=222)
    recs = engine.best_in_slot(_char([a, b]))
    assert not any("same item" in r.detail for r in recs)


# --------------------------------------------------------------------------
# tier 2: same category is plausible, not proven
# --------------------------------------------------------------------------


def test_same_category_comparison_is_labelled_unproven(engine):
    strong = _ring(13, "Ring1", [("WeaponAccuracy", 51), ("Critical", 33)], [], item_id=1)
    weak = _ring(14, "Ring2", [("HPRegen", 39), ("MPRegen", 25)], [], item_id=2)

    recs = engine.best_in_slot(_char([strong, weak]))
    cat = [r for r in recs if "weakest" in r.title]
    assert cat, "a clear value gap should be reported"
    assert "not proven" in cat[0].detail


def test_category_ranking_uses_weight_not_a_count(engine):
    """Counting wasted lines treats these as equal; weighting does not.
    Both carry exactly one zero-weight stat."""
    strong = _ring(13, "Ring1", [("WeaponAccuracy", 51), ("HPRegen", 39)], [], item_id=1)
    weak = _ring(14, "Ring2", [("Critical", 2), ("MPRegen", 25)], [], item_id=2)

    assert engine.slot_value(strong) > engine.slot_value(weak)


def test_percentage_stats_are_scaled(engine):
    """8.3% Weapon Damage Boost outranks a flat 8, which a naive reading of
    the number would not."""
    pct = Item.parse(
        {"id": 1, "name": "A", "categoryName": "Bow", "subStatCount": 1,
         "subStatRandom": True,
         "subStats": [{"id": "AmplifyWeaponDamage", "name": "x", "value": "8.3%"}]},
        1, "MainHand",
    )
    flat = Item.parse(
        {"id": 2, "name": "B", "categoryName": "Bow", "subStatCount": 1,
         "subStatRandom": True,
         "subStats": [{"id": "AmplifyWeaponDamage", "name": "x", "value": "8"}]},
        1, "MainHand",
    )
    assert engine.slot_value(pct) > engine.slot_value(flat) * 5


# --------------------------------------------------------------------------
# against the real character
# --------------------------------------------------------------------------


@needs_capture
def test_the_rings_case_fires_on_real_gear(engine):
    char = load_capture(CAPTURE)
    recs = engine.best_in_slot(char)

    proof = [r for r in recs if "identical" in r.title]
    assert proof, "both Kromede Rings are the same item id"
    assert "Snipe" in proof[0].detail


@needs_capture
def test_the_bow_is_the_strongest_slot(engine):
    """The user's own read: the bow's rolls are perfect for a Ranger."""
    char = load_capture(CAPTURE)
    rollable = [it for it in char.items if it.rerollable]
    best = max(rollable, key=engine.slot_value)
    assert best.slot_pos == 1


@needs_capture
def test_overview_names_the_weakest_slots(engine):
    char = load_capture(CAPTURE)
    overview = [
        r for r in engine.best_in_slot(char) if r.title.startswith("Weakest")
    ]
    assert len(overview) == 1
    assert overview[0].priority == Priority.INFO, "context, not an action"
