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
from .recommend import Engine, Recommendation, UnknownProfile, load_config
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
    thresholds = load_config(cfg.thresholds_path or None, strict=False)
    engine = Engine(thresholds)
    profiles = Engine.available_profiles(thresholds)

    def engine_for(profile: str | None) -> Engine:
        """Engines are cheap; build one per request when a profile is asked
        for, so switching goals needs no restart."""
        if not profile or profile == engine.profile:
            return engine
        try:
            return Engine(thresholds, profile=profile)
        except UnknownProfile as exc:
            raise HTTPException(400, str(exc)) from exc

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
        # thresholds_loaded is reported because a missing config does not stop
        # the service, it just quietly makes every recommendation worse.
        return JSONResponse(
            {
                "status": "ok" if engine.has_config else "degraded",
                "configured": cfg.configured,
                "character": cfg.character_name or None,
                "has_snapshot": db.latest_snapshot(cfg.db_path) is not None,
                "thresholds_loaded": engine.has_config,
                "thresholds_path": cfg.thresholds_path or None,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(profile: str | None = None) -> HTMLResponse:
        char, row = load_latest()
        template = _jinja.get_template("dashboard.html")
        eng = engine_for(profile)

        if char is None:
            return HTMLResponse(
                template.render(
                    character=None,
                    settings=cfg,
                    problems=cfg.validate(),
                    recommendations=[],
                    taken_at=None,
                    totals={},
                    profiles=profiles,
                    active_profile=eng.profile,
                    active_label=eng.profile_label,
                )
            )

        recs = eng.audit(char)
        return HTMLResponse(
            template.render(
                character=char,
                settings=cfg,
                problems=cfg.validate(),
                recommendations=recs,
                taken_at=row["taken_at"] if row else None,
                totals=_totals(char, eng),
                profiles=profiles,
                active_profile=eng.profile,
                active_label=eng.profile_label,
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

    @app.get("/api/profiles")
    def api_profiles() -> JSONResponse:
        return JSONResponse({"active": engine.profile, "available": profiles})

    @app.get("/api/recommendations")
    def api_recommendations(profile: str | None = None) -> JSONResponse:
        char, _row = load_latest()
        if char is None:
            raise HTTPException(404, "No snapshot yet. POST /refresh first.")
        eng = engine_for(profile)
        return JSONResponse(
            {
                "profile": eng.profile,
                "profile_label": eng.profile_label,
                "recommendations": [rec_to_dict(r) for r in eng.audit(char)],
            }
        )

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
    enh_totals = char.total_enhancement_gain()
    top_enh = sorted(enh_totals.items(), key=lambda kv: -kv[1])[:3]

    return {
        "gear_accuracy": char.gear_accuracy,
        "wasted_total": len(wasted),
        "wasted_stones": wasted_stones,
        "wasted_substats": len(wasted) - wasted_stones,
        "accuracy_headroom": engine._accuracy_headroom(char),  # noqa: SLF001
        "items": len(char.items),
        "stigmas": sum(1 for s in char.skills if s.is_stigma),
        "has_enhancement": char.has_enhancement_data,
        "enh_levels": char.enhancement_levels_available,
        "enh_top": top_enh,
    }


def run_snapshot(cfg: Settings, *, scan_enhancements: bool = True) -> int:
    """Fetch and store one snapshot. Blocking and rate limited.

    With the enhancement scan this is roughly 60 requests, so about four
    minutes at the default spacing.
    """
    ref = cfg.ref()
    with Aion2Client(rate_limit_seconds=cfg.rate_limit_seconds) as client:
        payloads = snapshot_payloads(
            client, ref, scan_enhancements=scan_enhancements
        )
    char = from_payloads(payloads)
    return db.save_snapshot(
        cfg.db_path,
        char,
        region=cfg.region,
        character_id=cfg.character_id,
        raw=payloads,
    )
