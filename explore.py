#!/usr/bin/env python3
"""Browse and export the extracted dataset.

Parquet is a binary columnar format — fast to query, impossible to open by
double-clicking. This gives three ways in:

    python explore.py views                     # what exists, and how big
    python explore.py peek ads_daily            # first rows of a view
    python explore.py peek ads_by_placement -n 30
    python explore.py sql "SELECT ..."          # any SQL you like
    python explore.py export                    # every view to CSV
    python explore.py export --view ads_daily   # just one
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

from meta_backfill.config import ConfigError, load_settings


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def show(frame: pd.DataFrame, *, max_rows: int = 50) -> None:
    with pd.option_context(
        "display.max_columns", None,
        "display.width", 250,
        "display.max_colwidth", 32,
        "display.max_rows", max_rows,
    ):
        print(frame.to_string(index=False))


def list_views(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [
        r[0]
        for r in con.sql(
            "SELECT table_name FROM information_schema.tables ORDER BY table_name"
        ).fetchall()
    ]


def cmd_views(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    rows = []
    for name in list_views(con):
        try:
            n = con.sql(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            cols = con.sql(f'SELECT * FROM "{name}" LIMIT 0').df().shape[1]
        except duckdb.Error as exc:
            rows.append({"view": name, "rows": "-", "columns": "-", "note": str(exc)[:40]})
            continue
        rows.append({"view": name, "rows": f"{n:,}", "columns": cols, "note": ""})
    show(pd.DataFrame(rows))
    return 0


def cmd_peek(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    if args.view not in list_views(con):
        print(f"Unknown view: {args.view}", file=sys.stderr)
        print(f"Available: {', '.join(list_views(con))}", file=sys.stderr)
        return 1
    frame = con.sql(f'SELECT * FROM "{args.view}" LIMIT {args.n}').df()
    print(f"{args.view} — first {len(frame)} row(s), {frame.shape[1]} columns\n")
    show(frame, max_rows=args.n)
    return 0


def cmd_sql(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    try:
        frame = con.sql(args.query).df()
    except duckdb.Error as exc:
        print(f"Query failed: {exc}", file=sys.stderr)
        return 1
    show(frame, max_rows=args.n)
    print(f"\n{len(frame):,} row(s)")
    return 0


def cmd_export(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [args.view] if args.view else list_views(con)
    for name in targets:
        frame = con.sql(f'SELECT * FROM "{name}"').df()
        # actions_raw carries Meta's full JSON payload per row. It is the
        # escape hatch for programmatic use and accounts for ~85% of the file
        # size, so browsing exports drop it unless asked for.
        if not args.full and "actions_raw" in frame.columns:
            frame = frame.drop(columns=["actions_raw"])
        path = out_dir / f"{name}.csv"
        # utf-8-sig so Excel opens accented campaign names correctly.
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"  {path}  ({len(frame):,} rows, {path.stat().st_size / 1e6:.1f} MB)")

    print(f"\nExported to {out_dir.resolve()}")
    return 0


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("views", help="list views with row counts")

    peek = sub.add_parser("peek", help="show the first rows of a view")
    peek.add_argument("view")
    peek.add_argument("-n", type=int, default=15, help="rows to show (default %(default)s)")

    sql = sub.add_parser("sql", help="run an arbitrary SQL query")
    sql.add_argument("query")
    sql.add_argument("-n", type=int, default=50, help="rows to display")

    export = sub.add_parser("export", help="write views to CSV")
    export.add_argument("--view", help="only this view (default: all)")
    export.add_argument("--out", type=Path, default=Path("export"), help="output folder")
    export.add_argument(
        "--full", action="store_true",
        help="keep the actions_raw JSON column (much larger files)",
    )

    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not settings.warehouse_path.exists():
        print(f"No warehouse at {settings.warehouse_path}.", file=sys.stderr)
        print("Run `python build_warehouse.py` first.", file=sys.stderr)
        return 1

    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    handlers = {
        "views": cmd_views,
        "peek": cmd_peek,
        "sql": cmd_sql,
        "export": cmd_export,
    }
    return handlers[args.command](con, args)


if __name__ == "__main__":
    raise SystemExit(main())
