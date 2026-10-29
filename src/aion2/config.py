"""Environment-driven configuration.

Everything comes from env vars so the same image runs locally and on Unraid
with nothing but a different .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .client import VALID_REGIONS, CharacterRef
from .paths import find_config_file, project_root

PROJECT_ROOT = project_root()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    region: str = "eu"
    server_id: int = 0
    character_id: str = ""
    character_name: str = ""
    rate_limit_seconds: float = 4.0
    snapshot_cooldown_minutes: int = 20
    snapshot_interval_hours: int = 6
    db_path: str = "./data/aion2.db"
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_auth_token: str = ""
    #: Hostnames the MCP endpoint will answer to. The SDK rejects any
    #: other Host header as DNS rebinding, which bites the moment the
    #: service sits behind a tunnel presenting its own hostname.
    mcp_allowed_hosts: tuple[str, ...] = ()
    thresholds_path: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.character_id and self.server_id and self.region)

    def ref(self) -> CharacterRef:
        if not self.configured:
            raise RuntimeError(
                "Character is not configured. Set AION2_REGION, AION2_SERVER_ID and "
                "AION2_CHARACTER_ID in .env -- resolve them with "
                "scripts/discover.ps1."
            )
        return CharacterRef(
            region=self.region,
            server_id=self.server_id,
            character_id=self.character_id,
            name=self.character_name,
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.region and self.region not in VALID_REGIONS:
            problems.append(
                f"AION2_REGION={self.region!r} is not one of {VALID_REGIONS}"
            )
        if self.character_id and "=" in self.character_id and "%3D" not in self.character_id:
            problems.append(
                "AION2_CHARACTER_ID looks URL-decoded. It must stay percent-encoded "
                "exactly as the search endpoint returned it, e.g. ending in %3D."
            )
        if self.rate_limit_seconds < 2:
            problems.append(
                f"AION2_RATE_LIMIT_SECONDS={self.rate_limit_seconds} is below the "
                "2s floor this project commits to."
            )
        return problems


def load_settings(env_file: str | Path | None = None) -> Settings:
    path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=False)

    found = find_config_file("thresholds.yaml")

    return Settings(
        region=_env("AION2_REGION", "eu").lower(),
        server_id=_env_int("AION2_SERVER_ID", 0),
        character_id=_env("AION2_CHARACTER_ID"),
        character_name=_env("AION2_CHARACTER_NAME"),
        rate_limit_seconds=_env_float("AION2_RATE_LIMIT_SECONDS", 4.0),
        snapshot_cooldown_minutes=_env_int("AION2_SNAPSHOT_COOLDOWN_MINUTES", 20),
        snapshot_interval_hours=_env_int("SNAPSHOT_INTERVAL_HOURS", 6),
        db_path=_env("AION2_DB_PATH", str(PROJECT_ROOT / "data" / "aion2.db")),
        host=_env("AION2_HOST", "0.0.0.0"),
        port=_env_int("AION2_PORT", 8080),
        mcp_auth_token=_env("MCP_AUTH_TOKEN"),
        mcp_allowed_hosts=tuple(
            h.strip() for h in _env("MCP_ALLOWED_HOSTS").split(",") if h.strip()
        ),
        # Empty means "search the conventional locations" rather than a guess
        # that may not exist. See paths.find_config_file.
        thresholds_path=_env("AION2_THRESHOLDS") or str(found or ""),
    )
