#!/usr/bin/env python3
"""Rank ad segments by conversion efficiency and size the reallocation upside.

    python run_segment_analysis.py
    python run_segment_analysis.py --dimension age_gender --dimension placement
    python run_segment_analysis.py --conversion add_to_cart --report report.md

Every scan is corrected for overdispersion and for the multiplicity of testing
many segments at once, so a "significant" flag here means the segment survived
a false-discovery-rate screen rather than merely clearing p < 0.05.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

from analysis import reallocation
from analysis.segments import (
    DEFAULT_CONVERSION,
    SEGMENT_SPECS,
    SegmentAnalysis,
    analyse,
    available_views,
)
from meta_backfill.config import ConfigError, load_settings


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def format_analysis(result: SegmentAnalysis, *, top: int) -> str:
    spec = result.spec
    lines: list[str] = []
    lines.append(f"## {spec.name} — {spec.description}")
    lines.append("")
    if result.frame.empty:
        lines.append("_No segment large enough to test._")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"Account rate: **{result.global_rate:.4f} conv/$** "
        f"({result.total_conversions:,.0f} conversions over {result.total_spend:,.0f} $) · "
        f"dispersion phi = {result.dispersion:.1f} · "
        f"{len(result.frame)} segments tested"
    )
    if not result.excluded.empty:
        lines.append(
            f"_{len(result.excluded)} segment(s) too small to test "
            f"({result.excluded['spend'].sum():,.0f} $ of spend)._"
        )
    lines.append("")

    view = result.frame.copy()
    display = pd.DataFrame({
        "segment": view["segment"],
        "spend": view["spend"].round(0),
        "conv": view["conversions"].round(0),
        "cost/conv": view["cost_per_conv"].round(2),
        "CI 95%": view.apply(
            lambda r: f"{r['cpc_ci_low']:.2f}–{r['cpc_ci_high']:.2f}"
            if pd.notna(r["cpc_ci_low"]) else "—", axis=1
        ),
        "index": view["perf_index"].round(2),
        "q": view["q_value"].map(lambda v: f"{v:.3g}"),
        "sig": view["significant"].map({True: "yes", False: ""}),
    })
    head = display.head(top)
    tail = display.tail(top) if len(display) > 2 * top else None

    lines.append("```")
    lines.append(head.to_string(index=False))
    if tail is not None:
        lines.append("   ...")
        lines.append(tail.to_string(index=False, header=False))
    lines.append("```")
    lines.append("")

    if not spec.allow_reallocation:
        lines.append(
            "_Ranking only: this breakdown exposes no pixel conversion, so the "
            "outcome is a proxy and no budget recommendation is derived from it._"
        )
        lines.append("")
        return "\n".join(lines)

    sim = reallocation.simulate(result.frame)
    if sim is None:
        lines.append("_No statistically separated donor/recipient pair — nothing to reallocate._")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"**Reallocation.** Moving {sim.freed_spend:,.0f} $ out of "
        f"{len(sim.donors)} under-performing segment(s) into "
        f"{len(sim.recipients)} winner(s):"
    )
    lines.append("")
    lines.append(
        f"- linear ceiling: {sim.linear_conversions:,.0f} conversions "
        f"({sim.linear_gain:+,.0f}, {sim.linear_gain_pct:+.1f}%)"
    )
    lines.append(
        f"- with saturation (alpha={sim.saturation}): {sim.saturated_conversions:,.0f} "
        f"({sim.saturated_gain:+,.0f}, {sim.saturated_gain_pct:+.1f}%)"
    )
    lines.append("")
    worst = sim.donors.head(3)[["segment", "spend", "conversions", "cost_per_conv"]]
    lines.append("Largest donors:")
    lines.append("```")
    lines.append(worst.round(2).to_string(index=False))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dimension", dest="dimensions", action="append", metavar="NAME",
        choices=sorted(SEGMENT_SPECS), help="segment family (repeatable); default: all available",
    )
    parser.add_argument("--conversion", default=DEFAULT_CONVERSION,
                        help="conversion column (default: %(default)s)")
    parser.add_argument("--min-expected", type=float, default=5.0,
                        help="minimum expected conversions to test a segment")
    parser.add_argument("--min-spend-share", type=float, default=0.002,
                        help="minimum share of total spend to test a segment")
    parser.add_argument("--fdr", type=float, default=0.05, help="target false discovery rate")
    parser.add_argument("--boot", type=int, default=2000, help="bootstrap resamples")
    parser.add_argument("--top", type=int, default=10, help="rows shown per table")
    parser.add_argument("--report", type=Path, help="also write the report to this markdown file")
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
    views = available_views(con)

    selected = args.dimensions or sorted(SEGMENT_SPECS)
    specs = [SEGMENT_SPECS[name] for name in selected]
    runnable = [s for s in specs if s.view in views]
    skipped = [s for s in specs if s.view not in views]

    if not runnable:
        print("None of the requested segment views exist yet.", file=sys.stderr)
        print(f"Available views: {', '.join(sorted(views))}", file=sys.stderr)
        return 1

    chunks = [
        "# Segment performance report",
        "",
        f"Conversion metric: `{args.conversion}` · FDR target: {args.fdr} · "
        f"bootstrap: {args.boot} resamples",
        "",
    ]
    for spec in runnable:
        result = analyse(
            con, spec,
            conversion=args.conversion,
            min_expected=args.min_expected,
            min_spend_share=args.min_spend_share,
            fdr_alpha=args.fdr,
            n_boot=args.boot,
        )
        chunks.append(format_analysis(result, top=args.top))

    if skipped:
        chunks.append(
            "_Not yet extracted: " + ", ".join(f"`{s.name}`" for s in skipped) + "._"
        )

    report = "\n".join(chunks)
    print(report)

    if args.report:
        args.report.write_text(report, encoding="utf-8")
        print(f"\nWritten to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
