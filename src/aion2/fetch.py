"""Orchestrates a full character read.

A complete snapshot costs 28-34 requests: 1 info + 1 equipment + one per
equipped slot + one per opened Daevanion board. At the default 4s rate limit
that is roughly two minutes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .client import Aion2Client, CharacterRef
from .models import Board, Character, Item, build_character, parse_board

log = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]


def fetch_character(
    client: Aion2Client,
    ref: CharacterRef,
    *,
    include_boards: bool = True,
    progress: Progress | None = None,
) -> Character:
    """Read everything public about one character."""

    def report(stage: str, done: int, total: int) -> None:
        if progress:
            progress(stage, done, total)
        log.debug("%s %d/%d", stage, done, total)

    info = client.info(ref)
    report("info", 1, 1)

    equipment = client.equipment(ref)
    report("equipment", 1, 1)

    slots = (equipment.get("equipment") or {}).get("equipmentList") or []
    items: list[Item] = []
    for i, entry in enumerate(slots, start=1):
        slot_pos = int(entry.get("slotPos") or 0)
        slot_name = entry.get("slotPosName", f"slot{slot_pos}")
        payload = client.item(
            ref,
            item_id=int(entry.get("id") or 0),
            enchant_level=int(entry.get("enchantLevel") or 0),
            slot_pos=slot_pos,
        )
        items.append(Item.parse(payload, slot_pos, slot_name))
        report("items", i, len(slots))

    boards: list[Board] = []
    if include_boards:
        # Board ids are class-specific. Always enumerate; never hardcode.
        summaries = [
            b
            for b in ((info.get("daevanion") or {}).get("boardList") or [])
            if b.get("open") == 1
        ]
        for i, summary in enumerate(summaries, start=1):
            detail = client.daevanion(ref, int(summary.get("id") or 0))
            boards.append(parse_board(summary, detail))
            report("boards", i, len(summaries))

    return build_character(info, equipment, items, boards)


def fetch_item_at_enchant(
    client: Aion2Client, ref: CharacterRef, item: Item, enchant_level: int
) -> Item:
    """Re-read one equipped item as if it were enhanced to `enchant_level`.

    PLAYNC recalculates for any level in range, including levels the character
    has not reached. This is what makes upgrade recommendations exact rather
    than estimated.
    """
    payload = client.item(
        ref,
        item_id=item.item_id,
        enchant_level=enchant_level,
        slot_pos=item.slot_pos,
    )
    return Item.parse(payload, item.slot_pos, item.slot_name)


def scan_enhancement_headroom(
    client: Aion2Client,
    ref: CharacterRef,
    items: list[Item],
    *,
    progress: Progress | None = None,
) -> dict[str, dict[str, float]]:
    """Exact main-stat gain from taking each item to its enchant cap.

    One request per upgradeable item. PLAYNC recalculates for any level in
    range, including levels not yet reached, so these are real values rather
    than estimates.

    Returns {slot_pos: {stat_id: delta}}. Items already at cap, or whose cap
    cannot be queried, are omitted.
    """
    out: dict[str, dict[str, float]] = {}
    upgradeable = [i for i in items if i.enchant_level < i.max_enchant_level]

    for n, item in enumerate(upgradeable, start=1):
        try:
            capped = fetch_item_at_enchant(client, ref, item, item.max_enchant_level)
        except Exception as exc:  # noqa: BLE001
            # Some items report a maxEnchantLevel that 404s when queried.
            # Record nothing rather than guessing.
            log.warning(
                "enhancement scan failed for %s at +%d: %s",
                item.name, item.max_enchant_level, exc,
            )
            continue

        gains: dict[str, float] = {}
        for new in capped.main_stats:
            old = next(
                (m.value for m in item.main_stats if m.stat_id == new.stat_id), 0.0
            )
            delta = new.value - old
            if delta > 0:
                gains[new.stat_id] = delta
        if gains:
            out[str(item.slot_pos)] = gains

        if progress:
            progress("enhancement", n, len(upgradeable))

    return out


def snapshot_payloads(
    client: Aion2Client,
    ref: CharacterRef,
    *,
    scan_enhancements: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Raw payloads, for archiving alongside a parsed snapshot so a future
    parser change can be replayed against real historical data.

    With `scan_enhancements` the snapshot also records exact enhancement
    headroom per item. That roughly doubles the request count, but means the
    audit never needs the network afterwards.
    """
    info = client.info(ref)
    equipment = client.equipment(ref)
    slots = (equipment.get("equipment") or {}).get("equipmentList") or []

    items = {}
    parsed: list[Item] = []
    for n, entry in enumerate(slots, start=1):
        sp = int(entry.get("slotPos") or 0)
        payload = client.item(
            ref, int(entry.get("id") or 0), int(entry.get("enchantLevel") or 0), sp
        )
        items[str(sp)] = payload
        parsed.append(Item.parse(payload, sp, entry.get("slotPosName", f"slot{sp}")))
        if progress:
            progress("items", n, len(slots))

    boards = {}
    summaries = [
        b for b in ((info.get("daevanion") or {}).get("boardList") or [])
        if b.get("open") == 1
    ]
    for n, b in enumerate(summaries, start=1):
        bid = int(b.get("id") or 0)
        boards[str(bid)] = client.daevanion(ref, bid)
        if progress:
            progress("boards", n, len(summaries))

    payloads: dict[str, Any] = {
        "info": info,
        "equipment": equipment,
        "items": items,
        "boards": boards,
    }

    if scan_enhancements:
        payloads["enhancement_headroom"] = scan_enhancement_headroom(
            client, ref, parsed, progress=progress
        )

    return payloads
