"""Item catalog from Shugo.GG.

PLAYNC publishes no item catalog for Global, so the engine could never know
which stats a slot is ABLE to roll -- only which it currently has. Shugo.GG
publishes exactly that, keyed by the same item ids PLAYNC uses.

WHAT THIS MODULE IS ALLOWED TO DO, AND WHY
------------------------------------------
Shugo.GG's robots.txt disallows `/api/` for every agent, naming ClaudeBot,
Claude-User and anthropic-ai specifically. **Do not call /api/.** If you are
tempted to "optimise" this module by hitting the endpoint whose path appears
in the seed block below: don't. It is disallowed.

What they do publish, and explicitly invite agents to read and cite, is the
server-rendered item page. Their llms.txt says:

    one page per item (`/items/<id>`) with grade, level, class restrictions,
    the Global base stats, enchant cap and random stats pool server-side

So this module reads `/items/<id>` -- an Allow: / path -- and parses the
`shugo-seed` JSON the page ships for hydration. Every response is cached, and
only items the character actually has equipped are ever fetched, which is
roughly 25 requests once rather than a crawl of their 10,863 items.

Per their request, the dashboard credits Shugo.GG and links back.

Shugo.GG is a fan site, unaffiliated with NCSOFT, as is this project.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .db import connect

log = logging.getLogger(__name__)

BASE = "https://shugo.gg"
ITEM_PAGE = BASE + "/items/{item_id}"
CREDIT_URL = BASE + "/items/{item_id}"
ATTRIBUTION = "Item roll pools from Shugo.GG"

#: Identify the tool honestly rather than pretending to be a browser.
USER_AGENT = (
    "aion2-tracker/0.1 (personal AION 2 character tracker; "
    "+https://github.com/doarjo/aion2-tracker)"
)

_SEED = re.compile(
    r'<script type="application/json" id="shugo-seed">(.*?)</script>', re.S
)
_NUM = re.compile(r"-?\d+(?:\.\d+)?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_items (
    item_id    INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class RollOption:
    """One stat an item is able to roll, with its range."""

    stat_id: str
    name: str
    min_value: float
    max_value: float
    percent: bool = False

    def quality(self, rolled: float) -> float:
        """Where `rolled` sits in the range, 0.0 at the floor, 1.0 at the cap.

        Returns 1.0 for a fixed stat whose floor equals its cap.
        """
        span = self.max_value - self.min_value
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (rolled - self.min_value) / span))


@dataclass(slots=True)
class CatalogItem:
    item_id: int
    name: str
    grade: str
    category: str
    equip_level: int
    max_enchant_level: int
    max_exceed_level: int
    roll_pool: list[RollOption] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    magic_stone_slots: int = 0
    sub_stat_slots: int = 0
    sub_skill_slots: int = 0

    @property
    def url(self) -> str:
        return CREDIT_URL.format(item_id=self.item_id)

    def option(self, stat_id: str) -> RollOption | None:
        for o in self.roll_pool:
            if o.stat_id == stat_id:
                return o
        return None


def _num(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM.search(str(value or ""))
    return float(m.group()) if m else 0.0


def parse_seed(html: str) -> dict[str, Any] | None:
    """Pull the hydration payload out of a rendered item page."""
    m = _SEED.search(html)
    if not m:
        return None
    try:
        blob = json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("shugo-seed present but not valid JSON")
        return None
    # The block is keyed by the route it hydrates; there is one entry.
    for value in blob.values():
        if isinstance(value, dict) and "id" in value:
            return value
    return None


def to_catalog_item(payload: dict[str, Any]) -> CatalogItem:
    pool: list[RollOption] = []
    for s in payload.get("subStats") or []:
        raw_min = s.get("minValue")
        raw_max = s.get("value")
        pool.append(
            RollOption(
                stat_id=s.get("id") or "",
                name=s.get("name") or "",
                min_value=_num(raw_min),
                max_value=_num(raw_max),
                percent="%" in str(raw_max or ""),
            )
        )

    return CatalogItem(
        item_id=int(payload.get("id") or 0),
        name=payload.get("name", ""),
        grade=payload.get("grade", ""),
        category=payload.get("categoryName", ""),
        equip_level=int(payload.get("equipLevel") or 0),
        max_enchant_level=int(payload.get("maxEnchantLevel") or 0),
        max_exceed_level=int(payload.get("maxExceedEnchantLevel") or 0),
        roll_pool=pool,
        sources=list(payload.get("sources") or []),
        magic_stone_slots=int(payload.get("magicStoneSlotCount") or 0),
        sub_stat_slots=int(payload.get("subStatCount") or 0),
        sub_skill_slots=int(payload.get("subSkillCountMax") or 0),
    )


class ShugoCatalog:
    """Cached, rate-limited reader for Shugo.GG item pages."""

    def __init__(
        self,
        db_path: str | Path,
        rate_limit_seconds: float = 2.0,
        timeout: float = 30.0,
        offline: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.rate_limit_seconds = max(rate_limit_seconds, 1.0)
        self.offline = offline
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._memo: dict[int, CatalogItem | None] = {}
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en",
            },
        )
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ShugoCatalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- cache -------------------------------------------------------------

    def _cached(self, item_id: int) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        try:
            with connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT payload FROM catalog_items WHERE item_id = ?",
                    (item_id,),
                ).fetchone()
        except sqlite3.DatabaseError:
            return None
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def _store(self, item_id: int, payload: dict[str, Any]) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO catalog_items (item_id, fetched_at, payload) "
                "VALUES (?,?,?)",
                (
                    item_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    # -- fetch -------------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            wait = self.rate_limit_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def item(self, item_id: int) -> CatalogItem | None:
        """Catalog entry for one item, from cache when possible.

        Returns None when the item is unknown to Shugo.GG or the network is
        unavailable. Callers must treat a missing entry as "no catalog data"
        rather than as an error -- the tracker works without it, just with
        less to say.
        """
        if item_id in self._memo:
            return self._memo[item_id]

        payload = self._cached(item_id)
        if payload is None:
            if self.offline:
                self._memo[item_id] = None
                return None
            payload = self._fetch(item_id)
            if payload is not None:
                self._store(item_id, payload)

        entry = to_catalog_item(payload) if payload else None
        self._memo[item_id] = entry
        return entry

    def _fetch(self, item_id: int) -> dict[str, Any] | None:
        # NOTE: the rendered PAGE, never /api/. See the module docstring.
        url = ITEM_PAGE.format(item_id=item_id)
        self._throttle()
        try:
            resp = self._http.get(url)
        except httpx.RequestError as exc:
            log.warning("shugo.gg unreachable for item %s: %s", item_id, exc)
            return None

        if resp.status_code == 404:
            log.info("shugo.gg has no item %s", item_id)
            return None
        if resp.status_code != 200:
            log.warning("shugo.gg returned %s for item %s", resp.status_code, item_id)
            return None

        payload = parse_seed(resp.text)
        if payload is None:
            log.warning("no shugo-seed block on the page for item %s", item_id)
        return payload

    def warm(self, item_ids: list[int], progress=None) -> int:
        """Pre-fetch a set of items, skipping any already cached."""
        missing = [i for i in item_ids if self._cached(i) is None]
        for n, item_id in enumerate(missing, start=1):
            self.item(item_id)
            if progress:
                progress("catalog", n, len(missing))
        return len(missing)

    def cached_count(self) -> int:
        if not self.db_path.exists():
            return 0
        try:
            with connect(self.db_path) as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM catalog_items").fetchone()
        except sqlite3.DatabaseError:
            return 0
        return int(row["n"]) if row else 0
