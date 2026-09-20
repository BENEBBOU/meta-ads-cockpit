"""Export the warehouse as a star schema for Power BI (or any BI tool).

Writes CSV files (UTF-8, comma, ISO dates) under export/powerbi/:

    dim_date.csv            one row per calendar day covered by the account
    dim_campaign.csv        campaign, event id derived from the name, activity window
    dim_ad.csv              ad, ad set, campaign, creative traits
    fact_daily.csv          ad x day, all funnel metrics
    fact_age_gender.csv     ad x day x age x gender
    fact_placement.csv      ad x day x platform x position x device
    fact_region.csv         ad x day x region
    fact_hourly.csv         campaign x day x hour of day
    stat_segments.csv       output of analysis.segments for every dimension:
                            performance index, bootstrap CI, FDR-adjusted q

Power BI reads the folder with Get Data > Text/CSV (one query per file) or
Get Data > Folder. Relationships and suggested DAX measures are documented
in docs/powerbi.md.

Usage:
    python export_powerbi.py                 # real warehouse (data/)
    python export_powerbi.py --skip-stats    # facts and dimensions only
    BACKFILL_DATA_DIR=data_sample python export_powerbi.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

from analysis.segments import SEGMENT_SPECS, analyse, available_views
from meta_backfill.config import load_settings

ROOT = Path(__file__).resolve().parent
FUNNEL = [
    "impressions", "reach", "clicks", "spend", "link_click", "landing_page_view",
    "view_content", "add_to_cart", "initiate_checkout", "purchase", "purchase_value",
    "video_view", "post_engagement",
]

# Same convention as the n8n workflows: hashtag first, then a leading number.
_HASHTAG = re.compile(r"#(\d{2,})")
_LEADING = re.compile(r"^\s*(\d{2,})\b")


def event_id(campaign_name: str | None, campaign_id: str) -> str:
    name = campaign_name or ""
    m = _HASHTAG.search(name) or _LEADING.match(name)
    return m.group(1) if m else campaign_id


def _write(df: pd.DataFrame, out_dir: Path, name: str) -> None:
    path = out_dir / f"{name}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
    print(f"  {name:<18} {len(df):>8,} lignes".replace(",", " "))


def _facts(con: duckdb.DuckDBPyConnection, view: str, extra_dims: list[str], key: str = "ad_id") -> pd.DataFrame:
    metrics = ", ".join(f"COALESCE({m}, 0) AS {m}" for m in FUNNEL)
    dims = ", ".join(extra_dims)
    keys = "ad_id, campaign_id" if key == "ad_id" else key
    sql = f"""
        SELECT CAST(date AS DATE) AS date, {keys}{', ' + dims if dims else ''}, {metrics}
        FROM {view}
        WHERE spend IS NOT NULL
    """
    return con.sql(sql).df()


def export(out_dir: Path, *, skip_stats: bool, n_boot: int) -> None:
    settings = load_settings()
    db = settings.data_dir / "meta_ads.duckdb"
    if not db.exists():
        sys.exit(f"Entrepôt introuvable : {db} — lancer build_warehouse.py d'abord.")
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db), read_only=True)
    views = available_views(con)
    print(f"Export Power BI depuis {db} vers {out_dir}/")

    # ---- dimensions -------------------------------------------------------
    span = con.sql("SELECT MIN(CAST(date AS DATE)), MAX(CAST(date AS DATE)) FROM ads_daily").fetchone()
    dates = pd.DataFrame({"date": pd.date_range(span[0], span[1], freq="D")})
    dates["year"] = dates["date"].dt.year
    dates["month"] = dates["date"].dt.month
    dates["month_key"] = dates["date"].dt.strftime("%Y-%m")
    dates["month_label"] = dates["date"].dt.strftime("%b %Y")
    dates["week"] = dates["date"].dt.isocalendar().week.astype(int)
    dates["day_of_week"] = dates["date"].dt.dayofweek + 1
    dates["day_name"] = dates["date"].dt.strftime("%A")
    dates["is_weekend"] = dates["day_of_week"] >= 6
    _write(dates, out_dir, "dim_date")

    campaigns = con.sql("""
        SELECT campaign_id, ANY_VALUE(campaign_name) AS campaign_name,
               MIN(CAST(date AS DATE)) AS first_day, MAX(CAST(date AS DATE)) AS last_day,
               COUNT(DISTINCT ad_id) AS n_ads, SUM(spend) AS total_spend
        FROM ads_daily GROUP BY campaign_id
    """).df()
    campaigns["event_id"] = [event_id(n, i) for n, i in zip(campaigns["campaign_name"], campaigns["campaign_id"])]
    _write(campaigns, out_dir, "dim_campaign")

    ads = con.sql("""
        SELECT ad_id, ANY_VALUE(ad_name) AS ad_name, ANY_VALUE(adset_id) AS adset_id,
               ANY_VALUE(adset_name) AS adset_name, ANY_VALUE(campaign_id) AS campaign_id,
               MIN(CAST(date AS DATE)) AS first_day, MAX(CAST(date AS DATE)) AS last_day,
               COUNT(*) AS active_days
        FROM ads_daily GROUP BY ad_id
    """).df()
    creatives = settings.data_dir / "creatives.parquet"
    if creatives.exists():
        cr = con.sql(f"""
            SELECT ad_id, ANY_VALUE(object_type) AS object_type, ANY_VALUE(cta) AS cta,
                   ANY_VALUE(video_id IS NOT NULL AND video_id <> '') AS is_video,
                   ANY_VALUE(LENGTH(body)) AS body_length, ANY_VALUE(title) AS title
            FROM read_parquet('{creatives.as_posix()}') GROUP BY ad_id
        """).df()
        ads = ads.merge(cr, on="ad_id", how="left")
    _write(ads, out_dir, "dim_ad")

    # ---- facts ------------------------------------------------------------
    _write(_facts(con, "ads_daily", []), out_dir, "fact_daily")
    if "ads_by_age_gender" in views:
        _write(_facts(con, "ads_by_age_gender", ["age", "gender"]), out_dir, "fact_age_gender")
    if "ads_by_placement" in views:
        _write(_facts(con, "ads_by_placement", ["publisher_platform", "platform_position", "impression_device"]),
               out_dir, "fact_placement")
    if "ads_by_region" in views:
        _write(_facts(con, "ads_by_region", ["region"]), out_dir, "fact_region")
    if "campaigns_by_hour" in views:
        hourly = _facts(con, "campaigns_by_hour", ["hourly_stats_aggregated_by_advertiser_time_zone AS hour_range"],
                        key="campaign_id")
        hourly["hour"] = hourly["hour_range"].str.slice(0, 2).astype(int)
        _write(hourly.drop(columns=["hour_range"]), out_dir, "fact_hourly")

    # ---- statistical results ---------------------------------------------
    if not skip_stats:
        frames = []
        for name, spec in SEGMENT_SPECS.items():
            if spec.view not in views:
                continue
            print(f"  analyse {name} (bootstrap {n_boot})...", end="", flush=True)
            res = analyse(con, spec, n_boot=n_boot)
            f = res.frame.copy()
            f.insert(0, "dimension", name)
            f["account_rate"] = res.global_rate
            frames.append(f[["dimension", "segment", "spend", "conversions", "n_ads", "rate", "account_rate",
                             "perf_index", "cost_per_conv", "rate_ci_low", "rate_ci_high",
                             "cpc_ci_low", "cpc_ci_high", "p_value", "q_value", "significant"]])
            print(" ok")
        if frames:
            _write(pd.concat(frames, ignore_index=True), out_dir, "stat_segments")

    con.close()
    print("Terminé. Ouvrir Power BI Desktop > Obtenir les données > Dossier, puis suivre docs/powerbi.md.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "export" / "powerbi")
    parser.add_argument("--skip-stats", action="store_true", help="ne pas recalculer les analyses de segments")
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args()
    export(args.out, skip_stats=args.skip_stats, n_boot=args.n_boot)
