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

from .models import Character  # noqa: F401  (used in type hints below)

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from . import stats as S
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

# Templates deal in stat ids; humans want the display name. Without this,
# "ArmorDefense +1295" leaks into the page instead of "Defense +1,295".
_jinja.filters["statname"] = lambda sid: S.display_name(sid)

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
                    plan={"top": [], "groups": [], "context": [],
                          "max_score": 1.0, "total": 0},
                    taken_at=None,
                    taken_ago="",
                    totals={},
                    key_skills=[],
                    skill_cap=20,
                    profiles=profiles,
                    active_profile=eng.profile,
                    active_label=eng.profile_label,
                )
            )

        recs = eng.audit(char)
        taken = row["taken_at"] if row else None
        return HTMLResponse(
            template.render(
                character=char,
                settings=cfg,
                problems=cfg.validate(),
                recommendations=recs,
                plan=_split_recommendations(recs),
                taken_at=taken,
                taken_ago=_humanise(taken),
                totals=_totals(char, eng),
                key_skills=eng.key_skills(char),
                skill_cap=int((eng.cfg.get("skills") or {}).get("level_cap", 20)),
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


CATEGORY_LABELS: dict[str, str] = {
    "enhancement": "Enhancement",
    "manastone": "Manastones",
    "reroll": "Sub-stat rerolls",
    "skill": "Skills",
    "skillslot": "Gear skill slots",
    "arcana": "Arcana",
    "soulbind": "Soul binding",
    "attributes": "Attributes",
    "accuracy": "Accuracy",
}

#: Rows that state a fact rather than ask for an action. Kept out of the
#: ranked list so "do this next" means what it says.
CONTEXT_CATEGORIES = frozenset({"accuracy"})


def _humanise(iso: str | None) -> str:
    """'3 hours ago' beats '2026-10-28T21:19:10+00:00' in a header."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    secs = (datetime.now(timezone.utc) - when).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        hours = int(secs // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(secs // 86400)
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return when.strftime("%d %b %Y")


def _split_recommendations(
    recs: list[Recommendation], top_n: int = 3
) -> dict[str, Any]:
    """Headline actions, then everything else grouped by category.

    42 flat rows was the single worst thing about the old page. Three things
    to do next, with the rest filed behind headings, is the same information
    at a fraction of the height.
    """
    actionable = [r for r in recs if r.category not in CONTEXT_CATEGORIES]
    context = [r for r in recs if r.category in CONTEXT_CATEGORIES]

    top = actionable[:top_n]
    rest = actionable[top_n:]

    groups: dict[str, list[Recommendation]] = {}
    for r in rest:
        groups.setdefault(r.category, []).append(r)

    ordered = sorted(
        groups.items(),
        key=lambda kv: -max((r.score for r in kv[1]), default=0.0),
    )

    max_score = max((r.score for r in actionable), default=1.0) or 1.0

    return {
        "top": top,
        "groups": [
            {
                "key": key,
                "label": CATEGORY_LABELS.get(key, key.title()),
                "items": items,
                "count": len(items),
                "best": max((r.score for r in items), default=0.0),
            }
            for key, items in ordered
        ],
        "context": context,
        "max_score": max_score,
        "total": len(actionable),
    }


def _totals(char: Any, engine: Engine) -> dict[str, Any]:
    """Headline numbers for the dashboard cards."""
    wasted = char.wasted_lines
    wasted_stones = sum(
        1 for it in char.items for st in it.stones if engine.is_wasted(st.stat_id)
    )
    enh_totals = char.total_enhancement_gain()
    # Use display names, not raw stat ids -- "ArmorDefense +1295" was leaking
    # into the UI where it should read "Defense +1,295".
    top_enh = [
        (S.display_name(stat_id), value)
        for stat_id, value in sorted(enh_totals.items(), key=lambda kv: -kv[1])[:3]
    ]

    return {
        "gear_accuracy": char.gear_accuracy,
        "wasted_total": len(wasted),
        "wasted_stones": wasted_stones,
        "wasted_substats": len(wasted) - wasted_stones,
        "accuracy_headroom": engine._accuracy_headroom(char),  # noqa: SLF001
        "items": len(char.items),
        "stigmas": sum(1 for s in char.skills if s.is_stigma),
        "skill_slots_empty": sum(it.empty_skill_slots for it in char.items),
        "skill_slots_total": sum(it.sub_skill_slots for it in char.items),
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
