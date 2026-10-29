"""Comparing a build against a reference character."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aion2.compare import Gap, compare, skill_gaps
from aion2.models import Character, Item, Skill
from aion2.recommend import Engine, load_config
from aion2.replay import from_payloads

REFERENCE = Path(__file__).resolve().parents[1] / "data" / "reference" / "tadas.json"

needs_reference = pytest.mark.skipif(
    not REFERENCE.exists(),
    reason="run 'aion2 compare Tadas' to capture the reference build",
)


@pytest.fixture(scope="module")
def engine():
    return Engine(load_config())


# --------------------------------------------------------------------------
# Gap arithmetic
# --------------------------------------------------------------------------


def test_higher_is_better_detects_being_behind():
    g = Gap("CP", mine=100, theirs=200)
    assert g.behind and g.delta == 100


def test_lower_is_better_inverts_the_test():
    """Fewer wasted lines is better, so 35 against 20 is behind."""
    g = Gap("Wasted", mine=35, theirs=20, higher_is_better=False)
    assert g.behind
    assert not Gap("Wasted", mine=20, theirs=35, higher_is_better=False).behind


def test_ratio_is_zero_when_ahead():
    assert Gap("CP", mine=300, theirs=200).ratio == 0.0


def test_outcomes_are_excluded_from_the_gap_list():
    """Combat Power is the RESULT of the other dimensions, so ranking it as
    something to attack is circular."""
    c = _comparison([
        Gap("Combat Power", 100, 500, outcome=True),
        Gap("Soul binding", 60, 90, unit="%"),
    ])
    names = [g.dimension for g in c.behind_on]
    assert "Combat Power" not in names
    assert "Soul binding" in names


def test_formatting_respects_unit_and_places():
    assert Gap("x", 0, 0, unit="%").format(63.42) == "63.4%"
    assert Gap("x", 0, 0, places=2).format(1.32203) == "1.32"
    assert Gap("x", 0, 0).format(160084) == "160,084"


def _comparison(gaps):
    from aion2.compare import Comparison

    blank = Character(
        name="a", class_name="Ranger", level=45, race="E", server_name="S",
        region="eu", server_id=1, combat_power=0, item_level=0,
    )
    return Comparison(mine=blank, theirs=blank, gaps=gaps)


# --------------------------------------------------------------------------
# dimensions
# --------------------------------------------------------------------------


def _char(name, **kw):
    return Character(
        name=name, class_name="Ranger", level=45, race="Elyos", server_name="S",
        region="eu", server_id=1302, combat_power=kw.pop("cp", 1),
        item_level=kw.pop("il", 1), **kw,
    )


def _item(slot, enchant, cap, bind=100.0, item_id=1):
    return Item(
        slot_pos=slot, slot_name=f"s{slot}", item_id=item_id, name=f"i{slot}",
        grade="Unique", enchant_level=enchant, max_enchant_level=cap,
        soul_bind_rate=bind,
    )


def test_enhancement_completion_is_a_share_not_a_count(engine):
    """A character with more items can have more enhancement levels while
    being proportionally less enhanced."""
    behind = _char("a", items=[_item(1, 5, 10), _item(2, 5, 10)])
    ahead = _char("b", items=[_item(1, 10, 10), _item(2, 10, 10)])

    c = compare(behind, ahead, engine)
    g = next(x for x in c.gaps if x.dimension == "Enhancement completion")
    assert g.mine == 50.0 and g.theirs == 100.0


def test_soul_binding_is_averaged(engine):
    a = _char("a", items=[_item(1, 0, 0, bind=20), _item(2, 0, 0, bind=40)])
    b = _char("b", items=[_item(1, 0, 0, bind=90), _item(2, 0, 0, bind=100)])
    g = next(
        x for x in compare(a, b, engine).gaps if x.dimension == "Soul binding"
    )
    assert g.mine == 30.0 and g.theirs == 95.0


def test_gear_overlap_separates_same_items_from_upgrades(engine):
    a = _char("a", items=[_item(1, 0, 0, item_id=111), _item(2, 0, 0, item_id=222)])
    b = _char("b", items=[_item(1, 0, 0, item_id=111), _item(2, 0, 0, item_id=999)])

    notes = " ".join(compare(a, b, engine).notes)
    assert "1 of their slots hold a different item" in notes
    assert "1 slots use the same item" in notes


def test_skill_gaps_only_reports_where_they_lead(engine):
    mine = _char("a", skills=[
        Skill("Deadshot", "Active", 20, acquired=True, equipped=True),
        Skill("Snipe", "Active", 17, acquired=True, equipped=True),
    ])
    theirs = _char("b", skills=[
        Skill("Deadshot", "Active", 20, acquired=True, equipped=True),
        Skill("Snipe", "Active", 14, acquired=True, equipped=True),
    ])
    assert skill_gaps(mine, theirs, engine) == []

    gaps = skill_gaps(theirs, mine, engine)
    assert gaps == [("Snipe", 14, 17)]


# --------------------------------------------------------------------------
# against the real reference build
# --------------------------------------------------------------------------


@needs_reference
def test_reference_build_parses():
    theirs = from_payloads(json.loads(REFERENCE.read_text(encoding="utf-8")))
    assert theirs.name == "Tadas"
    assert theirs.class_name == "Ranger"
    assert theirs.combat_power > 0


@needs_reference
def test_reference_is_nearly_fully_enhanced(engine):
    """The headline finding: their CP lead is mostly enhancement."""
    theirs = from_payloads(json.loads(REFERENCE.read_text(encoding="utf-8")))
    assert theirs.enhancement_levels_available < 20


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------


def test_preset_switch_is_flagged_when_many_slots_change():
    """PLAYNC exposes only the active preset, so a wholesale gear swap between
    snapshots is far more likely a preset switch than progression."""
    from aion2.compare import PRESET_SWITCH_SLOTS, detect_preset_change

    before = _char("a", cp=160000, items=[
        _item(i, 11, 15, item_id=100 + i) for i in range(1, 9)
    ])
    after = _char("a", cp=120000, items=[
        _item(i, 11, 15, item_id=900 + i) for i in range(1, 9)
    ])

    change = detect_preset_change(before, after)
    assert change.slots_changed == 8
    assert change.likely
    assert change.cp_delta == -40000
    assert len(change.changed_slots) == 8


def test_one_item_upgrade_is_not_a_preset_switch():
    from aion2.compare import detect_preset_change

    before = _char("a", items=[_item(i, 11, 15, item_id=100 + i) for i in range(1, 9)])
    items = [_item(i, 11, 15, item_id=100 + i) for i in range(1, 9)]
    items[0] = _item(1, 11, 15, item_id=777)
    after = _char("a", items=items)

    change = detect_preset_change(before, after)
    assert change.slots_changed == 1
    assert not change.likely


def test_enhancing_without_swapping_is_not_a_preset_switch():
    """Same items at higher enchant levels is progression, not a swap."""
    from aion2.compare import detect_preset_change

    before = _char("a", items=[_item(i, 11, 15, item_id=100 + i) for i in range(1, 9)])
    after = _char("a", items=[_item(i, 15, 15, item_id=100 + i) for i in range(1, 9)])

    change = detect_preset_change(before, after)
    assert change.slots_changed == 0
    assert not change.likely


def test_slots_only_in_one_snapshot_are_not_counted():
    """An item added to a previously empty slot is not a change of preset."""
    from aion2.compare import detect_preset_change

    before = _char("a", items=[_item(1, 0, 10, item_id=1)])
    after = _char("a", items=[_item(1, 0, 10, item_id=1), _item(2, 0, 10, item_id=2)])
    assert detect_preset_change(before, after).slots_changed == 0
