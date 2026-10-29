"""Compare a character against a reference build.

The recommendation engine answers "what should I fix next". This answers a
different question: "what does a better version of my character look like,
and which of the differences actually matter".

It is deliberately dimension-by-dimension rather than a single score. Two
Rangers can differ in enhancement, manastone quality, soul binding, skills
and Daevanion progress all at once, and lumping those into one number hides
which one is worth attacking first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from . import stats as S
from .models import Character, Item
from .recommend import Engine


@dataclass(slots=True)
class Gap:
    """One dimension on which two builds differ."""

    dimension: str
    mine: float
    theirs: float
    unit: str = ""
    #: True when a HIGHER number is better.
    higher_is_better: bool = True
    #: An outcome rather than a lever. Combat Power is the RESULT of the
    #: other dimensions, so listing it among "biggest gaps to attack" is
    #: circular -- you close it by closing the others.
    outcome: bool = False
    #: Decimal places when formatting.
    places: int = 0
    note: str = ""

    @property
    def delta(self) -> float:
        return self.theirs - self.mine

    @property
    def behind(self) -> bool:
        return self.delta > 0 if self.higher_is_better else self.delta < 0

    @property
    def ratio(self) -> float:
        """How far behind as a fraction, 0.0 when level or ahead."""
        if not self.behind:
            return 0.0
        base = max(abs(self.theirs), abs(self.mine), 1.0)
        return abs(self.delta) / base

    def format(self, value: float) -> str:
        if self.unit == "%":
            return f"{value:.1f}%"
        if self.places:
            return f"{value:,.{self.places}f}"
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:g}"


@dataclass(slots=True)
class Comparison:
    mine: Character
    theirs: Character
    gaps: list[Gap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def behind_on(self) -> list[Gap]:
        """Levers you are behind on, worst first. Outcomes are excluded:
        Combat Power is what closing the others produces."""
        return sorted(
            (g for g in self.gaps if g.behind and not g.outcome),
            key=lambda g: -g.ratio,
        )

    @property
    def ahead_on(self) -> list[Gap]:
        return [g for g in self.gaps if not g.behind and g.delta != 0]


def _avg(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def _stone_quality(char: Character) -> float:
    """Mean manastone grade, 0 Common to 3 Unique.

    A build with 23 Unique stones and one with 33 Rare can carry the same
    stone COUNT while differing enormously in what those stones give.
    """
    ranks = [
        S.grade_rank(st.grade)
        for it in char.items
        for st in it.stones
        if S.grade_rank(st.grade) >= 0
    ]
    return _avg(ranks)


def _enhancement_done(char: Character) -> float:
    """Percent of total enhancement capacity actually used."""
    cap = sum(it.max_enchant_level for it in char.items)
    have = sum(it.enchant_level for it in char.items)
    return 100.0 * have / cap if cap else 0.0


def compare(mine: Character, theirs: Character, engine: Engine) -> Comparison:
    c = Comparison(mine=mine, theirs=theirs)

    def add(dimension, a, b, unit="", higher=True, note="", outcome=False, places=0):
        c.gaps.append(
            Gap(dimension, float(a), float(b), unit, higher, outcome, places, note)
        )

    add("Combat Power", mine.combat_power, theirs.combat_power, outcome=True)
    add("Item Level", mine.item_level, theirs.item_level, outcome=True)

    add(
        "Enhancement completion",
        _enhancement_done(mine), _enhancement_done(theirs), "%",
        note="Share of every item's enchant cap actually reached.",
    )
    add(
        "Soul binding",
        _avg(it.soul_bind_rate for it in mine.items),
        _avg(it.soul_bind_rate for it in theirs.items),
        "%",
        note="Mean across all equipped items.",
    )
    add(
        "Manastone grade",
        _stone_quality(mine), _stone_quality(theirs), places=2,
        note="Mean grade, 0 Common to 3 Unique.",
    )

    mine_boards = sum(b.open_nodes for b in mine.boards)
    theirs_boards = sum(b.open_nodes for b in theirs.boards)
    add("Daevanion nodes", mine_boards, theirs_boards)

    # Lower is better for these two.
    add(
        "Wasted stat lines",
        len(mine.wasted_lines), len(theirs.wasted_lines),
        higher=False,
        note="Rolled stats and stones doing nothing for the build.",
    )
    wasted_stones = lambda ch: sum(  # noqa: E731
        1 for it in ch.items for st in it.stones if engine.is_wasted(st.stat_id)
    )
    add(
        "Wasted manastones",
        wasted_stones(mine), wasted_stones(theirs), higher=False,
    )

    # -- narrative notes -----------------------------------------------
    shared, upgrades = _gear_overlap(mine, theirs)
    if upgrades:
        names = ", ".join(sorted({it.name for it in upgrades})[:3])
        c.notes.append(
            f"{len(upgrades)} of their slots hold a different item to yours, "
            f"including {names}."
        )
    if shared:
        c.notes.append(
            f"{len(shared)} slots use the same item as you, so those "
            f"differences are enhancement and rolls rather than gear."
        )

    return c


def _gear_overlap(mine: Character, theirs: Character) -> tuple[list[Item], list[Item]]:
    """(slots with the same item, slots where theirs differs)."""
    by_slot = {it.slot_pos: it for it in mine.items}
    same: list[Item] = []
    different: list[Item] = []
    for it in theirs.items:
        other = by_slot.get(it.slot_pos)
        if other is None:
            continue
        (same if other.item_id == it.item_id else different).append(it)
    return same, different


#: How many slots must change at once before a snapshot-to-snapshot
#: difference looks more like a preset switch than like progression.
PRESET_SWITCH_SLOTS = 5


@dataclass(slots=True)
class PresetChange:
    """A snapshot pair that looks like a preset switch rather than progress."""

    slots_changed: int
    slots_total: int
    cp_delta: int
    changed_slots: list[str] = field(default_factory=list)

    @property
    def likely(self) -> bool:
        return self.slots_changed >= PRESET_SWITCH_SLOTS


def detect_preset_change(before: Character, after: Character) -> PresetChange:
    """Spot a wholesale gear swap between two snapshots.

    PLAYNC exposes only the ACTIVE preset, with no way to ask for another and
    no field saying which one is live. A character with a PvE and a PvP
    preset therefore produces two very different snapshots depending on what
    was equipped when the capture ran.

    Replacing five items between two snapshots is possible but unusual;
    switching a preset does it instantly. This reports the signal and lets
    the reader judge, rather than asserting which happened.
    """
    before_by_slot = {it.slot_pos: it.item_id for it in before.items}
    changed: list[str] = []
    for it in after.items:
        prev = before_by_slot.get(it.slot_pos)
        if prev is not None and prev != it.item_id:
            changed.append(it.slot_name)

    return PresetChange(
        slots_changed=len(changed),
        slots_total=max(len(after.items), len(before.items)),
        cp_delta=after.combat_power - before.combat_power,
        changed_slots=changed,
    )


def skill_gaps(
    mine: Character, theirs: Character, engine: Engine
) -> list[tuple[str, int, int]]:
    """Key skills where the reference build is ahead, as (name, mine, theirs)."""
    theirs_by_name = {s.name: s for s in theirs.skills}
    out = []
    for s in engine.key_skills(mine):
        other = theirs_by_name.get(s.name)
        if other and other.displayed_level > s.displayed_level:
            out.append((s.name, s.displayed_level, other.displayed_level))
    return sorted(out, key=lambda row: row[1] - row[2])
