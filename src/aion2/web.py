"""Read-only recommendations dashboard.

Phase 1 is deliberately read-only: it shows what the engine found and nothing
is editable. The interactive what-if editor comes later, and the enhancement
calculator discovered in Phase 0 is what will power it.
"""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Character  # noqa: F401  (used in type hints below)

from fastapi import FastAPI, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from . import stats as S
from .client import Aion2Client
from .config import Settings, load_settings
from .fetch import snapshot_payloads
from .recommend import Engine, Recommendation, UnknownProfile, load_config
from .replay import from_payloads
from .shugo import ShugoCatalog

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


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Constant-time bearer check for the MCP endpoint.

    compare_digest rather than == so the comparison does not leak the token
    a character at a time through timing.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request, call_next):
        sent = request.headers.get("authorization", "")
        if not secrets.compare_digest(sent, self._expected):
            return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    mcp_app, mcp_lifespan = _build_mcp(cfg)
    app = FastAPI(
        title="AION 2 Tracker", docs_url=None, redoc_url=None,
        lifespan=mcp_lifespan,
    )
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
                    has_catalog=False,
                    key_skills=[],
                    skill_cap=20,
                    profiles=profiles,
                    active_profile=eng.profile,
                    active_label=eng.profile_label,
                )
            )

        with ShugoCatalog(cfg.db_path, offline=True) as cat:
            catalog = cat if cat.cached_count() else None
            recs = eng.audit(char, catalog)
            has_catalog = catalog is not None
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
                has_catalog=has_catalog,
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
        with ShugoCatalog(cfg.db_path, offline=True) as cat:
            catalog = cat if cat.cached_count() else None
            recs = eng.audit(char, catalog)
            return JSONResponse(
                {
                    "profile": eng.profile,
                    "profile_label": eng.profile_label,
                    "catalog": bool(catalog),
                    "recommendations": [rec_to_dict(r) for r in recs],
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

    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    _start_scheduler(app, cfg)

    return app


def _transport_security(cfg: Settings):
    """Hosts the MCP endpoint will answer to.

    The SDK enforces DNS-rebinding protection and rejects any unexpected Host
    header with 421. That is correct for a local server and wrong the moment
    the service sits behind a tunnel presenting its own hostname, so the
    allowed list has to include wherever it is actually reached.

    MCP_ALLOWED_HOSTS="*" turns the protection off entirely. Only sensible
    when something in front is already validating the host.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    extra = list(cfg.mcp_allowed_hosts)
    if "*" in extra:
        log.warning(
            "MCP_ALLOWED_HOSTS=* disables DNS rebinding protection on /mcp."
        )
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts = [
        "localhost", f"localhost:{cfg.port}",
        "127.0.0.1", f"127.0.0.1:{cfg.port}",
        *extra,
        *[f"{h}:{cfg.port}" for h in extra if ":" not in h],
    ]
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return TransportSecuritySettings(
        allowed_hosts=hosts, allowed_origins=origins
    )


def _build_mcp(cfg: Settings):
    """Build the MCP sub-app and the lifespan that powers it.

    The session manager runs a task group that must be entered before any
    request arrives. A sub-app mounted into FastAPI does NOT get its own
    lifespan run, so the parent has to drive it -- otherwise the first call
    fails with "Task group is not initialized".

    Returns (sub_app, lifespan) or (None, None) when mcp is unavailable.
    """
    try:
        from .mcp_server import build_server
    except ImportError:
        log.warning("mcp package not installed; /mcp disabled")
        return None, None

    server = build_server(cfg)
    # Path "/" inside the sub-app: mounting at /mcp would otherwise nest the
    # SDK's own default and serve the endpoint at /mcp/mcp.
    sub_app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=_transport_security(cfg),
    )

    token = cfg.mcp_auth_token.strip()
    if token:
        # A Starlette app has add_middleware but not FastAPI's decorator.
        sub_app.add_middleware(BearerAuthMiddleware, token=token)
        log.info("/mcp mounted with bearer auth")
    else:
        # Loud, because an unauthenticated /mcp behind a public hostname lets
        # anyone trigger refresh() against PLAYNC in your name.
        log.warning(
            "MCP_AUTH_TOKEN is empty: /mcp is UNAUTHENTICATED. Fine on a LAN, "
            "not fine behind a public hostname."
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with server.session_manager.run():
            yield

    return sub_app, lifespan


def _start_scheduler(app: FastAPI, cfg: Settings) -> None:
    """Snapshot automatically every SNAPSHOT_INTERVAL_HOURS.

    Specified in the original brief and never wired up until now. Skipped
    when the interval is 0 or the character is not configured.
    """
    hours = cfg.snapshot_interval_hours
    if hours <= 0 or not cfg.configured:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        log.warning("apscheduler not installed; automatic snapshots disabled")
        return

    def tick() -> None:
        if not _refresh_lock.acquire(blocking=False):
            log.info("scheduled snapshot skipped: one already running")
            return
        try:
            snapshot_id = run_snapshot(cfg)
            log.info("scheduled snapshot #%s stored", snapshot_id)
        except Exception as exc:  # noqa: BLE001 - never kill the scheduler
            log.error("scheduled snapshot failed: %s", exc)
        finally:
            _refresh_lock.release()

    scheduler = BackgroundScheduler(daemon=True)
    # jitter spreads restarts so a container flap does not hammer PLAYNC
    scheduler.add_job(
        tick,
        IntervalTrigger(hours=hours, jitter=600),
        id="snapshot",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("automatic snapshots every %dh", hours)


CATEGORY_LABELS: dict[str, str] = {
    "enhancement": "Enhancement",
    "manastone": "Manastones",
    "reroll": "Sub-stat rerolls",
    "skill": "Skills",
    "skillslot": "Gear skill slots",
    "bis": "Best in slot",
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
        "slots_empty": sum(it.empty_slots for it in char.items),
        "slots_total": sum(it.slot_capacity for it in char.items),
        "slots_wasted": sum(len(it.reclaimable_slots) for it in char.items),
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
