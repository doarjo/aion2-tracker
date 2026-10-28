"""The only module that talks HTTP to PLAYNC.

Every endpoint is unauthenticated public game data. Read-only, rate limited,
one character at a time. If PLAYNC changes anything, it breaks here and only
here -- that is the point of this module.

Endpoint reference and evidence: docs/endpoints.md
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

SEARCH_BASE = "https://api-search.plaync.com/aion2global/search/v2/character"
API_BASE = "https://aion2.plaync.com/api/character"
GAMEINFO_BASE = "https://aion2.plaync.com/en-us/api/gameinfo"

# A realistic browser UA. The API does not require it, but the brief asks for
# one and it keeps us honest about what we are.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

VALID_REGIONS = ("nae", "naw", "eu", "la", "as")


class Aion2Error(RuntimeError):
    """Any failure talking to PLAYNC, with enough context to debug it."""


class CharacterNotFound(Aion2Error):
    pass


@dataclass(frozen=True, slots=True)
class CharacterRef:
    """Identifies one character. `character_id` is percent-encoded as the
    search endpoint returned it and MUST stay that way."""

    region: str
    server_id: int
    character_id: str
    name: str = ""
    server_name: str = ""
    level: int = 0

    def query(self) -> str:
        # Built by hand on purpose. Passing character_id through a urlencode
        # helper double-encodes the %3D and the API answers 404.
        return (
            f"region={self.region}&lang=en-US"
            f"&characterId={self.character_id}&serverId={self.server_id}"
        )


class Aion2Client:
    """Rate-limited, retrying, read-only PLAYNC client."""

    def __init__(
        self,
        rate_limit_seconds: float = 4.0,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.rate_limit_seconds = max(rate_limit_seconds, 1.0)
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://aion2.plaync.com/",
            },
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Aion2Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- core --------------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            wait = self.rate_limit_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _get_json(self, url: str) -> Any:
        """GET a pre-built URL. The URL is passed through verbatim so that an
        already percent-encoded characterId is not encoded a second time."""
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                # httpx.URL(..., ) on a raw string preserves existing
                # percent-encoding in the query.
                resp = self._http.get(httpx.URL(url))
            except httpx.RequestError as exc:
                last_error = exc
                log.warning("network error on %s (attempt %d): %s", url, attempt, exc)
            else:
                if resp.status_code == 404:
                    raise CharacterNotFound(
                        f"404 from PLAYNC. The endpoint or the character does not "
                        f"exist. Check region/serverId/characterId, and remember the "
                        f"characterId must stay percent-encoded.\nURL: {url}"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = Aion2Error(f"HTTP {resp.status_code}")
                    log.warning(
                        "HTTP %s on %s (attempt %d)", resp.status_code, url, attempt
                    )
                else:
                    resp.raise_for_status()
                    ctype = resp.headers.get("Content-Type", "")
                    if "json" not in ctype:
                        # The site serves its Korean error page as HTTP 200.
                        raise Aion2Error(
                            f"Expected JSON but got {ctype!r}. PLAYNC serves its "
                            f"error page with status 200, so this usually means a "
                            f"wrong path.\nURL: {url}"
                        )
                    return resp.json()

            if attempt < self.max_retries:
                backoff = self.rate_limit_seconds * (2 ** (attempt - 1))
                log.info("backing off %.1fs", backoff)
                time.sleep(backoff)

        raise Aion2Error(f"Giving up after {self.max_retries} attempts: {url}") from last_error

    # -- endpoints ---------------------------------------------------------

    def search(
        self, name: str, region: str, server_id: int | None = None, size: int = 20
    ) -> list[CharacterRef]:
        """Find characters by name. Matching is case-insensitive substring, so
        'Kira' also returns 'Akira'. An empty list is a valid answer, not an
        error."""
        if region not in VALID_REGIONS:
            raise ValueError(f"region must be one of {VALID_REGIONS}, got {region!r}")

        url = (
            f"{SEARCH_BASE}?keyword={_quote(name)}&region={region}"
            f"&localeInfo=en-US&page=1&size={size}"
        )
        if server_id is not None:
            url += f"&serverId={server_id}"

        payload = self._get_json(url)
        out: list[CharacterRef] = []
        for entry in payload.get("list") or []:
            out.append(
                CharacterRef(
                    region=entry.get("region", region),
                    server_id=int(entry["serverId"]),
                    # Keep verbatim. It arrives percent-encoded.
                    character_id=entry["characterId"],
                    name=_strip_highlight(entry.get("name", "")),
                    server_name=entry.get("serverName", ""),
                    level=int(entry.get("level") or 0),
                )
            )
        return out

    def resolve(self, name: str, region: str, server_id: int | None = None) -> CharacterRef:
        """Search and pick the best match: exact name first, then highest level.

        Search results are not stably ordered, so never trust position.
        """
        hits = self.search(name, region, server_id)
        if not hits:
            raise CharacterNotFound(
                f"No character matched {name!r} in region {region!r}"
                + (f" on server {server_id}" if server_id else "")
            )
        exact = [h for h in hits if h.name.casefold() == name.casefold()]
        pool = exact or hits
        return max(pool, key=lambda h: h.level)

    def info(self, ref: CharacterRef) -> dict[str, Any]:
        """Profile, base + god stats, titles, Daevanion board summary."""
        return self._get_json(f"{API_BASE}/info?{ref.query()}")

    def equipment(self, ref: CharacterRef) -> dict[str, Any]:
        """Equipment summary, skins, pet/wing, and the skill list."""
        return self._get_json(f"{API_BASE}/equipment?{ref.query()}")

    def item(
        self, ref: CharacterRef, item_id: int, enchant_level: int, slot_pos: int
    ) -> dict[str, Any]:
        """Full detail for one equipped item.

        `enchant_level` is a free parameter: pass a level the character has NOT
        reached and PLAYNC recalculates the stats for it. That is what makes
        "upgrade this to +N" exactly computable without any item catalog.
        """
        return self._get_json(
            f"{API_BASE}/equipment/item?{ref.query()}"
            f"&id={item_id}&enchantLevel={enchant_level}&slotPos={slot_pos}"
        )

    def daevanion(self, ref: CharacterRef, board_id: int) -> dict[str, Any]:
        """One Daevanion board. Board ids are class-specific -- always read them
        from info()['daevanion']['boardList'], never hardcode."""
        return self._get_json(f"{API_BASE}/daevanion/detail?{ref.query()}&boardId={board_id}")

    # -- game data (no character required) ---------------------------------

    def classes(self) -> dict[str, Any]:
        return self._get_json(f"{GAMEINFO_BASE}/pcdata?lang=en")

    def servers(self, region: str = "nae") -> dict[str, Any]:
        return self._get_json(f"{GAMEINFO_BASE}/servers?lang=en&region={region}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


def _quote(text: str) -> str:
    """Percent-encode a free-text query value."""
    out = []
    for byte in text.encode("utf-8"):
        ch = chr(byte)
        out.append(ch if ch in _SAFE else f"%{byte:02X}")
    return "".join(out)


def _strip_highlight(name: str) -> str:
    """Search wraps the matched substring in <strong> tags."""
    out = []
    depth = 0
    for ch in name:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()
