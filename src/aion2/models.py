"""Typed view over PLAYNC's JSON.

Parsing rules that matter, all learned the hard way in Phase 0:

* Every stat array on an item is OPTIONAL and is omitted rather than returned
  empty. Belt/Amulet/Runes carry only `mainStats`; Arcana carry `mainStats`
  plus `subSkills` plus `set`.
* An item's displayed value is `value + extra`, where `extra` is the
  enhancement bonus and scales with enchant level.
* A skill's real level is its listed level PLUS Daevanion PLUS Arcana.
* Key stats on `id` / `type`. The `name` field is localized and one value
  comes back as untranslated Korean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import stats as S

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _num(value: Any) -> float:
    """First number in a value like '+7', '18%', '226', 226."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM.search(str(value or ""))
    return float(m.group()) if m else 0.0


def _is_percent(value: Any) -> bool:
    return "%" in str(value or "")


def _arr(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Safe accessor. These keys are absent, not empty, on many items."""
    return payload.get(key) or []


# --------------------------------------------------------------------------
# stat lines
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatLine:
    stat_id: str
    name: str
    value: float
    percent: bool = False
    grade: str = ""          # manastones only
    slot_pos: int | None = None
    source: str = ""         # main | sub | stone | godstone

    @property
    def label(self) -> str:
        return S.display_name(self.stat_id, self.name)

    @property
    def wasted(self) -> bool:
        # Fixed main stats cannot be changed, so never flag them.
        return self.source in ("sub", "stone") and S.is_wasted(self.stat_id)

    def __str__(self) -> str:
        v = f"{self.value:g}{'%' if self.percent else ''}"
        return f"{self.label} {v}"


@dataclass(slots=True)
class Item:
    slot_pos: int
    slot_name: str
    item_id: int
    name: str
    grade: str
    enchant_level: int
    max_enchant_level: int
    category: str = ""
    magic_stone_slots: int = 0
    god_stone_slots: int = 0
    #: Percent, 0-100. NOT always 100 -- our Ranger's bow reads 13.3. Two
    #: earlier sample characters happened to be fully bound, which is why
    #: this was first recorded as a constant.
    soul_bind_rate: float = 100.0
    main_stats: list[StatLine] = field(default_factory=list)
    sub_stats: list[StatLine] = field(default_factory=list)
    stones: list[StatLine] = field(default_factory=list)
    god_stones: list[dict[str, Any]] = field(default_factory=list)
    sub_skills: list[tuple[str, int]] = field(default_factory=list)
    set_name: str = ""
    set_equipped: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_arcana(self) -> bool:
        return 41 <= self.slot_pos <= 45

    @property
    def all_lines(self) -> list[StatLine]:
        return [*self.main_stats, *self.sub_stats, *self.stones]

    @property
    def wasted_lines(self) -> list[StatLine]:
        return [ln for ln in self.all_lines if ln.wasted]

    @property
    def empty_stone_slots(self) -> int:
        return max(0, self.magic_stone_slots - len(self.stones))

    def total(self, stat_id: str) -> float:
        """Sum of one stat across every line on this item."""
        return sum(ln.value for ln in self.all_lines if ln.stat_id == stat_id)

    @classmethod
    def parse(cls, payload: dict[str, Any], slot_pos: int, slot_name: str) -> "Item":
        def lines(key: str, source: str) -> list[StatLine]:
            out = []
            for s in _arr(payload, key):
                sid = s.get("id") or ""
                raw_value = s.get("value")
                # `extra` is the enhancement bonus; the game shows value+extra.
                extra = _num(s.get("extra")) if s.get("extra") is not None else 0.0
                out.append(
                    StatLine(
                        stat_id=sid,
                        name=s.get("name", ""),
                        value=_num(raw_value) + extra,
                        percent=_is_percent(raw_value),
                        grade=s.get("grade", ""),
                        slot_pos=s.get("slotPos"),
                        source=source,
                    )
                )
            return out

        set_obj = payload.get("set") or {}

        return cls(
            slot_pos=slot_pos,
            slot_name=slot_name,
            item_id=int(payload.get("id") or 0),
            name=payload.get("name", ""),
            grade=payload.get("grade", ""),
            enchant_level=int(payload.get("enchantLevel") or 0),
            max_enchant_level=int(payload.get("maxEnchantLevel") or 0),
            category=payload.get("categoryName", ""),
            magic_stone_slots=int(payload.get("magicStoneSlotCount") or 0),
            god_stone_slots=int(payload.get("godStoneSlotCount") or 0),
            soul_bind_rate=_num(payload.get("soulBindRate", 100)),
            main_stats=lines("mainStats", "main"),
            sub_stats=lines("subStats", "sub"),
            stones=lines("magicStoneStat", "stone"),
            god_stones=list(_arr(payload, "godStoneStat")),
            sub_skills=[
                (s.get("name", ""), int(s.get("level") or 0))
                for s in _arr(payload, "subSkills")
            ],
            set_name=set_obj.get("name", ""),
            set_equipped=int(set_obj.get("equippedCount") or 0),
            raw=payload,
        )


# --------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------


#: Highest base level observed on any skill. Deadshot and Vaizel's Authority
#: both sit here, and nothing exceeds it, so treat it as the cap.
SKILL_LEVEL_CAP = 20


@dataclass(slots=True)
class Skill:
    name: str
    category: str          # Active | Passive | Dp  (Dp == Stigma)
    base_level: int
    daevanion_bonus: int = 0
    arcana_bonus: int = 0
    acquired: bool = True
    equipped: bool = False

    @property
    def effective_level(self) -> int:
        return self.base_level + self.daevanion_bonus + self.arcana_bonus

    @property
    def is_stigma(self) -> bool:
        return self.category == "Dp"

    @property
    def in_use(self) -> bool:
        """Does this skill currently do anything?

        Passives report equip=0 across the board, so they count as in use once
        acquired. Actives and stigmas must actually be slotted.
        """
        if not self.acquired:
            return False
        if self.category == "Passive":
            return True
        return self.equipped

    @property
    def levels_to_cap(self) -> int:
        return max(0, SKILL_LEVEL_CAP - self.base_level)


_EFFECT = re.compile(r"^(?P<name>.+?)\s*\+(?P<amount>\d+(?:\.\d+)?)\s*(?P<pct>%?)$")


def parse_effect(desc: str) -> tuple[str, float, bool] | None:
    """Split a free-text effect like 'Critical Hit +30' or 'Cooldown +3%'."""
    m = _EFFECT.match(desc.strip())
    if not m:
        return None
    return m.group("name").strip(), float(m.group("amount")), bool(m.group("pct"))


# --------------------------------------------------------------------------
# board
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Board:
    board_id: int
    name: str
    open_nodes: int
    total_nodes: int
    stat_effects: list[tuple[str, float, bool]] = field(default_factory=list)
    skill_effects: list[tuple[str, int]] = field(default_factory=list)

    @property
    def percent_open(self) -> float:
        return 100.0 * self.open_nodes / self.total_nodes if self.total_nodes else 0.0


# --------------------------------------------------------------------------
# the whole character
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Character:
    name: str
    class_name: str
    level: int
    race: str
    server_name: str
    region: str
    server_id: int
    combat_power: int
    item_level: int
    attributes: dict[str, float] = field(default_factory=dict)   # STR/DEX/...
    god_stats: dict[str, float] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    boards: list[Board] = field(default_factory=list)
    titles: list[dict[str, Any]] = field(default_factory=list)

    #: {slot_pos: {stat_id: gain}} -- exact main-stat gain from taking each
    #: item to its enchant cap, measured against PLAYNC at snapshot time.
    #: Empty when the snapshot was taken without an enhancement scan.
    enhancement_headroom: dict[str, dict[str, float]] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------

    @property
    def has_enhancement_data(self) -> bool:
        return bool(self.enhancement_headroom)

    @property
    def enhancement_levels_available(self) -> int:
        return sum(
            max(0, it.max_enchant_level - it.enchant_level) for it in self.items
        )

    def total_enhancement_gain(self) -> dict[str, float]:
        """Summed stat gain if every item went to its cap."""
        out: dict[str, float] = {}
        for gains in self.enhancement_headroom.values():
            for stat_id, value in gains.items():
                out[stat_id] = out.get(stat_id, 0.0) + value
        return out

    @property
    def gear_accuracy(self) -> float:
        """Flat Accuracy readable from gear.

        Deliberately NOT a claim about the in-game total: the class base value
        by level and any active buffs are not published. See docs/endpoints.md
        section 4. Exact id matching avoids the AbnormalAccuracy trap.
        """
        return sum(
            ln.value
            for item in self.items
            for ln in item.all_lines
            if S.is_accuracy(ln.stat_id) and not ln.percent
        )

    @property
    def wasted_lines(self) -> list[tuple[Item, StatLine]]:
        return [(it, ln) for it in self.items for ln in it.wasted_lines]

    def stat_total(self, stat_id: str) -> float:
        return sum(it.total(stat_id) for it in self.items)

    def skill(self, name: str) -> Skill | None:
        for s in self.skills:
            if s.name == name:
                return s
        return None

    def item_at(self, slot_pos: int) -> Item | None:
        for it in self.items:
            if it.slot_pos == slot_pos:
                return it
        return None


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def build_character(
    info: dict[str, Any],
    equipment: dict[str, Any],
    items: Iterable[Item],
    boards: Iterable[Board],
    enhancement_headroom: dict[str, dict[str, float]] | None = None,
) -> Character:
    """Combine the raw payloads into one object, resolving effective skill
    levels across all three of their sources."""
    profile = info.get("profile") or {}
    stat_list = (info.get("stat") or {}).get("statList") or []

    attributes: dict[str, float] = {}
    god_stats: dict[str, float] = {}
    item_level = 0
    for s in stat_list:
        # Key on `type`. `name` is localized and ItemLevel comes back Korean.
        t = s.get("type") or ""
        v = _num(s.get("value"))
        if t in S.BASE_ATTRIBUTES:
            attributes[t] = v
        elif t in S.GOD_STATS:
            god_stats[t] = v
        elif t == "ItemLevel":
            item_level = int(v)

    item_list = list(items)
    board_list = list(boards)

    # Effective skill level = listed + Daevanion + Arcana, matched by name
    # because the three sources do not share skill ids.
    daev: dict[str, int] = {}
    for b in board_list:
        for sk_name, amount in b.skill_effects:
            daev[sk_name] = daev.get(sk_name, 0) + amount

    arcana: dict[str, int] = {}
    for it in item_list:
        for sk_name, lvl in it.sub_skills:
            arcana[sk_name] = arcana.get(sk_name, 0) + lvl

    skills: list[Skill] = []
    for s in (equipment.get("skill") or {}).get("skillList") or []:
        nm = s.get("name", "")
        skills.append(
            Skill(
                name=nm,
                category=s.get("category", ""),
                base_level=int(s.get("skillLevel") or 0),
                daevanion_bonus=daev.get(nm, 0),
                arcana_bonus=arcana.get(nm, 0),
                acquired=bool(s.get("acquired")),
                equipped=bool(s.get("equip")),
            )
        )

    return Character(
        name=profile.get("characterName", ""),
        class_name=profile.get("className", ""),
        level=int(profile.get("characterLevel") or 0),
        race=profile.get("raceName", ""),
        server_name=profile.get("serverName", ""),
        region=profile.get("regionName") or "",
        server_id=int(profile.get("serverId") or 0),
        combat_power=int(profile.get("combatPower") or 0),
        item_level=item_level,
        attributes=attributes,
        god_stats=god_stats,
        items=item_list,
        skills=skills,
        boards=board_list,
        # titleList is the three stat-bearing equipped titles. profile.titleName
        # is a separate cosmetic title and is often blank; ignore it.
        titles=list((info.get("title") or {}).get("titleList") or []),
        enhancement_headroom=dict(enhancement_headroom or {}),
    )


def parse_board(summary: dict[str, Any], detail: dict[str, Any]) -> Board:
    """Build a Board from its info-list summary plus its detail payload.

    Prefer the pre-aggregated openStatEffectList over walking nodeList.
    """
    stat_effects = []
    for e in _arr(detail, "openStatEffectList"):
        parsed = parse_effect(e.get("desc", ""))
        if parsed:
            stat_effects.append(parsed)

    skill_effects = []
    for e in _arr(detail, "openSkillEffectList"):
        parsed = parse_effect(e.get("desc", ""))
        if parsed:
            name, amount, _pct = parsed
            skill_effects.append((name, int(amount)))

    return Board(
        board_id=int(summary.get("id") or 0),
        name=summary.get("name", ""),
        open_nodes=int(summary.get("openNodeCount") or 0),
        total_nodes=int(summary.get("totalNodeCount") or 0),
        stat_effects=stat_effects,
        skill_effects=skill_effects,
    )
