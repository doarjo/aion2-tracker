"""Locating config files in both a source checkout and an installed package.

This exists because of a real bug: the original code derived the config
directory from the package file's grandparent, which is correct for a source
checkout but points at site-packages' parent once installed. In the container
that silently produced an empty config, which disabled every stat weight and
quietly downgraded every recommendation. Config loading now fails loudly.
"""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent

#: Conventional locations, highest priority first.
_CONFIG_DIR_CANDIDATES = (
    # A source checkout or editable install: src/aion2/ -> repo root.
    _PACKAGE_DIR.parents[1] / "config",
    _PACKAGE_DIR.parents[0] / "config",
    # The container layout.
    Path("/app/config"),
)


def candidate_config_dirs() -> list[Path]:
    """Every directory we will look in, in order, including env overrides."""
    out: list[Path] = []

    env_dir = os.environ.get("AION2_CONFIG_DIR", "").strip()
    if env_dir:
        out.append(Path(env_dir))

    # Whatever the process was launched from.
    out.append(Path.cwd() / "config")
    out.extend(_CONFIG_DIR_CANDIDATES)

    seen: set[Path] = set()
    unique: list[Path] = []
    for p in out:
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def find_config_file(filename: str = "thresholds.yaml") -> Path | None:
    """First existing `filename` across the candidate directories.

    `AION2_THRESHOLDS` names the thresholds file outright and wins over
    everything else.
    """
    if filename == "thresholds.yaml":
        explicit = os.environ.get("AION2_THRESHOLDS", "").strip()
        if explicit:
            p = Path(explicit)
            return p if p.exists() else None

    for directory in candidate_config_dirs():
        candidate = directory / filename
        if candidate.exists():
            return candidate
    return None


def project_root() -> Path:
    """Best guess at a writable project root, used for default data paths."""
    env_dir = os.environ.get("AION2_PROJECT_ROOT", "").strip()
    if env_dir:
        return Path(env_dir)

    checkout = _PACKAGE_DIR.parents[1]
    if (checkout / "pyproject.toml").exists():
        return checkout
    return Path.cwd()
