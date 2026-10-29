"""MCP server: read-only access to the tracked character.

This is the original point of the project -- so Claude can read the gear and
stats directly instead of being sent screenshots.

Every tool is READ-ONLY except `refresh`, which takes a snapshot and is
subject to the same cooldown as everything else. Responses are compact JSON
summaries rather than raw API dumps, so a whole character fits comfortably
in context.

Two caveats are repeated in the tool OUTPUT rather than only documented
here, because a caller reading a single tool result will not have seen this
file:

* figures reflect the character's ACTIVE preset; PLAYNC exposes no others
* Combat Power is not calculable, so recommendations are a ranked heuristic
  and `get_history` is the honest measure of what a change did
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import db
from .client import Aion2Client
from .compare import compare as compare_builds
from .compare import detect_preset_change, skill_gaps
from .config import Settings, load_settings
from .models import Character
from .recommend import Engine, load_config
from .replay import from_payloads
from .shugo import ShugoCatalog

log = logging.getLogger(__name__)

PRESET_CAVEAT = (
    "Reflects the character's ACTIVE preset only; PLAYNC exposes no others."
)

CP_CAVEAT = (
    "Combat Power is not calculable from the API. The ranking is a weighting "
    "heuristic; the stat gains quoted are exact. Use get_history to see what "
    "a change actually did."
)


def _load(cfg: Settings) -> tuple[Character | None, str | None]:
    row = db.latest_snapshot(cfg.db_path, cfg.character_id or None)
    if row is None:
        return None, None
    payloads = json.loads(row["payload"] or "{}")
    if not payloads:
        return None, row["taken_at"]
    return from_payloads(payloads), row["taken_at"]


def _need_snapshot() -> dict[str, Any]:
    return {
        "error": "no_snapshot",
        "message": "No snapshot stored yet. Call refresh, or run 'aion2 snapshot'.",
    }


def build_server(settings: Settings | None = None) -> MCPServer:
    cfg = settings or load_settings()
    thresholds = load_config(cfg.thresholds_path or None, strict=False)

    mcp = MCPServer(
        name="aion2-tracker",
        title="AION 2 Character Tracker",
        version="0.1.0",
        instructions=(
            "Read-only access to one tracked AION 2 Global character. Figures "
            "come from PLAYNC's public endpoints and reflect the character's "
            "active preset. Combat Power cannot be calculated from the API, so "
            "get_recommendations is a ranked heuristic and get_history is the "
            "honest measure of what a change actually did."
        ),
    )

    def engine_for(profile: str | None = None) -> Engine:
        return Engine(thresholds, profile=profile)

    # -- character ---------------------------------------------------------

    @mcp.tool(
        description=(
            "Profile and headline stats for the tracked character: class, "
            "level, Combat Power, item level, base attributes and god stats."
        )
    )
    def get_character() -> dict[str, Any]:
        char, taken = _load(cfg)
        if char is None:
            return _need_snapshot()
        return {
            "name": char.name,
            "class": char.class_name,
            "level": char.level,
            "race": char.race,
            "server": char.server_name,
            "combat_power": char.combat_power,
            "item_level": char.item_level,
            "attributes": {k: int(v) for k, v in char.attributes.items()},
            "god_stats": {k: int(v) for k, v in char.god_stats.items()},
            "gear_accuracy": char.gear_accuracy,
            "gear_accuracy_note": (
                "Flat Accuracy readable from gear only. NOT the in-game total: "
                "the class base value by level and active buffs are not published."
            ),
            "snapshot_taken_at": taken,
            "note": PRESET_CAVEAT,
        }

    @mcp.tool(
        description=(
            "Every equipped item: slot, enhancement level and cap, grade, soul "
            "bind percentage, manastones, rolled sub-stats and any skill "
            "grants. Set wasted_only to list just the slots carrying stats "
            "that do nothing for the build."
        )
    )
    def get_equipment(wasted_only: bool = False) -> dict[str, Any]:
        char, taken = _load(cfg)
        if char is None:
            return _need_snapshot()

        items = []
        for it in sorted(char.items, key=lambda i: i.slot_pos):
            if wasted_only and not it.reclaimable_slots:
                continue
            items.append(
                {
                    "slot": it.slot_name,
                    "name": it.name,
                    "grade": it.grade,
                    "enchant": f"+{it.enchant_level}/{it.max_enchant_level}",
                    "soul_bind_percent": it.soul_bind_rate,
                    "slots": f"{it.slots_used}/{it.slot_capacity}",
                    "rerollable": it.rerollable,
                    "sub_stats": [f"{ln.label} {ln.value:g}" for ln in it.sub_stats],
                    "skill_grants": [f"{n} +{lv}" for n, lv in it.sub_skills],
                    "manastones": [
                        f"{st.grade} {st.label} +{st.value:g}" for st in it.stones
                    ],
                    "wasted": [ln.label for ln in it.reclaimable_slots],
                    "set": it.set_name or None,
                }
            )
        return {
            "character": char.name,
            "items": items,
            "enhancement_levels_available": char.enhancement_levels_available,
            "wasted_line_count": len(char.wasted_lines),
            "snapshot_taken_at": taken,
            "note": PRESET_CAVEAT,
        }

    @mcp.tool(
        description=(
            "Skills with their real levels. The in-game level already includes "
            "Daevanion board and gear grants; trained level is what was "
            "levelled by hand and caps at 10, or 20 for stigmas."
        )
    )
    def get_skills(key_only: bool = True) -> dict[str, Any]:
        char, taken = _load(cfg)
        if char is None:
            return _need_snapshot()

        eng = engine_for()
        chosen = eng.key_skills(char) if key_only else char.skills
        return {
            "character": char.name,
            "skills": [
                {
                    "name": s.name,
                    "category": s.category,
                    "in_game": s.displayed_level,
                    "trained": f"{s.trained_level}/{s.trained_cap}",
                    "from_boards": s.daevanion_bonus,
                    "from_gear": s.gear_bonus,
                    "at_cap": s.at_cap,
                }
                for s in chosen
            ],
            "stigmas_not_slotted": [
                s.name
                for s in char.skills
                if s.is_stigma and s.acquired and not s.equipped
            ],
            "snapshot_taken_at": taken,
            "note": PRESET_CAVEAT,
        }

    @mcp.tool(description="Daevanion board progress, nodes opened per board.")
    def get_daevanion() -> dict[str, Any]:
        char, taken = _load(cfg)
        if char is None:
            return _need_snapshot()
        return {
            "character": char.name,
            "boards": [
                {
                    "name": b.name,
                    "id": b.board_id,
                    "open": b.open_nodes,
                    "total": b.total_nodes,
                    "percent": round(b.percent_open, 1),
                }
                for b in char.boards
            ],
            "snapshot_taken_at": taken,
        }

    # -- the valuable one --------------------------------------------------

    @mcp.tool(
        description=(
            "Ranked list of what to fix next, highest impact first. Optionally "
            "pick a goal profile: combat_power (default), pve_dps or "
            "accuracy_rush."
        )
    )
    def get_recommendations(
        profile: str | None = None, limit: int = 12
    ) -> dict[str, Any]:
        char, taken = _load(cfg)
        if char is None:
            return _need_snapshot()

        eng = engine_for(profile)
        with ShugoCatalog(cfg.db_path, offline=True) as cat:
            catalog = cat if cat.cached_count() else None
            recs = eng.audit(char, catalog)

        return {
            "character": char.name,
            "goal": eng.profile_label,
            "total": len(recs),
            "recommendations": [
                {
                    "priority": r.priority.label,
                    "category": r.category,
                    "what": r.title,
                    "why": r.detail,
                    "gain": r.gain_summary or None,
                }
                for r in recs[: max(1, limit)]
            ],
            "snapshot_taken_at": taken,
            "notes": [PRESET_CAVEAT, CP_CAVEAT],
        }

    @mcp.tool(
        description=(
            "How a tracked number has moved across snapshots. This is the "
            "honest measure of progress, since Combat Power cannot be "
            "predicted. Stat is one of combat_power, item_level, "
            "gear_accuracy, wasted_lines or level."
        )
    )
    def get_history(stat: str = "combat_power", days: int = 90) -> dict[str, Any]:
        try:
            rows = db.history(cfg.db_path, stat, days)
        except ValueError as exc:
            return {"error": "bad_stat", "message": str(exc)}
        if not rows:
            return {"stat": stat, "points": [], "message": "No snapshots in range."}

        values = [r["value"] for r in rows]
        return {
            "stat": stat,
            "points": [{"at": r["taken_at"], "value": r["value"]} for r in rows],
            "first": values[0],
            "last": values[-1],
            "change": values[-1] - values[0],
            "snapshots": len(rows),
        }

    # -- other characters --------------------------------------------------

    @mcp.tool(
        description=(
            "Find a character by name. Matching is case-insensitive substring, "
            "so 'Kira' also returns 'Akira'. Region is one of nae, naw, eu, "
            "la, as."
        )
    )
    def search_character(
        name: str, region: str | None = None, server_id: int | None = None
    ) -> dict[str, Any]:
        with Aion2Client(rate_limit_seconds=cfg.rate_limit_seconds) as client:
            hits = client.search(name, region or cfg.region, server_id)
        return {
            "query": name,
            "matches": [
                {
                    "name": h.name,
                    "level": h.level,
                    "server": h.server_name,
                    "server_id": h.server_id,
                    "region": h.region,
                }
                for h in sorted(hits, key=lambda h: -h.level)
            ],
        }

    @mcp.tool(
        description=(
            "Compare the tracked character against a reference build already "
            "captured under data/reference. Reports gaps dimension by "
            "dimension rather than as a single score."
        )
    )
    def compare_to(name: str) -> dict[str, Any]:
        char, _taken = _load(cfg)
        if char is None:
            return _need_snapshot()

        cache = Path(cfg.db_path).parent / "reference" / f"{name.lower()}.json"
        if not cache.exists():
            return {
                "error": "no_reference",
                "message": (
                    f"No capture of {name}. Run 'aion2 compare {name}' once to "
                    f"fetch it."
                ),
            }

        theirs = from_payloads(json.loads(cache.read_text(encoding="utf-8")))
        eng = engine_for()
        result = compare_builds(char, theirs, eng)

        return {
            "mine": char.name,
            "theirs": theirs.name,
            "gaps": [
                {
                    "dimension": g.dimension,
                    "mine": g.format(g.mine),
                    "theirs": g.format(g.theirs),
                    "behind": g.behind,
                    "is_outcome": g.outcome,
                    "note": g.note or None,
                }
                for g in result.gaps
            ],
            "biggest_gaps": [g.dimension for g in result.behind_on[:4]],
            "skills_they_lead": [
                {"skill": n, "mine": a, "theirs": b}
                for n, a, b in skill_gaps(char, theirs, eng)
            ],
            "notes": result.notes + [PRESET_CAVEAT],
        }

    # -- the one write -----------------------------------------------------

    @mcp.tool(
        description=(
            "Take a fresh snapshot from PLAYNC. Roughly 60 rate-limited "
            "requests, so about four minutes. Subject to a cooldown unless "
            "force is set."
        )
    )
    def refresh(force: bool = False) -> dict[str, Any]:
        if not cfg.configured:
            return {
                "error": "not_configured",
                "message": "Character not configured. See .env.example.",
            }
        if not force:
            age = db.seconds_since_last_snapshot(cfg.db_path, cfg.character_id)
            cooldown = cfg.snapshot_cooldown_minutes * 60
            if age is not None and age < cooldown:
                return {
                    "error": "cooldown",
                    "seconds_remaining": int(cooldown - age),
                    "message": "Pass force=true to override.",
                }

        from .web import run_snapshot

        before, _ = _load(cfg)
        snapshot_id = run_snapshot(cfg)
        after, taken = _load(cfg)

        result: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "taken_at": taken,
            "combat_power": after.combat_power if after else None,
        }
        if before is not None and after is not None:
            change = detect_preset_change(before, after)
            result["combat_power_change"] = after.combat_power - before.combat_power
            if change.likely:
                result["warning"] = (
                    f"{change.slots_changed} slots changed item at once. That "
                    f"looks like a preset switch rather than progression."
                )
        return result

    return mcp
