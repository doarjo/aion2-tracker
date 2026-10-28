"""Read-only recommendations dashboard.

Phase 1 is deliberately read-only: it shows what the engine found and nothing
is editable. The interactive what-if editor comes later, and the enhancement
calculator discovered in Phase 0 is what will power it.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .client import Aion2Client
from .config import Settings, load_settings
from .fetch import snapshot_payloads
from .recommend import Engine, Recommendation, load_config
from .replay import from_payloads

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent / "templates"

_jinja = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
)

# One refresh at a time. A snapshot is ~30 rate-limited requests and running
# two concurrently would both double our request rate and race on the DB.
_refresh_lock = threading.Lock()


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    app = FastAPI(title="AION 2 Tracker", docs_url=None, redoc_url=None)
    engine = Engine(load_config(cfg.thresholds_path or None))

    # -- helpers -----------------------------------------------------------

    def load_latest() -> tuple[Any, Any] | tuple[None, None]:
        row = db.latest_snapshot(cfg.db_path, cfg.character_id or None)
        if row is None:
            return None, None
        try:
            payloads = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            return None, None
        if not payloads:
            return None, row
        return from_payloads(payloads), row

    def rec_to_dict(r: Recommendation) -> dict[str, Any]:
        return {
            "category": r.category,
            "title": r.title,
            "detail": r.detail,
            "priority": r.priority.label,
            "priority_rank": int(r.priority),
            "score": round(r.score, 1),
            "slot": r.slot_name,
            "item": r.item_name,
            "gains": r.gains,
            "losses": r.losses,
            "action": r.action,
            "gain_summary": r.gain_summary,
        }

    # -- routes ------------------------------------------------------------

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "configured": cfg.configured,
                "character": cfg.character_name or None,
                "has_snapshot": db.latest_snapshot(cfg.db_path) is not None,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        char, row = load_latest()
        template = _jinja.get_template("dashboard.html")

        if char is None:
            return HTMLResponse(
                template.render(
                    character=None,
                    settings=cfg,
                    problems=cfg.validate(),
                    recommendations=[],
                    taken_at=None,
                    totals={},
                )
            )

        recs = engine.audit(char)
        return HTMLResponse(
            template.render(
                character=char,
                settings=cfg,
                problems=cfg.validate(),
                recommendations=recs,
                taken_at=row["taken_at"] if row else None,
                totals=_totals(char, engine),
            )
        )

    @app.get("/api/character")
    def api_character() -> JSONResponse:
        char, row = load_latest()
        if char is None:
            raise HTTPException(404, "No snapshot yet. POST /refresh first.")
        return JSONResponse(
            {
                "name": char.name,
                "class": char.class_name,
                "level": char.level,
                "race": char.race,
                "server": char.server_name,
                "combat_power": char.combat_power,
                "item_level": char.item_level,
                "gear_accuracy": char.gear_accuracy,
                "attributes": char.attributes,
                "god_stats": char.god_stats,
                "wasted_line_count": len(char.wasted_lines),
                "taken_at": row["taken_at"] if row else None,
            }
        )

    @app.get("/api/recommendations")
    def api_recommendations() -> JSONResponse:
        char, _row = load_latest()
        if char is None:
            raise HTTPException(404, "No snapshot yet. POST /refresh first.")
        return JSONResponse([rec_to_dict(r) for r in engine.audit(char)])

    @app.get("/api/history")
    def api_history(stat: str = "combat_power", days: int = 90) -> JSONResponse:
        try:
            return JSONResponse(db.history(cfg.db_path, stat, days))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/refresh")
    def refresh(force: bool = False) -> JSONResponse:
        if not cfg.configured:
            raise HTTPException(400, "Character not configured. See .env.example.")

        if not force:
            age = db.seconds_since_last_snapshot(cfg.db_path, cfg.character_id)
            cooldown = cfg.snapshot_cooldown_minutes * 60
            if age is not None and age < cooldown:
                raise HTTPException(
                    429,
                    f"Snapshot cooldown: {int(cooldown - age)}s remaining. "
                    f"Pass ?force=true to override.",
                )

        if not _refresh_lock.acquire(blocking=False):
            raise HTTPException(409, "A refresh is already running.")
        try:
            snapshot_id = run_snapshot(cfg)
        finally:
            _refresh_lock.release()

        return JSONResponse({"status": "ok", "snapshot_id": snapshot_id})

    return app


def _totals(char: Any, engine: Engine) -> dict[str, Any]:
    """Headline numbers for the dashboard cards."""
    wasted = char.wasted_lines
    wasted_stones = sum(
        1 for it in char.items for st in it.stones if engine.is_wasted(st.stat_id)
    )
    return {
        "gear_accuracy": char.gear_accuracy,
        "wasted_total": len(wasted),
        "wasted_stones": wasted_stones,
        "wasted_substats": len(wasted) - wasted_stones,
        "accuracy_headroom": engine._accuracy_headroom(char),  # noqa: SLF001
        "items": len(char.items),
        "stigmas": sum(1 for s in char.skills if s.is_stigma),
    }


def run_snapshot(cfg: Settings) -> int:
    """Fetch and store one snapshot. Blocking and rate limited."""
    ref = cfg.ref()
    with Aion2Client(rate_limit_seconds=cfg.rate_limit_seconds) as client:
        payloads = snapshot_payloads(client, ref)
    char = from_payloads(payloads)
    return db.save_snapshot(
        cfg.db_path,
        char,
        region=cfg.region,
        character_id=cfg.character_id,
        raw=payloads,
    )
