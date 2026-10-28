"""The recommendation engine.

Produces a ranked "fix this first" list from a parsed Character. Every
recommendation carries a concrete gain where one is computable, and says
plainly when it is not.

Three kinds of recommendation:

* offline  -- derived from a snapshot alone (manastones, rerolls, skills)
* online   -- needs the client to ask PLAYNC for exact enhancement deltas
* advisory -- a check that reports a fact rather than an action
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import stats as S
from .client import Aion2Client, CharacterRef
from .fetch import fetch_item_at_enchant
from .models import Character, Item, StatLine

log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "thresholds.yaml"


class Priority(IntEnum):
    CRITICAL = 4
    HIGH = 3
    MEDIUM = 2
    LOW = 1
    INFO = 0

    @property
    def label(self) -> str:
        return self.name.title()


@dataclass(slots=True)
class Recommendation:
    category: str
    title: str
    detail: str
    priority: Priority
    score: float = 0.0
    slot_name: str = ""
    item_name: str = ""
    gains: dict[str, float] = field(default_factory=dict)
    losses: dict[str, float] = field(default_factory=dict)
    action: str = ""

    @property
    def gain_summary(self) -> str:
        if not self.gains:
            return ""
        return ", ".join(
            f"+{v:g} {S.display_name(k)}" for k, v in sorted(
                self.gains.items(), key=lambda kv: -abs(kv[1])
            )
        )


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        log.warning("thresholds file not found at %s, using built-in defaults", p)
        return {}
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Engine:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.cfg = config if config is not None else load_config()
        self.weights: dict[str, float] = self.cfg.get("stat_weights") or {}
        wasted = self.cfg.get("wasted_stats")
        self.wasted_ids = frozenset(wasted) if wasted else S.WASTED_DPS_IDS

    # -- scoring -----------------------------------------------------------

    def weight(self, stat_id: str) -> float:
        return float(self.weights.get(stat_id, 0.0))

    def score_gains(self, gains: dict[str, float]) -> float:
        return sum(self.weight(k) * v for k, v in gains.items())

    def is_wasted(self, stat_id: str) -> bool:
        return stat_id in self.wasted_ids

    # -- offline checks ----------------------------------------------------

    def manastone_swaps(self, char: Character) -> list[Recommendation]:
        """Replace manastones carrying wasted stats with the best DPS stone of
        the SAME grade.

        Grouped by (wasted stat, grade) so the output is one actionable batch
        per stone type rather than twenty near-identical lines.
        """
        groups: dict[tuple[str, str], list[tuple[Item, StatLine]]] = {}
        for item in char.items:
            for stone in item.stones:
                if self.is_wasted(stone.stat_id):
                    groups.setdefault((stone.stat_id, stone.grade), []).append(
                        (item, stone)
                    )

        out: list[Recommendation] = []
        for (stat_id, grade), entries in groups.items():
            replacement = self._best_replacement(grade)
            if replacement is None:
                # No evidence of a DPS stone at this grade. Say so rather than
                # inventing one.
                out.append(
                    Recommendation(
                        category="manastone",
                        title=(
                            f"{len(entries)}x {grade} "
                            f"{S.display_name(stat_id)} stone(s) are wasted"
                        ),
                        detail=(
                            "These do nothing for PvE damage, but no DPS "
                            f"manastone has been observed at {grade} grade yet, "
                            "so there is no verified replacement to name."
                        ),
                        priority=Priority.LOW,
                        score=1.0,
                        gains={},
                        losses={stat_id: sum(s.value for _i, s in entries)},
                    )
                )
                continue

            new_id, new_value = replacement
            count = len(entries)
            total_gain = float(new_value * count)
            gains = {new_id: total_gain}
            score = self.score_gains(gains)

            where = ", ".join(
                f"{item.name} socket {stone.slot_pos}" for item, stone in entries[:4]
            )
            if count > 4:
                where += f", and {count - 4} more"

            plural = "s" if count != 1 else ""
            swap_phrase = (
                f"swapping all {count} is worth" if count > 1 else "swapping it is worth"
            )

            out.append(
                Recommendation(
                    category="manastone",
                    title=(
                        f"Replace {count}x {grade} {S.display_name(stat_id)} "
                        f"stone{plural} with {S.display_name(new_id)}"
                    ),
                    detail=(
                        f"{S.display_name(stat_id)} does nothing for PvE damage. "
                        f"A {grade} {S.display_name(new_id)} stone gives "
                        f"+{new_value:g} each, so {swap_phrase} "
                        f"+{total_gain:g} {S.display_name(new_id)}. Found on: {where}."
                    ),
                    priority=Priority.HIGH if score >= 40 else Priority.MEDIUM,
                    score=score,
                    slot_name=entries[0][0].slot_name if count == 1 else "",
                    item_name=entries[0][0].name if count == 1 else f"{count} items",
                    gains=gains,
                    losses={stat_id: sum(s.value for _i, s in entries)},
                    action=(
                        f"Swap {count}x {grade} {S.display_name(stat_id)} manastone{plural} "
                        f"for {grade} {S.display_name(new_id)}."
                    ),
                )
            )
        return out

    def _best_replacement(self, grade: str) -> tuple[str, int] | None:
        """Highest-weighted recommendable DPS stone at exactly `grade`.

        Deliberately does NOT fall back to another grade: suggesting a Unique
        replacement for a Common stone is not actionable advice.
        """
        best: tuple[float, str, int] | None = None
        for stat_id in S.RECOMMENDABLE_STONES:
            if self.weight(stat_id) <= 0:
                continue
            value = S.stone_value(stat_id, grade)
            if value is None:
                continue
            cand = (self.weight(stat_id) * value, stat_id, value)
            if best is None or cand[0] > best[0]:
                best = cand
        if best is None:
            return None
        _score, stat_id, value = best
        return stat_id, value

    def empty_stone_slots(self, char: Character) -> list[Recommendation]:
        out = []
        for item in char.items:
            empty = item.empty_stone_slots
            if empty <= 0:
                continue
            rep = self._best_replacement("Legend")
            gains: dict[str, float] = {}
            detail = f"{item.name} has {empty} empty manastone slot(s)."
            if rep:
                stat_id, value = rep
                gains = {stat_id: float(value * empty)}
                detail += (
                    f" Filling them with Legend {S.display_name(stat_id)} "
                    f"would add +{value * empty:g}."
                )
            out.append(
                Recommendation(
                    category="manastone",
                    title=f"{empty} empty manastone slot(s) on {item.name}",
                    detail=detail,
                    priority=Priority.HIGH,
                    score=self.score_gains(gains) if gains else 10.0,
                    slot_name=item.slot_name,
                    item_name=item.name,
                    gains=gains,
                    action=f"Socket {empty} manastone(s) into {item.name}.",
                )
            )
        return out

    def reroll_candidates(self, char: Character) -> list[Recommendation]:
        """Rolled sub-stat lines carrying wasted stats.

        PLAYNC does not publish sub-stat ranges, so we cannot say whether a
        given roll is good within its range -- only that the stat itself is
        useless for this build.
        """
        out = []
        for item in char.items:
            bad = [ln for ln in item.sub_stats if self.is_wasted(ln.stat_id)]
            if not bad:
                continue
            total = sum(ln.value for ln in bad)
            out.append(
                Recommendation(
                    category="reroll",
                    title=f"{len(bad)} wasted sub-stat line(s) on {item.name}",
                    detail=(
                        f"{', '.join(f'{ln.label} {ln.value:g}' for ln in bad)}. "
                        f"{total:g} points of stat budget doing nothing for PvE "
                        f"damage. Sub-stat ranges are not published, so this "
                        f"flags the stat, not the quality of the roll."
                    ),
                    priority=Priority.MEDIUM if total >= 100 else Priority.LOW,
                    score=total * 0.05,
                    slot_name=item.slot_name,
                    item_name=item.name,
                    losses={ln.stat_id: ln.value for ln in bad},
                    action=f"Reroll the flagged sub-stats on {item.name}.",
                )
            )
        return out

    def arcana(self, char: Character) -> list[Recommendation]:
        out = []
        for item in char.items:
            if not item.is_arcana:
                continue
            if item.enchant_level < item.max_enchant_level:
                headroom = item.max_enchant_level - item.enchant_level
                out.append(
                    Recommendation(
                        category="arcana",
                        title=f"Enhance {item.name} (+{item.enchant_level})",
                        detail=(
                            f"{item.name} is at +{item.enchant_level} of a possible "
                            f"+{item.max_enchant_level}. Each level adds 1 to its "
                            f"god stat, and Arcana enhancement is cheap relative "
                            f"to gear."
                        ),
                        priority=Priority.MEDIUM if headroom >= 3 else Priority.LOW,
                        score=headroom * 3.0,
                        slot_name=item.slot_name,
                        item_name=item.name,
                        action=f"Enhance {item.name} to +{item.max_enchant_level}.",
                    )
                )
        return out

    def skill_targets(self, char: Character) -> list[Recommendation]:
        out = []
        targets: dict[str, int] = self.cfg.get("skill_targets") or {}
        for name, want in targets.items():
            skill = char.skill(name)
            if skill is None:
                out.append(
                    Recommendation(
                        category="skill",
                        title=f"Skill not found: {name}",
                        detail=(
                            f"{name} is in the target list but not in this "
                            f"character's skill list. Check the spelling against "
                            f"the in-game name."
                        ),
                        priority=Priority.INFO,
                        action="",
                    )
                )
                continue
            if skill.effective_level < want:
                short = want - skill.effective_level
                out.append(
                    Recommendation(
                        category="skill",
                        title=f"{name} is {short} level(s) below target",
                        detail=(
                            f"Effective level {skill.effective_level} "
                            f"(listed {skill.base_level} + Daevanion "
                            f"{skill.daevanion_bonus} + Arcana {skill.arcana_bonus}) "
                            f"against a target of {want}."
                        ),
                        priority=Priority.HIGH,
                        score=short * 4.0,
                        action=f"Raise {name} to level {want}.",
                    )
                )
        return out

    def attribute_checks(self, char: Character) -> list[Recommendation]:
        out = []
        attrs = self.cfg.get("attributes") or {}
        if attrs.get("flag_dexterity_above_precision", True):
            dex = char.attributes.get("DEX", 0.0)
            agi = char.attributes.get("AGI", 0.0)
            if dex > agi:
                out.append(
                    Recommendation(
                        category="attributes",
                        title=f"Dexterity ({dex:g}) exceeds Precision ({agi:g})",
                        detail=(
                            "Precision drives Accuracy and Critical Hit. Dexterity "
                            "drives Evasion, Block and Critical Hit Resist, none of "
                            "which help PvE damage. Favour Precision when choosing "
                            "attribute sources."
                        ),
                        priority=Priority.MEDIUM,
                        score=(dex - agi) * 1.5,
                        action="Shift attribute investment from Dexterity to Precision.",
                    )
                )
        return out

    def accuracy_summary(self, char: Character) -> Recommendation:
        acc_cfg = self.cfg.get("accuracy") or {}
        gear = char.gear_accuracy
        warn = float(acc_cfg.get("gear_warn_below", 0))
        headroom = self._accuracy_headroom(char)

        detail = (
            f"{gear:g} flat Accuracy is readable from gear. This is NOT your "
            f"in-game total: the class base value by level and any active buffs "
            f"are not published by PLAYNC."
        )
        if headroom > 0:
            detail += (
                f" Swapping every wasted manastone to Accuracy would add roughly "
                f"+{headroom:g} more."
            )
        return Recommendation(
            category="accuracy",
            title=f"Gear Accuracy: {gear:g}",
            detail=detail,
            priority=Priority.CRITICAL if gear < warn else Priority.INFO,
            score=0.0,
            gains={"Accuracy": headroom} if headroom else {},
        )

    def _accuracy_headroom(self, char: Character) -> float:
        """Accuracy gained if every wasted manastone became an Accuracy stone
        of the SAME grade. Stones with no Accuracy equivalent at their grade
        contribute nothing, so this stays an honest floor rather than a
        best case."""
        total = 0.0
        for item in char.items:
            for stone in item.stones:
                if not self.is_wasted(stone.stat_id):
                    continue
                value = S.stone_value("Accuracy", stone.grade)
                if value:
                    total += value
        return total

    # -- online check ------------------------------------------------------

    def enhancement_upgrades(
        self,
        char: Character,
        client: Aion2Client,
        ref: CharacterRef,
        *,
        max_levels_ahead: int | None = None,
    ) -> list[Recommendation]:
        """Ask PLAYNC what each item looks like a few enhancement levels up.

        Costs one request per item per level probed, so it is rate limited and
        deliberately bounded.
        """
        enh = self.cfg.get("enhancement") or {}
        ahead = max_levels_ahead or int(enh.get("max_levels_ahead", 4))
        min_gain = float(enh.get("min_weighted_gain", 5.0))

        out: list[Recommendation] = []
        for item in char.items:
            if item.enchant_level >= item.max_enchant_level:
                continue
            target = min(item.enchant_level + ahead, item.max_enchant_level)
            try:
                upgraded = fetch_item_at_enchant(client, ref, item, target)
            except Exception as exc:  # noqa: BLE001 - report, do not abort the run
                log.warning("enhancement probe failed for %s: %s", item.name, exc)
                continue

            gains: dict[str, float] = {}
            for new in upgraded.main_stats:
                old = next(
                    (m.value for m in item.main_stats if m.stat_id == new.stat_id), 0.0
                )
                delta = new.value - old
                if delta > 0:
                    gains[new.stat_id] = delta
            if not gains:
                continue

            score = self.score_gains(gains)
            if score < min_gain:
                continue

            out.append(
                Recommendation(
                    category="enhancement",
                    title=f"Enhance {item.name} from +{item.enchant_level} to +{target}",
                    detail=(
                        "Exact values from PLAYNC, not an estimate: "
                        + ", ".join(
                            f"{S.display_name(k)} +{v:g}" for k, v in gains.items()
                        )
                        + f". Item caps at +{item.max_enchant_level}."
                    ),
                    priority=Priority.HIGH if score >= 60 else Priority.MEDIUM,
                    score=score,
                    slot_name=item.slot_name,
                    item_name=item.name,
                    gains=gains,
                    action=f"Enhance {item.name} to +{target}.",
                )
            )
        return out

    # -- entry point -------------------------------------------------------

    def audit(
        self,
        char: Character,
        client: Aion2Client | None = None,
        ref: CharacterRef | None = None,
    ) -> list[Recommendation]:
        """Full ranked audit. Pass client+ref to include exact enhancement
        deltas, which costs extra requests."""
        recs: list[Recommendation] = [self.accuracy_summary(char)]
        recs += self.empty_stone_slots(char)
        recs += self.manastone_swaps(char)
        recs += self.skill_targets(char)
        recs += self.arcana(char)
        recs += self.reroll_candidates(char)
        recs += self.attribute_checks(char)

        if client is not None and ref is not None:
            recs += self.enhancement_upgrades(char, client, ref)

        return rank(recs)


def rank(recs: Iterable[Recommendation]) -> list[Recommendation]:
    """Highest priority first, then by weighted score."""
    return sorted(recs, key=lambda r: (-int(r.priority), -r.score, r.title))
