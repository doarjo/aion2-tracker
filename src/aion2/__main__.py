"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import db
from .client import Aion2Client, Aion2Error, CharacterRef
from .compare import compare as compare_builds
from .compare import detect_preset_change, skill_gaps
from .config import load_settings
from .fetch import snapshot_payloads
from .recommend import ConfigNotFound, Engine, UnknownProfile, load_config
from .replay import from_payloads, load_capture
from .shugo import ATTRIBUTION, ShugoCatalog

log = logging.getLogger("aion2")


def _force_utf8_output() -> None:
    """Windows consoles default to cp1252, which raises UnicodeEncodeError on
    the arrows and bullets in recommendation titles. Linux and macOS are
    already UTF-8, so this is a no-op there."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic terminals
            pass


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

    thresholds = load_config(cfg.thresholds_path or None, strict=False)
    scan = bool((thresholds.get("enhancement") or {}).get("scan_on_snapshot", True))
    if args.no_enhancements:
        scan = False

    est = 60 if scan else 32
    print(f"Snapshotting {cfg.character_name or ref.character_id} ...")
    print(
        f"About {est} requests at {cfg.rate_limit_seconds}s spacing, "
        f"so roughly {int(est * cfg.rate_limit_seconds / 60) + 1} minutes."
    )
    if scan:
        print("Includes an enhancement scan, which makes later audits offline.")

    last_stage = ""

    def progress(stage: str, done: int, total: int) -> None:
        nonlocal last_stage
        if stage != last_stage:
            print(f"  {stage} ...", flush=True)
            last_stage = stage
        if done == total:
            print(f"    {done}/{total} done", flush=True)

    with Aion2Client(rate_limit_seconds=cfg.rate_limit_seconds) as client:
        payloads = snapshot_payloads(
            client, ref, scan_enhancements=scan, progress=progress
        )

    char = from_payloads(payloads)
    snapshot_id = db.save_snapshot(
        cfg.db_path, char, region=cfg.region, character_id=cfg.character_id, raw=payloads
    )
    print(
        f"\nSaved snapshot #{snapshot_id}: {char.name}, CP "
        f"{char.combat_power:,}, item level {char.item_level:,}, "
        f"gear accuracy {char.gear_accuracy:g}, {len(char.wasted_lines)} wasted lines."
    )
    if char.has_enhancement_data:
        totals = char.total_enhancement_gain()
        top = ", ".join(
            f"{k} +{v:g}" for k, v in
            sorted(totals.items(), key=lambda kv: -kv[1])[:3]
        )
        print(
            f"Enhancement headroom: {char.enhancement_levels_available} levels "
            f"available ({top})."
        )
    print(f"Database: {cfg.db_path}")
    return 0


def _print_audit(char, engine: Engine, limit: int | None, catalog=None) -> None:
    recs = engine.audit(char, catalog)
    print(f"\n{char.name} -- {char.class_name} {char.level}, "
          f"CP {char.combat_power:,}, item level {char.item_level:,}")
    print(f"Goal: {engine.profile_label}")
    print(f"Gear Accuracy {char.gear_accuracy:g} "
          f"(components only, not an in-game total)")
    print(f"{len(char.wasted_lines)} wasted stat lines")
    print("Reflects your ACTIVE preset only; PLAYNC exposes no others.")
    if char.has_enhancement_data:
        print(f"{char.enhancement_levels_available} enhancement levels available")
    else:
        print("No enhancement data in this snapshot "
              "(re-run 'aion2 snapshot' to collect it)")
    print()
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


def cmd_profiles(args: argparse.Namespace) -> int:
    cfg = load_settings(args.env_file)
    thresholds = load_config(args.thresholds or cfg.thresholds_path or None)
    default = thresholds.get("default_profile", "")

    print("Goal profiles. Pick one with --profile on audit or replay.\n")
    for name, body in (thresholds.get("profiles") or {}).items():
        mark = "  (default)" if name == default else ""
        print(f"  {name}{mark}")
        print(f"      {body.get('label', name)}")
        desc = " ".join((body.get("description") or "").split())
        if desc:
            print(f"      {desc}")
        print()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    cfg = load_settings(args.env_file)
    engine = Engine(
        load_config(args.thresholds or cfg.thresholds_path or None),
        profile=args.profile,
    )

    row = db.latest_snapshot(cfg.db_path, cfg.character_id or None)
    if row is None:
        print("No snapshot found. Run 'aion2 snapshot' first.", file=sys.stderr)
        return 1

    import json

    char = from_payloads(json.loads(row["payload"] or "{}"))
    print(f"Snapshot taken {row['taken_at']}")

    # Offline: use whatever the catalog has already cached, never fetch
    # during an audit. `aion2 catalog` is the explicit fetch step.
    with ShugoCatalog(cfg.db_path, offline=True) as cat:
        catalog = cat if cat.cached_count() else None
        if catalog is None:
            print("No item roll pools cached. Run 'aion2 catalog' to add them.")
        _print_audit(char, engine, None if args.all else 15, catalog)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Audit a capture directory without touching the network."""
    engine = Engine(load_config(args.thresholds), profile=args.profile)
    char = load_capture(args.directory)
    _print_audit(char, engine, None if args.all else 15)
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    """Fetch roll pools for equipped items from Shugo.GG.

    Reads their server-rendered item pages, which their llms.txt explicitly
    offers to agents. Never /api/, which their robots.txt disallows.
    """
    cfg = load_settings(args.env_file)
    row = db.latest_snapshot(cfg.db_path, cfg.character_id or None)
    if row is None:
        print("No snapshot yet. Run 'aion2 snapshot' first.", file=sys.stderr)
        return 1

    import json as _json

    char = from_payloads(_json.loads(row["payload"] or "{}"))
    ids = sorted({it.item_id for it in char.items if it.item_id})

    with ShugoCatalog(cfg.db_path, rate_limit_seconds=args.rate_limit) as cat:
        have = cat.cached_count()
        print(f"{len(ids)} equipped items, {have} already cached.")
        if args.refresh:
            print("Refreshing every entry.")
            with db.connect(cfg.db_path) as conn:
                conn.execute("DELETE FROM catalog_items")

        def progress(_stage: str, done: int, total: int) -> None:
            print(f"  fetching {done}/{total}", flush=True)

        fetched = cat.warm(ids, progress=progress)
        print(f"\nFetched {fetched} new. Cache now holds {cat.cached_count()}.")

        missing = [i for i in ids if cat.item(i) is None]
        if missing:
            print(f"{len(missing)} not found on Shugo.GG: {missing}")

    print(f"\n{ATTRIBUTION}. https://shugo.gg")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Measure your build against another character's.

    Fetches one named character you asked about. Not bulk collection.
    """
    import json as _json

    cfg = load_settings(args.env_file)
    engine = Engine(
        load_config(args.thresholds or cfg.thresholds_path or None),
        profile=args.profile,
    )

    row = db.latest_snapshot(cfg.db_path, cfg.character_id or None)
    if row is None:
        print("No snapshot of your own character yet. Run 'aion2 snapshot'.",
              file=sys.stderr)
        return 1
    mine = from_payloads(_json.loads(row["payload"] or "{}"))

    cache = Path(cfg.db_path).parent / "reference" / f"{args.name.lower()}.json"
    if cache.exists() and not args.refresh:
        print(f"Using cached capture of {args.name}. --refresh to re-fetch.")
        payloads = _json.loads(cache.read_text(encoding="utf-8"))
    else:
        region = args.region or cfg.region
        with Aion2Client(rate_limit_seconds=cfg.rate_limit_seconds) as client:
            ref = client.resolve(args.name, region, args.server_id or cfg.server_id)
            print(f"Capturing {ref.name}, level {ref.level} on {ref.server_name}.")
            print("About 60 rate-limited requests, so roughly four minutes.")

            def prog(stage: str, done: int, total: int) -> None:
                if done == total:
                    print(f"  {stage} {done}/{total}", flush=True)

            payloads = snapshot_payloads(client, ref, progress=prog)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(_json.dumps(payloads, ensure_ascii=False), encoding="utf-8")

    theirs = from_payloads(payloads)
    result = compare_builds(mine, theirs, engine)

    print()
    print(f"{mine.name} vs {theirs.name}  --  {engine.profile_label}")
    print("Both sides show whichever preset was ACTIVE when captured.")
    print("=" * 66)
    print(f"{'':<26}{mine.name:>13}{theirs.name:>13}{'gap':>13}")
    for g in result.gaps:
        arrow = "behind" if g.behind else ("ahead" if g.delta else "level")
        print(
            f"{g.dimension:<26}{g.format(g.mine):>13}{g.format(g.theirs):>13}"
            f"{arrow:>13}"
        )

    behind = result.behind_on
    if behind:
        print()
        print("Biggest gaps, proportionally:")
        for g in behind[:4]:
            print(f"  {g.dimension}: {g.format(g.mine)} vs {g.format(g.theirs)}")
            if g.note:
                print(f"      {g.note}")

    gaps = skill_gaps(mine, theirs, engine)
    if gaps:
        print()
        print("Key skills where they are ahead:")
        for name, a, b in gaps:
            print(f"  {name:<22} {a} vs {b}")

    if result.notes:
        print()
        for n in result.notes:
            print(f"  {n}")

    ahead = result.ahead_on
    if ahead:
        print()
        print("Where you are ahead: " + ", ".join(g.dimension for g in ahead))

    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show how a tracked number has moved across snapshots.

    This is the honest measurement of Combat Power: the engine ranks by a
    heuristic, but what actually happened is whatever the next snapshot says.
    """
    cfg = load_settings(args.env_file)
    rows = db.history(cfg.db_path, args.stat, args.days)
    if not rows:
        print(f"No snapshots in the last {args.days} days.")
        return 1

    print(f"\n{args.stat} over the last {args.days} days\n")
    values = [r["value"] for r in rows]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1

    prev = None
    for r in rows:
        v = r["value"]
        bar = "#" * int(1 + 34 * (v - lo) / span)
        delta = ""
        if prev is not None:
            d = v - prev
            if d:
                delta = f"  {d:+,.0f}"
        print(f"  {r['taken_at'][:16]}  {v:>12,.0f}  {bar}{delta}")
        prev = v

    first, last = values[0], values[-1]
    change = last - first
    print(f"\n  {len(rows)} snapshots, net change {change:+,.0f}")
    if change and first:
        print(f"  {100.0 * change / first:+.2f}% from the first snapshot")
    return 0


def _warn_about_preset_switches(cfg) -> None:
    """A big one-step gear change is more likely a preset switch than
    progress, and reads as a bug in the history if unexplained."""
    import json as _json

    with db.connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT taken_at, payload FROM snapshots "
            "WHERE character_id = ? ORDER BY taken_at",
            (cfg.character_id,),
        ).fetchall()

    chars = []
    for r in rows:
        try:
            chars.append((r["taken_at"], from_payloads(_json.loads(r["payload"]))))
        except Exception:  # noqa: BLE001 - a bad row must not break history
            continue

    for (_t0, a), (t1, b) in zip(chars, chars[1:]):
        change = detect_preset_change(a, b)
        if change.likely:
            print(
                f"\n  NOTE {t1[:16]}: {change.slots_changed} of "
                f"{change.slots_total} slots changed item at once "
                f"({change.cp_delta:+,} CP). That looks like a preset "
                f"switch rather than progression."
            )


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
    s.add_argument(
        "--no-enhancements",
        action="store_true",
        help="skip the enhancement scan, halving request count but leaving "
             "the audit without upgrade figures",
    )
    s.set_defaults(func=cmd_snapshot)

    a = sub.add_parser("audit", help="recommendations from the latest snapshot")
    a.add_argument("--all", action="store_true", help="show every recommendation")
    a.add_argument("--profile", default=None,
                   help="goal profile, see 'aion2 profiles'")
    a.add_argument("--thresholds", default=None)
    a.set_defaults(func=cmd_audit)

    r = sub.add_parser("replay", help="audit a capture directory offline")
    r.add_argument("directory", type=Path)
    r.add_argument("--all", action="store_true")
    r.add_argument("--profile", default=None)
    r.add_argument("--thresholds", default=None)
    r.set_defaults(func=cmd_replay)

    p_ = sub.add_parser("profiles", help="list the available goal profiles")
    p_.add_argument("--thresholds", default=None)
    p_.set_defaults(func=cmd_profiles)

    cat_ = sub.add_parser(
        "catalog",
        help="fetch item roll pools from Shugo.GG for your equipped items",
    )
    cat_.add_argument("--refresh", action="store_true", help="re-fetch everything")
    cat_.add_argument("--rate-limit", type=float, default=2.0)
    cat_.set_defaults(func=cmd_catalog)

    cmp_ = sub.add_parser(
        "compare", help="measure your build against another character"
    )
    cmp_.add_argument("name")
    cmp_.add_argument("--region", default=None)
    cmp_.add_argument("--server-id", type=int, default=None)
    cmp_.add_argument("--profile", default=None)
    cmp_.add_argument("--thresholds", default=None)
    cmp_.add_argument("--refresh", action="store_true",
                      help="re-fetch rather than using the cached capture")
    cmp_.set_defaults(func=cmd_compare)

    h = sub.add_parser("history", help="how a tracked number moved over time")
    h.add_argument(
        "stat",
        nargs="?",
        default="combat_power",
        choices=["combat_power", "item_level", "gear_accuracy",
                 "wasted_lines", "level"],
    )
    h.add_argument("--days", type=int, default=90)
    h.set_defaults(func=cmd_history)

    w = sub.add_parser("serve", help="run the read-only dashboard")
    w.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except UnknownProfile as exc:
        print(f"\n{exc}", file=sys.stderr)
        print("Run 'aion2 profiles' to list them.", file=sys.stderr)
        return 2
    except ConfigNotFound as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except Aion2Error as exc:
        print(f"\nPLAYNC error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
