#!/usr/bin/env python3
"""Command line entry point for the Meta Ads historical backfill.

    python run_backfill.py check                    # validate token + account
    python run_backfill.py list                     # show available passes
    python run_backfill.py run --pass base          # the core dataset
    python run_backfill.py run --all --months 37    # everything, 3 years
    python run_backfill.py status                   # what has been extracted
"""

from __future__ import annotations

import argparse
import logging
import sys

from meta_backfill.api import InsightsClient, MetaApiError
from meta_backfill.checkpoint import Checkpoint
from meta_backfill.config import (
    DEFAULT_PASS_ORDER,
    PASSES,
    ConfigError,
    load_settings,
)
from meta_backfill.creatives import download_thumbnails, fetch_creatives
from meta_backfill.runner import MAX_MONTHS_BACK, month_windows, run_pass

ACCOUNT_STATUS = {
    1: "ACTIVE",
    2: "DISABLED",
    3: "UNSETTLED",
    7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD",
    100: "PENDING_CLOSURE",
    101: "CLOSED",
}


def configure_console() -> None:
    """Force UTF-8 on stdout/stderr.

    The Windows console defaults to cp1252, which cannot encode the accents in
    campaign names ("Saidia", "Tetouan", ...) nor the em dashes in log lines,
    and raises UnicodeEncodeError mid-run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = InsightsClient(settings)
    print(f"account   : {settings.account_id}")
    print(f"api       : {settings.api_version}")
    print(f"data dir  : {settings.data_dir.resolve()}")
    try:
        info = client.verify_access()
    except MetaApiError as exc:
        print(f"\nFAILED: {exc}")
        print(
            "\nThe token cannot read this ad account. Most likely the token was "
            "issued for a different account, or the account owner has not granted "
            "ads_read / ads_management to the app behind it."
        )
        return 1
    status = info.get("account_status")
    print("\nOK — token can read this account:")
    print(f"  name     : {info.get('name')}")
    print(f"  currency : {info.get('currency')}")
    print(f"  timezone : {info.get('timezone_name')}")
    print(f"  status   : {status} ({ACCOUNT_STATUS.get(status, 'unknown')})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    print(f"{'pass':<12} {'level':<9} {'breakdowns':<48} description")
    print("-" * 110)
    for name in DEFAULT_PASS_ORDER:
        cfg = PASSES[name]
        breakdowns = ", ".join(cfg.breakdowns) or "-"
        print(f"{cfg.name:<12} {cfg.level:<9} {breakdowns:<48} {cfg.description}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    checkpoint = Checkpoint(settings.checkpoint_path)
    totals = checkpoint.summary()
    if not totals:
        print("Nothing extracted yet.")
        return 0
    print(f"{'pass':<12} {'rows':>12}")
    print("-" * 25)
    for name, rows in sorted(totals.items()):
        print(f"{name:<12} {rows:>12,}")
    print("-" * 25)
    print(f"{'TOTAL':<12} {sum(totals.values()):>12,}")
    print(f"\nParquet root: {settings.raw_dir.resolve()}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()

    if args.all:
        selected = list(DEFAULT_PASS_ORDER)
    elif args.passes:
        selected = args.passes
    else:
        selected = ["base"]

    unknown = [name for name in selected if name not in PASSES]
    if unknown:
        print(f"Unknown pass(es): {', '.join(unknown)}", file=sys.stderr)
        print(f"Available: {', '.join(DEFAULT_PASS_ORDER)}", file=sys.stderr)
        return 2

    client = InsightsClient(settings)
    if not args.dry_run:
        try:
            info = client.verify_access()
        except MetaApiError as exc:
            print(f"Cannot read {settings.account_id}: {exc}", file=sys.stderr)
            print("Run `python run_backfill.py check` for details.", file=sys.stderr)
            return 1
        logging.info("account %s (%s) reachable", info.get("name"), settings.account_id)

    windows = month_windows(args.months)
    checkpoint = Checkpoint(settings.checkpoint_path)
    logging.info(
        "%s pass(es) x %s month(s): %s .. %s",
        len(selected), len(windows), windows[0].label, windows[-1].label,
    )

    grand_total = 0
    for name in selected:
        results = run_pass(
            client, settings, PASSES[name], windows, checkpoint,
            dry_run=args.dry_run,
        )
        extracted = sum(results.values())
        grand_total += extracted
        logging.info("pass %s complete — %s new row(s)", name, f"{extracted:,}")

    logging.info("done — %s new row(s) this run", f"{grand_total:,}")
    if not args.dry_run:
        print("\nNext: python build_warehouse.py")
    return 0


def cmd_creatives(args: argparse.Namespace) -> int:
    """Fetch ad copy, format and thumbnails for every ad."""
    import duckdb
    settings = load_settings()
    client = InsightsClient(settings)

    frame = fetch_creatives(client, settings)
    if frame.empty:
        print("No creatives returned.", file=sys.stderr)
        return 1

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    out = settings.data_dir / "creatives.parquet"
    frame.to_parquet(out, index=False)
    print(f"{len(frame):,} creative(s) -> {out}")

    # Only ads that actually delivered carry a performance signal worth joining.
    delivered: set[str] | None = None
    if settings.warehouse_path.exists():
        con = duckdb.connect(str(settings.warehouse_path), read_only=True)
        delivered = {
            r[0] for r in con.sql(
                "SELECT DISTINCT ad_id FROM ads_daily WHERE spend > 0"
            ).fetchall()
        }
        print(f"{len(delivered):,} ad(s) with delivery — restricting thumbnails to those")

    if not args.no_images:
        saved = download_thumbnails(frame, settings.data_dir / "thumbnails", only_ads=delivered)
        print(f"{len(saved):,} thumbnail(s) downloaded")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Meta Ads insight history into partitioned Parquet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify the token can read the ad account")
    sub.add_parser("list", help="list available extraction passes")
    sub.add_parser("status", help="show what has already been extracted")

    creatives = sub.add_parser("creatives", help="fetch ad copy, format and thumbnails")
    creatives.add_argument("--no-images", action="store_true",
                           help="metadata only, skip thumbnail download")

    run = sub.add_parser("run", help="run one or more extraction passes")
    run.add_argument(
        "--pass", dest="passes", action="append", metavar="NAME",
        help="pass to run (repeatable). Defaults to 'base'.",
    )
    run.add_argument("--all", action="store_true", help="run every pass, in order")
    run.add_argument(
        "--months", type=int, default=MAX_MONTHS_BACK,
        help=f"how many months back to go (max {MAX_MONTHS_BACK}, default %(default)s)",
    )
    run.add_argument(
        "--dry-run", action="store_true",
        help="print the requests that would be issued, call nothing",
    )
    return parser


def main() -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)

    handlers = {
        "check": cmd_check,
        "list": cmd_list,
        "status": cmd_status,
        "run": cmd_run,
        "creatives": cmd_creatives,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is checkpointed — rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
