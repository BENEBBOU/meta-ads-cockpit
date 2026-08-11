"""Orchestration: walk the calendar month by month and persist each slice.

Work is chunked per calendar month rather than issued as one giant request.
That keeps individual async jobs small enough for Meta to actually complete
them, gives the checkpoint a useful granularity, and makes partial results
usable — after twenty minutes you already have queryable Parquet on disk.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

from .api import InsightsClient, MetaApiError
from .checkpoint import Checkpoint
from .config import BackfillPass, Settings
from .transform import to_dataframe

log = logging.getLogger(__name__)

# Meta retains ad insights for roughly 37 months.
MAX_MONTHS_BACK = 37


@dataclass(frozen=True)
class MonthWindow:
    label: str  # "2026-07"
    first: dt.date
    last: dt.date


def month_windows(months_back: int, *, today: dt.date | None = None) -> list[MonthWindow]:
    """Calendar months from oldest to newest, excluding today (incomplete)."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=1)
    months_back = max(1, min(months_back, MAX_MONTHS_BACK))

    windows: list[MonthWindow] = []
    for offset in range(months_back):
        year, month = today.year, today.month - offset
        while month <= 0:
            month += 12
            year -= 1
        first = dt.date(year, month, 1)
        next_month_first = dt.date(year + (month == 12), (month % 12) + 1, 1)
        last = min(next_month_first - dt.timedelta(days=1), cutoff)
        if last < first:
            continue  # current month before any complete day exists
        windows.append(MonthWindow(f"{year:04d}-{month:02d}", first, last))

    return sorted(windows, key=lambda w: w.label)


def build_params(pass_cfg: BackfillPass, window: MonthWindow, settings: Settings) -> dict:
    params = {
        "level": pass_cfg.level,
        "fields": ",".join(pass_cfg.fields),
        "time_range": json.dumps(
            {"since": window.first.isoformat(), "until": window.last.isoformat()}
        ),
        "time_increment": 1,
        "limit": settings.page_size,
        # Ads deleted since the campaign ran still hold historical spend.
        "filtering": "[]",
    }
    if pass_cfg.breakdowns:
        params["breakdowns"] = ",".join(pass_cfg.breakdowns)
    return params


def run_pass(
    client: InsightsClient,
    settings: Settings,
    pass_cfg: BackfillPass,
    windows: list[MonthWindow],
    checkpoint: Checkpoint,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Extract one pass across ``windows``. Returns per-month row counts."""
    results: dict[str, int] = {}
    total = len(windows)

    for index, window in enumerate(windows, start=1):
        if checkpoint.done(pass_cfg.name, window.label):
            log.info("[%s %s/%s] %s — already done, skipping",
                     pass_cfg.name, index, total, window.label)
            continue

        params = build_params(pass_cfg, window, settings)
        if dry_run:
            log.info("[%s %s/%s] %s — dry run: %s",
                     pass_cfg.name, index, total, window.label, params)
            continue

        log.info("[%s %s/%s] %s — requesting %s..%s",
                 pass_cfg.name, index, total, window.label,
                 window.first, window.last)

        try:
            rows = client.run_insights(params)
        except MetaApiError as exc:
            # Do not checkpoint: the month stays pending for the next run.
            log.error("[%s] %s failed: %s", pass_cfg.name, window.label, exc)
            continue

        frame = to_dataframe(
            rows, pass_name=pass_cfg.name, breakdowns=pass_cfg.breakdowns
        )
        row_count = len(frame)

        if row_count:
            out_dir = settings.raw_dir / f"pass={pass_cfg.name}" / f"month={window.label}"
            out_dir.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(out_dir / "part.parquet", index=False)

        checkpoint.mark(pass_cfg.name, window.label, rows=row_count)
        results[window.label] = row_count
        log.info("[%s] %s — %s row(s)", pass_cfg.name, window.label, row_count)

    return results
