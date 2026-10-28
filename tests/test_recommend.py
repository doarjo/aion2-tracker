"""Recommendation engine behaviour.

These guard the quality bugs found during the first run: recommending a stone
grade the player cannot obtain, and recommending a one-off drop twenty times.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aion2 import stats as S
from aion2.recommend import Engine, Priority, load_config
from aion2.replay import load_capture

CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "epitet"

pytestmark = pytest.mark.skipif(
    not CAPTURE.is_dir(),
    reason="run scripts/discover.ps1 to create captures/epitet",
)


@pytest.fixture(scope="module")
def char():
    return load_capture(CAPTURE)


@pytest.fixture(scope="module")
def engine():
    return Engine(load_config())


# --------------------------------------------------------------------------


def test_character_parses(char):
    assert char.name == "Epitet"
    assert char.class_name == "Ranger"
    assert char.level == 45
    assert len(char.items) == 25, "Ranger has 25 slots including 5 Arcana"
    assert len(char.boards) == 6


def test_arcana_slots_present(char):
    arcana = [i for i in char.items if i.is_arcana]
    assert len(arcana) == 5
    assert all(41 <= i.slot_pos <= 45 for i in arcana)


def test_effective_skill_level_sums_three_sources(char):
    """Reading skillLevel alone under-reports. Deadshot is 20 listed but 30
    effective once Daevanion and Arcana are counted."""
    deadshot = char.skill("Deadshot")
    assert deadshot is not None
    assert deadshot.base_level == 20
    assert deadshot.daevanion_bonus > 0
    assert deadshot.arcana_bonus > 0
    assert deadshot.effective_level == (
        deadshot.base_level + deadshot.daevanion_bonus + deadshot.arcana_bonus
    )
    assert deadshot.effective_level > deadshot.base_level


def test_stigmas_are_category_dp(char):
    stigmas = [s for s in char.skills if s.is_stigma]
    assert stigmas, "Ranger should have Dp skills"
    names = {s.name for s in stigmas}
    assert "Vaizel's Authority" in names
    # Dp skills receive no Daevanion or Arcana bonuses.
    for s in stigmas:
        assert s.daevanion_bonus == 0
        assert s.arcana_bonus == 0


def test_gear_accuracy_excludes_status_effect_chance(char):
    """The trap: substring-matching stat ids picks up AbnormalAccuracy, which
    is Status Effect Chance measured in percent.

    On this character it adds a phantom 18 points. Note the trap only bites an
    implementation that ALSO forgets to exclude percentage lines, which is
    exactly what our first analysis script did.
    """
    correct = char.gear_accuracy

    naive = sum(
        line.value
        for item in char.items
        for line in item.all_lines
        if "Accuracy" in line.stat_id  # no exact match, no percent check
    )
    assert naive > correct, "fixture must exercise the AbnormalAccuracy trap"

    phantom = sum(
        line.value
        for item in char.items
        for line in item.all_lines
        if line.stat_id == "AbnormalAccuracy"
    )
    assert phantom > 0
    assert naive - correct == pytest.approx(phantom)


def test_abnormal_accuracy_is_present_and_is_a_percentage(char):
    lines = [
        line
        for item in char.items
        for line in item.all_lines
        if line.stat_id == "AbnormalAccuracy"
    ]
    assert lines, "fixture should contain the trap stat"
    assert all(line.percent for line in lines)
    assert all(not S.is_accuracy(line.stat_id) for line in lines)


# --------------------------------------------------------------------------
# recommendation quality
# --------------------------------------------------------------------------


def test_audit_produces_ranked_recommendations(char, engine):
    recs = engine.audit(char)
    assert recs
    ranks = [int(r.priority) for r in recs]
    assert ranks == sorted(ranks, reverse=True), "must be highest priority first"


def test_manastone_swaps_respect_grade(char, engine):
    """Never suggest replacing a Common stone with a Unique one -- that is not
    an action the player can take."""
    for rec in engine.manastone_swaps(char):
        if not rec.gains:
            continue
        grade = next(g for g in S.MANASTONE_GRADES if g in rec.title)
        for stat_id, total in rec.gains.items():
            assert S.stone_value(stat_id, grade) is not None, (
                f"{rec.title} suggests {stat_id} at {grade}, which has never "
                f"been observed at that grade"
            )


def test_never_recommends_the_one_off_cloak_stone(char, engine):
    """AmplifyWeaponDamage Unique +100 was seen exactly once, on a cloak.
    Treating it as farmable produced twenty nonsense recommendations."""
    assert "AmplifyWeaponDamage" not in S.RECOMMENDABLE_STONES
    for rec in engine.audit(char):
        assert "AmplifyWeaponDamage" not in rec.gains


def test_swaps_are_grouped_not_one_per_stone(char, engine):
    """Grouped by (stat, grade) so the output is actionable."""
    swaps = engine.manastone_swaps(char)
    wasted_stones = sum(
        1 for it in char.items for st in it.stones if engine.is_wasted(st.stat_id)
    )
    assert len(swaps) < wasted_stones, "should group, not emit one per stone"


def test_accuracy_headroom_is_grade_honest(char, engine):
    """Headroom only counts stones that have an Accuracy equivalent at their
    own grade, so it is a floor rather than a best case."""
    headroom = engine._accuracy_headroom(char)  # noqa: SLF001
    best_case = sum(
        10
        for it in char.items
        for st in it.stones
        if engine.is_wasted(st.stat_id)
    )
    assert 0 < headroom <= best_case


def test_dexterity_over_precision_is_flagged(char, engine):
    assert char.attributes["DEX"] > char.attributes["AGI"]
    recs = engine.attribute_checks(char)
    assert any("Dexterity" in r.title for r in recs)


def test_skill_targets_all_met(char, engine):
    """Every target in the shipped thresholds file is satisfied, so the engine
    should raise nothing for skills."""
    assert engine.skill_targets(char) == []


def test_arcana_enhancement_flagged_for_plus_zero(char, engine):
    recs = engine.arcana(char)
    titles = " ".join(r.title for r in recs)
    assert "Bell of Vigor" in titles
    assert "Mirror of Vigor" in titles


def test_main_stats_never_flagged_as_wasted(char):
    """Fixed main stats cannot be changed, so flagging them is noise."""
    for item in char.items:
        for line in item.main_stats:
            assert not line.wasted


def test_accuracy_summary_is_explicit_about_what_it_is_not(char, engine):
    rec = engine.accuracy_summary(char)
    assert rec.priority in (Priority.CRITICAL, Priority.INFO)
    assert "not your in-game total" in rec.detail.lower()
