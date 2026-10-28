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
from .models import SKILL_LEVEL_CAP, Character, Item, Skill, StatLine
from .paths import candidate_config_dirs, find_config_file

log = logging.getLogger(__name__)

class ConfigNotFound(RuntimeError):
    """Raised when thresholds.yaml cannot be located.

    Deliberately fatal. An empty config silently disables every stat weight,
    which downgrades recommendations without any visible error -- exactly the
    bug that shipped to the container on the first build.
    """


def _plural(n: float, word: str, suffix: str = "s") -> str:
    """'1 level' / '8 levels'. The old '8 level(s)' read like a form field."""
    return word if n == 1 else word + suffix


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
    def sorted_gains(self) -> list[tuple[str, float]]:
        """Biggest first. Dict order is insertion order, which put '+4 Combat
        Speed' ahead of '+400 Penetration' in the UI."""
        return sorted(self.gains.items(), key=lambda kv: -abs(kv[1]))

    @property
    def gain_summary(self) -> str:
        if not self.gains:
            return ""
        return ", ".join(
            f"+{v:g} {S.display_name(k)}" for k, v in self.sorted_gains
        )


def load_config(path: str | Path | None = None, *, strict: bool = True) -> dict[str, Any]:
    """Load thresholds.yaml.

    Raises ConfigNotFound by default rather than returning an empty dict,
    because a silently empty config produces plausible-looking but badly
    ranked recommendations.
    """
    if path:
        p = Path(path)
        if not p.exists():
            if strict:
                raise ConfigNotFound(f"thresholds file not found: {p}")
            log.warning("thresholds file not found at %s", p)
            return {}
    else:
        found = find_config_file("thresholds.yaml")
        if found is None:
            searched = "\n  ".join(str(d) for d in candidate_config_dirs())
            message = (
                "Could not find config/thresholds.yaml. Searched:\n  "
                f"{searched}\n"
                "Set AION2_THRESHOLDS to the file, or AION2_CONFIG_DIR to its "
                "directory."
            )
            if strict:
                raise ConfigNotFound(message)
            log.warning(message)
            return {}
        p = found

    with p.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}

    if not loaded:
        log.warning("thresholds file %s is empty", p)
    else:
        log.info("loaded thresholds from %s", p)
    return loaded


class UnknownProfile(ValueError):
    pass


class Engine:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        profile: str | None = None,
    ) -> None:
        self.cfg = config if config is not None else load_config()

        profiles: dict[str, Any] = self.cfg.get("profiles") or {}
        requested = profile or self.cfg.get("default_profile") or ""

        if requested and profiles:
            if requested not in profiles:
                raise UnknownProfile(
                    f"Unknown profile {requested!r}. Available: "
                    f"{', '.join(sorted(profiles))}"
                )
            chosen = profiles[requested]
            self.profile = requested
            self.profile_label = chosen.get("label", requested)
            self.weights = dict(chosen.get("stat_weights") or {})
        else:
            # Backward compatibility with a flat top-level stat_weights block.
            self.profile = requested or "custom"
            self.profile_label = self.profile
            self.weights = dict(self.cfg.get("stat_weights") or {})

        wasted = self.cfg.get("wasted_stats")
        self.wasted_ids = frozenset(wasted) if wasted else S.WASTED_DPS_IDS

        if not self.weights:
            # Without weights every gain scores zero, so manastone swaps and
            # enhancement upgrades are silently dropped or demoted. Say so.
            log.error(
                "No stat_weights loaded. Recommendations will be unranked and "
                "manastone swaps suppressed. Check that config/thresholds.yaml "
                "is readable."
            )
        else:
            log.info(
                "profile %r with %d stat weights", self.profile, len(self.weights)
            )

    @property
    def has_config(self) -> bool:
        return bool(self.weights)

    @staticmethod
    def available_profiles(config: dict[str, Any] | None = None) -> dict[str, str]:
        cfg = config if config is not None else load_config()
        return {
            name: body.get("label", name)
            for name, body in (cfg.get("profiles") or {}).items()
        }

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

            out.append(
                Recommendation(
                    category="manastone",
                    title=(
                        f"Swap {count}× {grade} {S.display_name(stat_id)} "
                        f"→ {S.display_name(new_id)}"
                    ),
                    detail=(
                        f"{S.display_name(stat_id)} does nothing for damage. "
                        f"On {where}."
                    ),
                    priority=Priority.HIGH if score >= 40 else Priority.MEDIUM,
                    score=score,
                    slot_name=entries[0][0].slot_name if count == 1 else "",
                    item_name=entries[0][0].name if count == 1 else f"{count} items",
                    gains=gains,
                    losses={stat_id: sum(s.value for _i, s in entries)},
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
            detail = "Unsocketed slots are free stats going unused."
            if rep:
                stat_id, value = rep
                gains = {stat_id: float(value * empty)}
                detail = (
                    f"Free stats going unused. Legend "
                    f"{S.display_name(stat_id)} stones are the best fit."
                )
            out.append(
                Recommendation(
                    category="manastone",
                    title=(
                        f"Socket {empty} empty manastone "
                        f"{_plural(empty, 'slot')} on {item.name}"
                    ),
                    detail=detail,
                    priority=Priority.HIGH,
                    score=self.score_gains(gains) if gains else 10.0,
                    slot_name=item.slot_name,
                    item_name=item.name,
                    gains=gains,
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
                    title=(
                        f"Reroll {len(bad)} wasted sub-"
                        f"{_plural(len(bad), 'stat')} on {item.name}"
                    ),
                    detail=(
                        f"{', '.join(f'{ln.label} {ln.value:g}' for ln in bad)} "
                        f"— {total:g} points doing nothing."
                    ),
                    priority=Priority.MEDIUM if total >= 100 else Priority.LOW,
                    score=total * 0.05,
                    slot_name=item.slot_name,
                    item_name=item.name,
                    losses={ln.stat_id: ln.value for ln in bad},
                )
            )
        return out

    def arcana(self, char: Character) -> list[Recommendation]:
        """Arcana set completion.

        Per-item Arcana enhancement is NOT reported here -- enhancement_upgrades
        already covers every item including Arcana, and emitting both produced
        duplicate rows.
        """
        arcana = [i for i in char.items if i.is_arcana]
        if not arcana:
            return []

        out: list[Recommendation] = []
        sets: dict[str, int] = {}
        for item in arcana:
            if item.set_name:
                sets[item.set_name] = max(
                    sets.get(item.set_name, 0), item.set_equipped
                )

        # An Arcana contributing to no set, or to a set with only one piece
        # equipped, is giving up the set bonus.
        for item in arcana:
            if item.set_name and sets.get(item.set_name, 0) <= 1:
                out.append(
                    Recommendation(
                        category="arcana",
                        title=(
                            f"{item.name} is the only piece of its "
                            f"{item.set_name} set"
                        ),
                        detail=(
                            f"A lone set piece gives no set bonus. Either build "
                            f"toward {item.set_name} or replace it with a piece "
                            f"of a set you already run."
                        ),
                        priority=Priority.LOW,
                        score=5.0,
                        slot_name=item.slot_name,
                        item_name=item.name,
                        action=f"Reconsider {item.name} in slot {item.slot_pos}.",
                    )
                )

        for set_name, count in sorted(sets.items()):
            if count > 1:
                out.append(
                    Recommendation(
                        category="arcana",
                        title=f"{set_name} set: {count} pieces equipped",
                        detail=(
                            f"{count} of 5 slots are contributing to {set_name}."
                        ),
                        priority=Priority.INFO,
                        score=0.0,
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

    def skill_levels(self, char: Character) -> list[Recommendation]:
        """Every in-use skill below the level cap.

        Distinct from skill_targets, which only checks the handful named in
        the thresholds file. This catches the rest, which is what you actually
        want when the question is "get my skills to 20".
        """
        cfg = self.cfg.get("skills") or {}
        cap = int(cfg.get("level_cap", SKILL_LEVEL_CAP))
        min_gap = int(cfg.get("min_gap_to_report", 1))

        out: list[Recommendation] = []
        key_names = {s.name for s in self.key_skills(char)}

        # --- key skills first, named individually -------------------------
        # A level spent on Explosion Trap is a level not spent on Deadshot,
        # so the skills that matter get called out rather than averaged into
        # a category total.
        key_below = [
            s for s in self.key_skills(char)
            if s.in_use and (cap - s.base_level) >= min_gap
        ]
        if key_below:
            key_below.sort(key=lambda s: (cap - s.base_level), reverse=True)
            listing = ", ".join(
                f"{s.name} {s.base_level}/{cap}"
                + (f" (+{s.daevanion_bonus + s.gear_bonus} from gear and boards)"
                   if s.daevanion_bonus or s.gear_bonus else "")
                for s in key_below[:3]
            )
            if len(key_below) > 3:
                listing += f", +{len(key_below) - 3} more"
            total = sum(cap - s.base_level for s in key_below)
            out.append(
                Recommendation(
                    category="skill",
                    title=(
                        f"{total} {_plural(total, 'level')} to train on "
                        f"{len(key_below)} key {_plural(len(key_below), 'skill')}"
                    ),
                    detail=f"{listing}.",
                    priority=Priority.HIGH,
                    score=float(total) * 4.0,
                )
            )

        # --- everything else, summarised by category ----------------------
        for category, label in (("Active", "active"), ("Dp", "stigma"),
                                ("Passive", "passive")):
            below = [
                s for s in char.skills
                if s.category == category and s.in_use
                and s.name not in key_names
                and (cap - s.base_level) >= min_gap
            ]
            if not below:
                continue
            below.sort(key=lambda s: s.base_level)

            total_levels = sum(cap - s.base_level for s in below)
            listing = ", ".join(f"{s.name} {s.base_level}/{cap}" for s in below[:3])
            if len(below) > 3:
                listing += f", +{len(below) - 3} more"

            out.append(
                Recommendation(
                    category="skill",
                    title=(
                        f"{total_levels} {_plural(total_levels, 'level')} on "
                        f"other {label} skills"
                    ),
                    detail=f"{listing}. Lower priority than the key skills above.",
                    priority=Priority.LOW,
                    score=float(total_levels) * (
                        0.8 if category in ("Active", "Dp") else 0.4
                    ),
                )
            )

        # --- stigmas that are learned but not slotted ---------------------
        idle = [
            s for s in char.skills
            if s.is_stigma and s.acquired and not s.equipped
        ]
        if idle:
            out.append(
                Recommendation(
                    category="skill",
                    title=f"{len(idle)} learned {_plural(len(idle), 'stigma')} not slotted",
                    detail=(
                        f"{', '.join(s.name for s in idle)}. "
                        f"Learned but contributing nothing while unequipped."
                    ),
                    priority=Priority.MEDIUM,
                    score=float(len(idle)) * 8.0,
                )
            )

        # --- stigmas never learned ----------------------------------------
        unlearned = [s for s in char.skills if s.is_stigma and not s.acquired]
        if unlearned:
            out.append(
                Recommendation(
                    category="skill",
                    title=f"{len(unlearned)} {_plural(len(unlearned), 'stigma')} not yet learned",
                    detail=", ".join(s.name for s in unlearned) + ".",
                    priority=Priority.LOW,
                    score=float(len(unlearned)) * 2.0,
                )
            )

        return out

    def key_skills(self, char: Character) -> list[Skill]:
        """The skills worth spending gear slots on, in configured order."""
        cfg = self.cfg.get("skills") or {}
        names = list(cfg.get("key_active") or []) + list(cfg.get("key_passive") or [])
        by_name = {s.name: s for s in char.skills}
        return [by_name[n] for n in names if n in by_name]

    def skill_slots(self, char: Character) -> list[Recommendation]:
        """Unused gear skill slots, matched against key skills below cap.

        Equipment carries `subSkills` worth +N to a named skill, with
        `subSkillCountMax` as the capacity. This is not Arcana-only: rings,
        cloak, pauldrons, breastplate and necklace all carry them, and weapons
        and some earrings hold five.

        An empty slot is a free skill level going unused.
        """
        cfg = self.cfg.get("skill_slots") or {}
        min_empty = int(cfg.get("min_empty_to_report", 1))
        cap = int((self.cfg.get("skills") or {}).get("level_cap", SKILL_LEVEL_CAP))

        with_space = [
            it for it in char.items if it.empty_skill_slots >= min_empty
        ]
        if not with_space:
            return []

        total_empty = sum(it.empty_skill_slots for it in with_space)

        # Which key skills would most benefit from a gear grant.
        #
        # Measured on TRAINED level, consistently with skill_levels. A gear
        # grant and a trained level are interchangeable ways to close the same
        # gap, so judging this on effective_level contradicted the sweep --
        # one row claimed every key skill was capped while the next listed
        # nine that were not.
        wanted = [s for s in self.key_skills(char) if s.base_level < cap]
        wanted.sort(key=lambda s: s.base_level)

        out: list[Recommendation] = []

        if wanted:
            targets = ", ".join(
                f"{s.name} {s.base_level}/{cap}"
                + (f", already +{s.gear_bonus} from gear" if s.gear_bonus else "")
                for s in wanted[:3]
            )
            detail = f"Key skills short of {cap}: {targets}."
        else:
            detail = (
                f"Every key skill is already trained to {cap}, so spend these "
                "on whatever your rotation leans on hardest."
            )

        out.append(
            Recommendation(
                category="skillslot",
                title=(
                    f"{total_empty} empty gear skill "
                    f"{_plural(total_empty, 'slot')} across "
                    f"{len(with_space)} {_plural(len(with_space), 'item')}"
                ),
                detail=detail,
                priority=Priority.HIGH if total_empty >= 10 else Priority.MEDIUM,
                score=float(total_empty) * 6.0,
            )
        )

        # Call out the roomiest items individually; those are the easy wins.
        biggest = sorted(
            with_space, key=lambda it: -it.empty_skill_slots
        )[:4]
        for item in biggest:
            if item.empty_skill_slots < 3:
                continue
            have = ", ".join(f"{n} +{lv}" for n, lv in item.sub_skills) or "nothing"
            suggest = ", ".join(s.name for s in wanted[:2]) if wanted else ""
            detail = (
                f"{item.empty_skill_slots} of {item.sub_skill_slots} slots unused. "
                f"Currently grants {have}."
            )
            if suggest:
                detail += f" Good candidates: {suggest}."
            out.append(
                Recommendation(
                    category="skillslot",
                    title=(
                        f"{item.name} ({item.slot_name}) has "
                        f"{item.empty_skill_slots} free skill "
                        f"{_plural(item.empty_skill_slots, 'slot')}"
                    ),
                    detail=detail,
                    priority=Priority.MEDIUM,
                    score=float(item.empty_skill_slots) * 4.0,
                    slot_name=item.slot_name,
                    item_name=item.name,
                )
            )
        return out

    def soul_binding(self, char: Character) -> list[Recommendation]:
        """Items well short of fully soul bound.

        What soul binding does mechanically is not published, and there is no
        way to ask PLAYNC "what would this item look like at 100". So this
        reports completion, like Daevanion progress, and deliberately claims
        no stat gain.
        """
        cfg = self.cfg.get("soul_binding") or {}
        threshold = float(cfg.get("flag_below_percent", 90))

        low = [
            it for it in char.items
            if it.soul_bind_rate < threshold
        ]
        if not low:
            return []

        low.sort(key=lambda it: it.soul_bind_rate)
        out: list[Recommendation] = []

        # The weapon matters most, so call it out on its own.
        weapon = next((it for it in low if it.slot_pos == 1), None)
        if weapon:
            out.append(
                Recommendation(
                    category="soulbind",
                    title=(
                        f"{weapon.name} is only "
                        f"{weapon.soul_bind_rate:g}% soul bound"
                    ),
                    detail=(
                        "Your weapon is the highest-leverage slot to finish "
                        "binding."
                    ),
                    priority=Priority.HIGH,
                    score=(100.0 - weapon.soul_bind_rate) * 0.9,
                    slot_name=weapon.slot_name,
                    item_name=weapon.name,
                )
            )

        rest = [it for it in low if it.slot_pos != 1]
        if rest:
            avg = sum(it.soul_bind_rate for it in rest) / len(rest)
            listing = ", ".join(
                f"{it.name} {it.soul_bind_rate:g}%" for it in rest[:4]
            )
            if len(rest) > 4:
                listing += f", +{len(rest) - 4} more"
            out.append(
                Recommendation(
                    category="soulbind",
                    title=(
                        f"{len(rest)} other {_plural(len(rest), 'item')} "
                        f"below {threshold:g}% soul bound"
                    ),
                    detail=f"Lowest first: {listing}. Average {avg:.0f}%.",
                    priority=Priority.MEDIUM,
                    score=(100.0 - avg) * 0.4,
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

        # Only escalate when the active profile actually cares about Accuracy.
        # Under a Combat Power goal this is context, not a call to action.
        accuracy_weight = max(
            self.weight("WeaponAccuracy"), self.weight("Accuracy")
        )
        urgent = gear < warn and accuracy_weight >= 3.0

        return Recommendation(
            category="accuracy",
            title=f"Gear Accuracy: {gear:g}",
            detail=detail,
            priority=Priority.CRITICAL if urgent else Priority.INFO,
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

    def enhancement_upgrades(self, char: Character) -> list[Recommendation]:
        """Rank enhancement using the headroom recorded at snapshot time.

        Values are exact, having come from PLAYNC recalculating each item at
        its cap. No network needed here.
        """
        if not char.has_enhancement_data:
            return []

        enh = self.cfg.get("enhancement") or {}
        min_gain = float(enh.get("min_weighted_gain", 5.0))

        out: list[Recommendation] = []
        for slot_str, gains in char.enhancement_headroom.items():
            item = char.item_at(int(slot_str))
            if item is None or not gains:
                continue

            score = self.score_gains(gains)
            if score < min_gain:
                continue

            levels = item.max_enchant_level - item.enchant_level

            # An item sitting far below its cap is both a bigger win and
            # usually cheaper per level, so weight those up.
            if score >= 400:
                prio = Priority.CRITICAL
            elif score >= 150:
                prio = Priority.HIGH
            elif score >= 40:
                prio = Priority.MEDIUM
            else:
                prio = Priority.LOW

            # Title carries the action; `gains` carries the numbers. Detail
            # adds only what neither already says -- the three used to repeat
            # each other, which is most of why the page was eight screens long.
            out.append(
                Recommendation(
                    category="enhancement",
                    title=(
                        f"Enhance {item.name} ({item.slot_name}) "
                        f"+{item.enchant_level} → +{item.max_enchant_level}"
                    ),
                    detail=f"{levels} {_plural(levels, 'level')} of headroom.",
                    priority=prio,
                    score=score,
                    slot_name=item.slot_name,
                    item_name=item.name,
                    gains=dict(gains),
                )
            )
        return out

    def enhancement_summary(self, char: Character) -> Recommendation | None:
        """One headline row for total enhancement headroom."""
        if not char.has_enhancement_data:
            return None
        totals = char.total_enhancement_gain()
        if not totals:
            return None

        top = ", ".join(
            f"{S.display_name(k)} +{v:g}"
            for k, v in sorted(totals.items(), key=lambda kv: -kv[1])[:4]
        )
        return Recommendation(
            category="enhancement",
            title=(
                f"{char.enhancement_levels_available} enhancement levels "
                f"available across your gear"
            ),
            detail=(
                f"Taking every item to its cap is worth {top}. These are exact "
                f"values from PLAYNC. Enhancement is usually the largest single "
                f"lever on Combat Power."
            ),
            priority=Priority.INFO,
            score=0.0,
            gains=dict(totals),
        )

    # -- entry point -------------------------------------------------------

    def audit(self, char: Character) -> list[Recommendation]:
        """Full ranked audit. Entirely offline: enhancement figures come from
        the headroom recorded at snapshot time."""
        recs: list[Recommendation] = []

        summary = self.enhancement_summary(char)
        if summary:
            recs.append(summary)
        recs.append(self.accuracy_summary(char))

        recs += self.enhancement_upgrades(char)
        recs += self.skill_levels(char)
        recs += self.skill_slots(char)
        recs += self.soul_binding(char)
        recs += self.empty_stone_slots(char)
        recs += self.manastone_swaps(char)
        recs += self.skill_targets(char)
        recs += self.arcana(char)
        recs += self.reroll_candidates(char)
        recs += self.attribute_checks(char)

        return rank(recs)


def rank(recs: Iterable[Recommendation]) -> list[Recommendation]:
    """Highest priority first, then by weighted score."""
    return sorted(recs, key=lambda r: (-int(r.priority), -r.score, r.title))
