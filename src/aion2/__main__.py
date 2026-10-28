"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import db
from .client import Aion2Client, Aion2Error
from .config import load_settings
from .fetch import snapshot_payloads
from .recommend import Engine, load_config
from .replay import from_payloads, load_capture

log = logging.getLogger("aion2")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    """Resolve a character name to the ids needed for .env."""
    with Aion2Client(rate_limit_seconds=args.rate_limit) as client:
        hits = client.search(args.name, args.region, args.server_id)

    if not hits:
        print(f"No character matched {args.name!r} in region {args.region!r}.")
        print("Matching is case-insensitive substring. serverId ranges per region:")
        print("  nae 11xx/21xx | naw 12xx/22xx | eu 13xx/23xx | la 14xx/24xx | as 15xx/25xx")
        return 1

    print(f"{len(hits)} match(es):\n")
    for h in sorted(hits, key=lambda x: -x.level):
        print(f"  {h.name:<20} lvl {h.level:<4} {h.server_name} ({h.server_id})")

    exact = [h for h in hits if h.name.casefold() == args.name.casefold()]
    best = max(exact or hits, key=lambda h: h.level)
    print("\nBest match -> .env values:\n")
    print(f"  AION2_REGION={best.region}")
    print(f"  AION2_SERVER_ID={best.server_id}")
    print(f"  AION2_CHARACTER_ID={best.character_id}")
    print(f"  AION2_CHARACTER_NAME={best.name}")
    print("\nKeep AION2_CHARACTER_ID exactly as shown, including any %3D.")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = load_settings(args.env_file)
    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"config: {p}", file=sys.stderr)
    ref = cfg.ref()

    if not args.force:
        age = db.seconds_since_last_snapshot(cfg.db_path, cfg.character_id)
        cooldown = cfg.snapshot_cooldown_minutes * 60
        if age is not None and age < cooldown:
            print(
                f"Last snapshot was {int(age)}s ago, cooldown is {int(cooldown)}s. "
                f"Use --force to override."
            )
            return 1

    print(f"Snapshotting {cfg.character_name or ref.character_id} ...")
    print(f"About 30 requests at {cfg.rate_limit_seconds}s spacing, so ~2 minutes.")

    with Aion2Client(rate_limit_seconds=cfg.rate_limit_seconds) as client:
        payloads = snapshot_payloads(client, ref)

    char = from_payloads(payloads)
    snapshot_id = db.save_snapshot(
        cfg.db_path, char, region=cfg.region, character_id=cfg.character_id, raw=payloads
    )
    print(
        f"\nSaved snapshot #{snapshot_id}: {char.name}, CP "
        f"{char.combat_power:,}, item level {char.item_level:,}, "
        f"gear accuracy {char.gear_accuracy:g}, {len(char.wasted_lines)} wasted lines."
    )
    print(f"Database: {cfg.db_path}")
    return 0


def _print_audit(char, engine: Engine, limit: int | None) -> None:
    recs = engine.audit(char)
    print(f"\n{char.name} -- {char.class_name} {char.level}, "
          f"CP {char.combat_power:,}, item level {char.item_level:,}")
    print(f"Gear Accuracy {char.gear_accuracy:g} "
          f"(components only, not an in-game total)")
    print(f"{len(char.wasted_lines)} wasted stat lines\n")
    print(f"{len(recs)} recommendations, highest priority first:\n")

    shown = recs[:limit] if limit else recs
    for i, r in enumerate(shown, 1):
        print(f"{i:>3}. [{r.priority.label:<8}] {r.title}")
        print(f"      {r.detail}")
        if r.gain_summary:
            print(f"      GAIN: {r.gain_summary}")
        print()
    if limit and len(recs) > limit:
        print(f"... and {len(recs) - limit} more. Use --all to see everything.")


def cmd_audit(args: argparse.Namespace) -> int:
    cfg = load_settings(args.env_file)
    engine = Engine(load_config(args.thresholds or cfg.thresholds_path or None))

    row = db.latest_snapshot(cfg.db_path, cfg.character_id or None)
    if row is None:
        print("No snapshot found. Run 'aion2 snapshot' first.", file=sys.stderr)
        return 1

    import json

    char = from_payloads(json.loads(row["payload"] or "{}"))
    print(f"Snapshot taken {row['taken_at']}")
    _print_audit(char, engine, None if args.all else 15)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Audit a capture directory without touching the network."""
    engine = Engine(load_config(args.thresholds))
    char = load_capture(args.directory)
    _print_audit(char, engine, None if args.all else 15)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    cfg = load_settings(args.env_file)
    from .web import create_app

    app = create_app(cfg)
    print(f"Dashboard on http://{cfg.host}:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aion2", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--env-file", default=None, help="path to .env")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="resolve a character name to .env values")
    d.add_argument("name")
    d.add_argument("--region", default="eu", help="nae naw eu la as")
    d.add_argument("--server-id", type=int, default=None)
    d.add_argument("--rate-limit", type=float, default=4.0)
    d.set_defaults(func=cmd_discover)

    s = sub.add_parser("snapshot", help="fetch and store a snapshot")
    s.add_argument("--force", action="store_true", help="ignore the cooldown")
    s.set_defaults(func=cmd_snapshot)

    a = sub.add_parser("audit", help="recommendations from the latest snapshot")
    a.add_argument("--all", action="store_true", help="show every recommendation")
    a.add_argument("--thresholds", default=None)
    a.set_defaults(func=cmd_audit)

    r = sub.add_parser("replay", help="audit a capture directory offline")
    r.add_argument("directory", type=Path)
    r.add_argument("--all", action="store_true")
    r.add_argument("--thresholds", default=None)
    r.set_defaults(func=cmd_replay)

    w = sub.add_parser("serve", help="run the read-only dashboard")
    w.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except Aion2Error as exc:
        print(f"\nPLAYNC error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
