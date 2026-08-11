#!/usr/bin/env python3
"""Expose the extracted Parquet files as a queryable DuckDB database.

No data is copied: DuckDB reads the Parquet partitions in place, so rebuilding
after a new extraction is instantaneous and the files stay the source of truth.

    python build_warehouse.py
    duckdb data/meta_ads.duckdb -c "SELECT * FROM funnel_by_month"
"""

from __future__ import annotations

import sys

import duckdb

from meta_backfill.config import PASSES, ConfigError, load_settings

# Views created only if the corresponding pass produced files.
PASS_VIEWS = {
    "base": "ads_daily",
    "age_gender": "ads_by_age_gender",
    "placement": "ads_by_placement",
    "region": "ads_by_region",
    "hourly": "campaigns_by_hour",
}

FUNNEL_VIEW = """
CREATE OR REPLACE VIEW funnel_by_month AS
SELECT
    date_trunc('month', date)                      AS month,
    SUM(spend)                                     AS spend,
    SUM(impressions)                               AS impressions,
    SUM(link_click)                                AS link_clicks,
    SUM(landing_page_view)                         AS landing_page_views,
    SUM(add_to_cart)                               AS add_to_cart,
    SUM(initiate_checkout)                         AS initiate_checkout,
    SUM(purchase)                                  AS purchase,
    ROUND(SUM(add_to_cart)        / NULLIF(SUM(landing_page_view), 0) * 100, 2) AS lpv_to_cart_pct,
    ROUND(SUM(initiate_checkout)  / NULLIF(SUM(add_to_cart), 0)       * 100, 2) AS cart_to_checkout_pct,
    ROUND(SUM(purchase)           / NULLIF(SUM(initiate_checkout), 0) * 100, 2) AS checkout_to_purchase_pct,
    ROUND(SUM(spend)              / NULLIF(SUM(initiate_checkout), 0),       4) AS cost_per_checkout
FROM ads_daily
GROUP BY 1
ORDER BY 1
"""

CREATIVE_VIEW = """
CREATE OR REPLACE VIEW ad_lifecycle AS
SELECT
    ad_id,
    ANY_VALUE(ad_name)                             AS ad_name,
    ANY_VALUE(campaign_name)                       AS campaign_name,
    MIN(date)                                      AS first_day,
    MAX(date)                                      AS last_day,
    COUNT(*)                                       AS active_days,
    SUM(spend)                                     AS spend,
    SUM(impressions)                               AS impressions,
    ROUND(AVG(ctr), 4)                             AS avg_ctr,
    SUM(initiate_checkout)                         AS initiate_checkout
FROM ads_daily
GROUP BY ad_id
"""


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not settings.raw_dir.exists():
        print(f"No data found at {settings.raw_dir.resolve()}", file=sys.stderr)
        print("Run `python run_backfill.py run --pass base` first.", file=sys.stderr)
        return 1

    settings.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(settings.warehouse_path))

    created: list[str] = []
    registered: set[str] = set()
    for pass_name, view_name in PASS_VIEWS.items():
        if pass_name not in PASSES:
            continue
        pass_dir = settings.raw_dir / f"pass={pass_name}"
        if not list(pass_dir.glob("month=*/*.parquet")):
            continue
        # DuckDB refuses prepared parameters inside CREATE VIEW, so the glob is
        # inlined; single quotes are doubled to keep the literal well formed.
        pattern = str(pass_dir / "month=*" / "*.parquet").replace("\\", "/")
        literal = pattern.replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            # union_by_name reconciles partitions whose schemas differ — a month
            # where some action never fired can carry a narrower column set.
            f"SELECT * FROM read_parquet('{literal}', hive_partitioning = 1, "
            f"union_by_name = 1)"
        )
        rows = con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]
        created.append(f"  {view_name:<22} {rows:>12,} rows")
        registered.add(view_name)

    if not created:
        print("No Parquet partitions found — nothing to register.", file=sys.stderr)
        return 1

    if "ads_daily" in registered:
        con.execute(FUNNEL_VIEW)
        con.execute(CREATIVE_VIEW)
        created.append(f"  {'funnel_by_month':<22} {'(derived)':>12}")
        created.append(f"  {'ad_lifecycle':<22} {'(derived)':>12}")

    con.close()

    print(f"Warehouse ready: {settings.warehouse_path.resolve()}\n")
    print("Views:")
    print("\n".join(created))
    print(
        "\nTry:\n"
        f'  duckdb "{settings.warehouse_path}" -c "SELECT * FROM funnel_by_month"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
