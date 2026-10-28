"""Build a Character from saved capture files instead of the network.

Used by tests and for offline development. A capture directory is what
scripts/discover.ps1 writes:

    10-info.json
    12-equipment.json
    20-item-<slotPos>-<SlotName>.json
    22-daevanion-<boardId>-<Name>.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Board, Character, Item, build_character, parse_board

_ITEM_RE = re.compile(r"^20-item-(\d+)-(.+)\.json$")
_BOARD_RE = re.compile(r"^22-daevanion-(\d+)")


def from_payloads(payloads: dict[str, Any]) -> Character:
    """Rebuild a Character from the raw dict produced by
    fetch.snapshot_payloads, which is what gets archived in SQLite.

    This is why raw payloads are stored: a parser improvement can be replayed
    against every historical snapshot without refetching anything.
    """
    info = payloads.get("info") or {}
    equipment = payloads.get("equipment") or {}

    slot_names = {
        int(e.get("slotPos") or 0): e.get("slotPosName", "")
        for e in (equipment.get("equipment") or {}).get("equipmentList") or []
    }

    items: list[Item] = []
    for slot_str, payload in (payloads.get("items") or {}).items():
        slot_pos = int(slot_str)
        items.append(
            Item.parse(payload, slot_pos, slot_names.get(slot_pos, f"slot{slot_pos}"))
        )
    items.sort(key=lambda i: i.slot_pos)

    summaries = {
        int(b.get("id") or 0): b
        for b in (info.get("daevanion") or {}).get("boardList") or []
    }
    boards: list[Board] = []
    for board_str, detail in (payloads.get("boards") or {}).items():
        bid = int(board_str)
        boards.append(parse_board(summaries.get(bid, {"id": bid}), detail))
    boards.sort(key=lambda b: b.board_id)

    return build_character(
        info, equipment, items, boards,
        enhancement_headroom=payloads.get("enhancement_headroom") or {},
    )


def _load(path: Path) -> Any:
    # utf-8-sig tolerates a BOM, which PowerShell's Out-File writes by default.
    with path.open(encoding="utf-8-sig") as fh:
        return json.load(fh)


def load_capture(directory: str | Path) -> Character:
    d = Path(directory)
    if not d.is_dir():
        raise FileNotFoundError(f"capture directory not found: {d}")

    info_path = d / "10-info.json"
    equip_path = d / "12-equipment.json"
    for p in (info_path, equip_path):
        if not p.exists():
            raise FileNotFoundError(f"missing {p.name} in {d}")

    info = _load(info_path)
    equipment = _load(equip_path)

    items: list[Item] = []
    for path in sorted(d.glob("20-item-*.json")):
        m = _ITEM_RE.match(path.name)
        if not m:
            continue
        items.append(Item.parse(_load(path), int(m.group(1)), m.group(2)))

    summaries = {
        int(b.get("id") or 0): b
        for b in (info.get("daevanion") or {}).get("boardList") or []
    }
    boards: list[Board] = []
    for path in sorted(d.glob("22-daevanion-*.json")):
        m = _BOARD_RE.match(path.name)
        if not m:
            continue
        bid = int(m.group(1))
        boards.append(parse_board(summaries.get(bid, {"id": bid}), _load(path)))

    return build_character(info, equipment, items, boards)
