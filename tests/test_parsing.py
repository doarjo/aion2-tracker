"""Tests that lock in the Phase 0 traps.

Each test here exists because the behaviour it checks actually bit us, or
would have. See docs/endpoints.md for the evidence behind each one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aion2 import stats as S
from aion2.client import _strip_highlight
from aion2.models import Item, parse_effect

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    with (FIXTURES / name).open(encoding="utf-8-sig") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# optional arrays
# --------------------------------------------------------------------------


def test_belt_has_only_mainstats():
    """Belt, Amulet and Runes omit subStats and magicStoneStat entirely.
    Keys are ABSENT, not empty, so a direct index raises."""
    raw = load("23-item-belt-mainstats-only.json")
    assert "subStats" not in raw
    assert "magicStoneStat" not in raw

    item = Item.parse(raw, slot_pos=17, slot_name="Belt")
    assert item.main_stats, "belt should still have main stats"
    assert item.sub_stats == []
    assert item.stones == []
    assert item.wasted_lines == []


def test_necklace_omits_godstone_key():
    raw = load("21-item-necklace.json")
    assert "godStoneStat" not in raw
    item = Item.parse(raw, slot_pos=10, slot_name="Necklace")
    assert item.god_stones == []


def test_arcana_carries_subskills_and_set():
    """Arcana are the only items with subSkills and set."""
    raw = load("32-item-arcana-subskills-set.json")
    item = Item.parse(raw, slot_pos=41, slot_name="Arcana1")

    assert item.is_arcana
    assert item.set_name == "Primal Vigor"
    assert item.set_equipped == 4
    assert dict(item.sub_skills)["Deadshot"] == 3
    assert len(item.sub_skills) == 4


# --------------------------------------------------------------------------
# the AbnormalAccuracy trap
# --------------------------------------------------------------------------


def test_abnormal_accuracy_is_not_accuracy():
    """`AbnormalAccuracy` means Status Effect Chance. Substring matching on
    'Accuracy' silently inflates the total -- it added a phantom 18 points on
    our Ranger."""
    assert not S.is_accuracy("AbnormalAccuracy")
    assert S.is_accuracy("WeaponAccuracy")
    assert S.is_accuracy("Accuracy")

    # The naive implementation that this guards against:
    assert "Accuracy" in "AbnormalAccuracy"


def test_both_accuracy_ids_count():
    """Two different ids are both genuine flat Accuracy. Matching only one
    under-reports."""
    assert S.ACCURACY_IDS == {"WeaponAccuracy", "Accuracy"}


def test_bow_accuracy_excludes_status_effect_chance():
    raw = load("33-item-bow.json")
    item = Item.parse(raw, slot_pos=1, slot_name="MainHand")

    ids = {line.stat_id for line in item.all_lines}
    accuracy = sum(
        line.value for line in item.all_lines if S.is_accuracy(line.stat_id)
    )
    naive = sum(line.value for line in item.all_lines if "Accuracy" in line.stat_id)

    if "AbnormalAccuracy" in ids:
        assert naive > accuracy, "fixture should demonstrate the trap"
    assert accuracy >= 0


# --------------------------------------------------------------------------
# value + extra
# --------------------------------------------------------------------------


def test_value_plus_extra_is_the_displayed_total():
    """Verified against the questlog.gg screenshot: the Nuakum Helm shows
    Defense 752 and HP 435, which is value + extra."""
    raw = load("30-ranger-info.json")  # sanity: fixture set is the Ranger's
    assert raw["profile"]["characterName"] == "Epitet"

    helm = {
        "id": 1,
        "name": "Nuakum Helm",
        "enchantLevel": 11,
        "maxEnchantLevel": 15,
        "mainStats": [
            {"id": "ArmorDefense", "name": "Defense", "value": "477", "extra": "275"},
            {"id": "HPMax", "name": "HP", "value": "215", "extra": "220"},
        ],
    }
    item = Item.parse(helm, slot_pos=3, slot_name="Helmet")
    totals = {line.stat_id: line.value for line in item.main_stats}
    assert totals["ArmorDefense"] == 752
    assert totals["HPMax"] == 435


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def test_search_strips_highlight_tags():
    assert _strip_highlight("<strong>Kira</strong>") == "Kira"
    assert _strip_highlight("A<strong>kira</strong>") == "Akira"
    assert _strip_highlight("Epitet") == "Epitet"


def test_search_results_carry_highlight_tags():
    raw = load("03-search-results.json")
    assert raw["list"], "fixture should contain matches"
    assert any("<strong>" in e["name"] for e in raw["list"])


def test_empty_search_is_not_an_error():
    """An unmatched keyword returns 200 with an empty list."""
    raw = load("04-search-empty.json")
    assert raw["list"] == []
    assert raw["pagination"]["total"] == 0


def test_character_id_arrives_percent_encoded():
    raw = load("03-search-results.json")
    ids = [e["characterId"] for e in raw["list"]]
    assert any("%3D" in cid for cid in ids), (
        "characterId must stay percent-encoded; re-encoding it causes a 404"
    )


# --------------------------------------------------------------------------
# localization
# --------------------------------------------------------------------------


def test_item_level_name_is_untranslated_korean():
    """Key on `type`, never on `name`."""
    raw = load("30-ranger-info.json")
    entry = next(
        s for s in raw["stat"]["statList"] if s["type"] == "ItemLevel"
    )
    assert not entry["name"].isascii(), (
        "ItemLevel still returns a Korean label even with lang=en-US"
    )
    assert entry["value"] > 0


def test_precision_is_agi_not_pre():
    assert S.BASE_ATTRIBUTES["AGI"] == "Precision"
    assert S.BASE_ATTRIBUTES["DEX"] == "Dexterity"


# --------------------------------------------------------------------------
# daevanion
# --------------------------------------------------------------------------


def test_board_ids_differ_by_class():
    """Hardcoding board ids breaks across classes."""
    glad = load("10-info.json")
    ranger = load("30-ranger-info.json")

    glad_ids = {b["id"] for b in glad["daevanion"]["boardList"]}
    ranger_ids = {b["id"] for b in ranger["daevanion"]["boardList"]}

    assert glad_ids & ranger_ids == set(), (
        "board id sets must be disjoint across classes"
    )


def test_board_with_no_open_nodes_returns_empty_not_null():
    raw = load("34-daevanion-ranger-36.json")
    assert isinstance(raw["openStatEffectList"], list)
    assert isinstance(raw["openSkillEffectList"], list)


def test_total_node_count_excludes_padding():
    """totalNodeCount counts Stat + SkillLevel only, not None padding or Start."""
    info = load("30-ranger-info.json")
    detail = load("34-daevanion-ranger-36.json")

    board_id = 36
    summary = next(b for b in info["daevanion"]["boardList"] if b["id"] == board_id)
    real = sum(
        1 for n in detail["nodeList"] if n["type"] in ("Stat", "SkillLevel")
    )
    assert real == summary["totalNodeCount"]

    opened = sum(1 for n in detail["nodeList"] if n["open"] == 1)
    assert opened == summary["openNodeCount"]


@pytest.mark.parametrize(
    "desc,expected",
    [
        ("Critical Hit +30", ("Critical Hit", 30.0, False)),
        ("Cooldown Reduction +3%", ("Cooldown Reduction", 3.0, True)),
        ("Keen Strike +1", ("Keen Strike", 1.0, False)),
        ("Multi-hit Resist +4.5%", ("Multi-hit Resist", 4.5, True)),
    ],
)
def test_parse_effect(desc, expected):
    assert parse_effect(desc) == expected


def test_parse_effect_rejects_garbage():
    assert parse_effect("not an effect") is None
    assert parse_effect("") is None
