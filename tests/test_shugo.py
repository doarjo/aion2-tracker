"""Shugo.GG item catalog.

Shugo.GG's robots.txt disallows /api/ for every agent, naming ClaudeBot,
Claude-User and anthropic-ai specifically, while their llms.txt explicitly
offers the server-rendered /items/<id> pages with the random stats pool. This
module reads the pages, never the API, and these tests pin that down so the
boundary cannot be "optimised" away later.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aion2 import shugo
from aion2.shugo import CatalogItem, RollOption, ShugoCatalog, parse_seed, to_catalog_item

SEED_PAGE = """<!DOCTYPE html><html><head><title>Enraged Kromede Ring</title></head>
<body><script type="application/json" id="shugo-seed">{"/api/items/detail/310330053":
{"id":310330053,"name":"Enraged Kromede Ring","grade":"Unique",
"categoryName":"Ring","equipLevel":45,"maxEnchantLevel":15,
"maxExceedEnchantLevel":5,"magicStoneSlotCount":4,"subStatCount":4,
"subSkillCountMax":4,"subStatRandom":true,
"mainStats":[{"id":"WeaponFixingDamage","name":"Attack","value":"43"}],
"subStats":[
 {"id":"WeaponAccuracy","name":"Accuracy","minValue":"18","value":"28"},
 {"id":"Critical","name":"Critical Hit","minValue":"10","value":"19"},
 {"id":"Block","name":"Block","minValue":"15","value":"24"},
 {"id":"CombatSpeed","name":"Combat Speed","minValue":"10.4%","value":"12%"}],
"sources":["Reward Chest","Expedition"]}}</script></body></html>"""


# --------------------------------------------------------------------------
# the access boundary
# --------------------------------------------------------------------------


def test_only_the_rendered_page_url_is_used():
    """Their robots.txt disallows /api/. The module must target /items/."""
    assert shugo.ITEM_PAGE == "https://shugo.gg/items/{item_id}"
    assert "/api/" not in shugo.ITEM_PAGE


def test_user_agent_identifies_the_tool_honestly():
    """No pretending to be a browser against a site that asked to be cited."""
    assert "aion2-tracker" in shugo.USER_AGENT
    assert "Mozilla" not in shugo.USER_AGENT


def test_attribution_string_exists():
    assert "Shugo.GG" in shugo.ATTRIBUTION


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parse_seed_extracts_the_item_payload():
    payload = parse_seed(SEED_PAGE)
    assert payload is not None
    assert payload["id"] == 310330053
    assert payload["name"] == "Enraged Kromede Ring"


def test_parse_seed_returns_none_without_the_block():
    assert parse_seed("<html><body>nothing here</body></html>") is None


def test_parse_seed_survives_malformed_json():
    assert parse_seed(
        '<script type="application/json" id="shugo-seed">{not json</script>'
    ) is None


def test_to_catalog_item_builds_the_roll_pool():
    entry = to_catalog_item(parse_seed(SEED_PAGE))
    assert isinstance(entry, CatalogItem)
    assert entry.item_id == 310330053
    assert entry.category == "Ring"
    assert entry.max_exceed_level == 5
    assert entry.sources == ["Reward Chest", "Expedition"]
    assert len(entry.roll_pool) == 4

    acc = entry.option("WeaponAccuracy")
    assert acc is not None
    assert acc.min_value == 18
    assert acc.max_value == 28
    assert not acc.percent


def test_percentage_options_are_flagged():
    entry = to_catalog_item(parse_seed(SEED_PAGE))
    speed = entry.option("CombatSpeed")
    assert speed is not None and speed.percent
    assert speed.min_value == pytest.approx(10.4)


def test_unknown_stat_returns_none():
    entry = to_catalog_item(parse_seed(SEED_PAGE))
    assert entry.option("NotAStat") is None


def test_item_url_points_at_the_page_for_citation():
    entry = to_catalog_item(parse_seed(SEED_PAGE))
    assert entry.url == "https://shugo.gg/items/310330053"


# --------------------------------------------------------------------------
# roll quality
# --------------------------------------------------------------------------


def test_quality_positions_a_roll_in_its_range():
    opt = RollOption("X", "X", 10.0, 20.0)
    assert opt.quality(10) == 0.0
    assert opt.quality(15) == pytest.approx(0.5)
    assert opt.quality(20) == 1.0


def test_quality_clamps_outside_the_range():
    opt = RollOption("X", "X", 10.0, 20.0)
    assert opt.quality(5) == 0.0
    assert opt.quality(99) == 1.0


def test_quality_of_a_fixed_stat_is_full():
    assert RollOption("X", "X", 12.0, 12.0).quality(12) == 1.0


# --------------------------------------------------------------------------
# caching and offline behaviour
# --------------------------------------------------------------------------


def test_offline_mode_never_touches_the_network(tmp_path):
    db = tmp_path / "t.db"
    with ShugoCatalog(db, offline=True) as cat:
        assert cat.item(999999) is None
        assert cat.cached_count() == 0


def test_cache_round_trip(tmp_path):
    db = tmp_path / "t.db"
    payload = parse_seed(SEED_PAGE)
    with ShugoCatalog(db, offline=True) as cat:
        cat._store(310330053, payload)  # noqa: SLF001
        assert cat.cached_count() == 1

    # A fresh instance, still offline, must read it back from disk.
    with ShugoCatalog(db, offline=True) as cat:
        entry = cat.item(310330053)
        assert entry is not None
        assert entry.name == "Enraged Kromede Ring"


def test_missing_catalog_entry_is_not_an_error(tmp_path):
    """The tracker must work without a catalog, just with less to say."""
    db = tmp_path / "t.db"
    with ShugoCatalog(db, offline=True) as cat:
        assert cat.item(1) is None
        assert cat.item(1) is None  # memoised, still fine


def test_rate_limit_has_a_floor(tmp_path):
    cat = ShugoCatalog(tmp_path / "t.db", rate_limit_seconds=0.01)
    try:
        assert cat.rate_limit_seconds >= 1.0
    finally:
        cat.close()
