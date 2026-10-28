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


def test_displayed_level_is_not_a_base_to_add_to(char):
    """skillLevel from the API is what the game SHOWS and already contains the
    grants. Adding them reported Deadshot as 30 against an in-game 20."""
    deadshot = char.skill("Deadshot")
    assert deadshot is not None
    assert deadshot.displayed_level == 20
    assert deadshot.daevanion_bonus == 4
    assert deadshot.gear_bonus == 6
    assert deadshot.trained_level == 10
    assert deadshot.at_cap


def test_every_capped_skill_lands_on_exactly_ten(char):
    """The evidence for a trained cap of 10: subtract the grants and 18 of 26
    skills land on exactly 10, and none exceeds it."""
    trained = [
        s.trained_level for s in char.skills
        if not s.is_stigma and s.displayed_level > 0
    ]
    assert trained
    assert max(trained) == 10, "nothing trains past 10"
    assert trained.count(10) >= 15, "most skills are already capped"


def test_gear_skill_grants_are_not_arcana_only(char):
    """The field was called arcana_bonus, which hid the fact that rings,
    cloak, pauldrons, breastplate and necklace all grant skill levels too."""
    granting = [it for it in char.items if it.sub_skills]
    non_arcana = [it for it in granting if not it.is_arcana]

    assert non_arcana, "non-Arcana items grant skill levels"
    slots = {it.slot_name for it in non_arcana}
    assert "Ring1" in slots or "Ring2" in slots


def test_stigmas_are_category_dp(char):
    stigmas = [s for s in char.skills if s.is_stigma]
    assert stigmas, "Ranger should have Dp skills"
    names = {s.name for s in stigmas}
    assert "Vaizel's Authority" in names
    # Dp skills receive no Daevanion or gear grants, so displayed is trained.
    for s in stigmas:
        assert s.daevanion_bonus == 0
        assert s.gear_bonus == 0
        assert s.trained_level == s.displayed_level
        assert s.trained_cap == 20


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


def test_skill_targets_uses_the_displayed_level(char, engine):
    """Gale Arrow shows 16 against a target of 20. The old inflated maths
    reported it as 22 and hid the shortfall entirely."""
    recs = engine.skill_targets(char)
    gale = [r for r in recs if "Gale Arrow" in r.title]
    assert gale, "a genuine shortfall the old model concealed"
    assert "4 levels below target" in gale[0].title

    # Training is already maxed, so the advice must point at grants.
    assert "Training is capped" in gale[0].detail


def test_arcana_reports_sets_not_enhancement(char, engine):
    """Arcana enhancement is covered by enhancement_upgrades, which handles
    every item. Emitting it from both produced duplicate rows."""
    recs = engine.arcana(char)
    titles = " ".join(r.title for r in recs)

    assert "Primal Vigor" in titles, "should report the equipped set"
    assert "Enhance" not in titles, "enhancement belongs to enhancement_upgrades"


def test_lone_set_piece_is_flagged(char, engine):
    """The Compass of Magic is the only Magic Armor piece equipped, so it
    contributes no set bonus."""
    recs = engine.arcana(char)
    lone = [r for r in recs if "only piece" in r.title]
    assert lone, "a single-piece set should be flagged"
    assert "Compass of Magic" in lone[0].title


def test_main_stats_never_flagged_as_wasted(char):
    """Fixed main stats cannot be changed, so flagging them is noise."""
    for item in char.items:
        for line in item.main_stats:
            assert not line.wasted


def test_accuracy_summary_is_explicit_about_what_it_is_not(char, engine):
    rec = engine.accuracy_summary(char)
    assert rec.priority in (Priority.CRITICAL, Priority.INFO)
    assert "not your in-game total" in rec.detail.lower()
