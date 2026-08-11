"""Flatten Meta's nested insight rows into a tidy, analysis-ready table.

The API returns conversions as a list of ``{"action_type": ..., "value": ...}``
objects, which is awkward to model. We pivot the action types we care about
into their own columns and keep the untouched payload alongside, so a later
analysis can recover an action type we did not anticipate.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

import pandas as pd

# Funnel-relevant action types, ordered from top to bottom of the funnel.
ACTION_METRICS: tuple[str, ...] = (
    "link_click",
    "landing_page_view",
    "view_content",
    "search",
    "add_to_cart",
    "initiate_checkout",
    "purchase",
    "complete_registration",
    "lead",
    "video_view",
    "post_engagement",
    "comment",
)

NUMERIC_FIELDS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "spend",
    "reach",
    "frequency",
    "cpm",
    "cpc",
    "ctr",
)

_ID_FIELDS: tuple[str, ...] = (
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _index_actions(entries: Any) -> dict[str, float]:
    """Turn Meta's action list into a ``{action_type: value}`` mapping."""
    indexed: dict[str, float] = {}
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
        return indexed
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        action = entry.get("action_type")
        value = _to_float(entry.get("value"))
        if action and value is not None:
            indexed[action] = value
    return indexed


def flatten_row(
    row: dict,
    *,
    pass_name: str,
    breakdowns: Sequence[str],
) -> dict:
    actions = _index_actions(row.get("actions"))
    values = _index_actions(row.get("action_values"))

    flat: dict[str, Any] = {
        "pass_name": pass_name,
        "date": row.get("date_start"),
    }
    for field in _ID_FIELDS:
        flat[field] = row.get(field)
    for field in NUMERIC_FIELDS:
        flat[field] = _to_float(row.get(field))
    for action in ACTION_METRICS:
        flat[action] = actions.get(action)
        flat[f"{action}_value"] = values.get(action)
    for breakdown in breakdowns:
        flat[breakdown] = row.get(breakdown)

    # Keep the raw payload so nothing is lost to our choice of ACTION_METRICS.
    raw_actions = row.get("actions")
    flat["actions_raw"] = json.dumps(raw_actions, ensure_ascii=False) if raw_actions else None

    return flat


def to_dataframe(
    rows: Iterable[dict],
    *,
    pass_name: str,
    breakdowns: Sequence[str],
) -> pd.DataFrame:
    records = [flatten_row(row, pass_name=pass_name, breakdowns=breakdowns) for row in rows]
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return frame

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")

    # Dtypes are pinned explicitly rather than inferred. A month in which an
    # action never fired yields a column of all-None, which pandas infers as
    # object and pyarrow writes as Parquet's null type — so that one month gets
    # a schema incompatible with every other, and reading the partitions
    # together fails. Forcing float/Int64 keeps the schema stable across months.
    for field in NUMERIC_FIELDS:
        if field in frame.columns:
            frame[field] = pd.to_numeric(frame[field], errors="coerce").astype("float64")

    for action in ACTION_METRICS:
        # Counts are integers; nullable Int64 keeps "no data" distinct from a
        # genuine zero, which matters when modelling the funnel.
        if action in frame.columns:
            frame[action] = (
                pd.to_numeric(frame[action], errors="coerce")
                .astype("Float64").round().astype("Int64")
            )
        value_col = f"{action}_value"
        if value_col in frame.columns:
            frame[value_col] = pd.to_numeric(
                frame[value_col], errors="coerce"
            ).astype("float64")

    return frame
