"""Segment-level performance analysis over the extracted warehouse.

The output is a ranked shortlist of segments whose conversion efficiency
differs from the account average by more than sampling noise explains, with
the multiplicity of the scan accounted for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

from .stats import (
    benjamini_hochberg,
    bootstrap_pvalue,
    cluster_bootstrap_rates,
    pearson_dispersion,
    rate_test,
)

# ``initiate_checkout`` rather than ``purchase``: the Purchase pixel on this
# account fires for a negligible fraction of real orders, so it carries almost
# no signal. Checkout is the deepest funnel stage that is reliably tracked.
DEFAULT_CONVERSION = "initiate_checkout"


@dataclass(frozen=True)
class SegmentSpec:
    name: str
    view: str
    dimensions: tuple[str, ...]
    description: str = ""
    # Resampling unit for the cluster bootstrap. Campaign-level passes carry no
    # ad_id, so they cluster on the campaign instead.
    cluster: str = "ad_id"
    # Some breakdowns come back without pixel conversions at all, so they need
    # a shallower outcome. None means "use whatever the caller asked for".
    conversion: str | None = None
    # Budget advice is only offered when the outcome is a real conversion.
    # A proxy such as link_click ranks segments fine but says nothing about
    # what a dollar moved between them would actually buy.
    allow_reallocation: bool = True


SEGMENT_SPECS: dict[str, SegmentSpec] = {
    "age_gender": SegmentSpec(
        "age_gender", "ads_by_age_gender", ("age", "gender"),
        "Audience demographics",
    ),
    "placement": SegmentSpec(
        "placement", "ads_by_placement", ("publisher_platform", "platform_position"),
        "Where the ad is shown",
    ),
    "device": SegmentSpec(
        "device", "ads_by_placement", ("impression_device",),
        "Device class",
    ),
    # Meta returns no offsite pixel events alongside a region breakdown, so the
    # deepest signal available here is the on-platform link click.
    "region": SegmentSpec(
        "region", "ads_by_region", ("region",),
        "Geography (link clicks — pixel conversions unavailable on this breakdown)",
        conversion="link_click",
        allow_reallocation=False,
    ),
    "hourly": SegmentSpec(
        "hourly", "campaigns_by_hour",
        ("hourly_stats_aggregated_by_advertiser_time_zone",),
        "Hour of day",
        cluster="campaign_id",
    ),
}


@dataclass
class SegmentAnalysis:
    spec: SegmentSpec
    frame: pd.DataFrame
    global_rate: float
    dispersion: float
    total_spend: float
    total_conversions: float
    excluded: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def significant(self) -> pd.DataFrame:
        return self.frame[self.frame["significant"]].copy()


def available_views(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.sql(
        "SELECT table_name FROM information_schema.tables"
    ).fetchall()
    return {r[0] for r in rows}


def analyse(
    con: duckdb.DuckDBPyConnection,
    spec: SegmentSpec,
    *,
    conversion: str = DEFAULT_CONVERSION,
    min_expected: float = 5.0,
    min_spend_share: float = 0.002,
    fdr_alpha: float = 0.05,
    n_boot: int = 2000,
) -> SegmentAnalysis:
    """Score every segment of ``spec`` against the account-wide rate."""
    dims = list(spec.dimensions)
    dim_sql = ", ".join(dims)
    metric = spec.conversion or conversion

    rows = con.sql(
        f"""
        SELECT {dim_sql},
               {spec.cluster} AS cluster_id,
               CAST(COALESCE(spend, 0) AS DOUBLE)    AS spend,
               CAST(COALESCE({metric}, 0) AS DOUBLE) AS conversions
        FROM {spec.view}
        WHERE spend IS NOT NULL AND spend > 0
        """
    ).df()

    if rows.empty:
        empty = pd.DataFrame()
        return SegmentAnalysis(spec, empty, 0.0, 1.0, 0.0, 0.0, empty)

    grouped = (
        rows.groupby(dims, dropna=False)
        .agg(spend=("spend", "sum"), conversions=("conversions", "sum"), n_rows=("spend", "size"))
        .reset_index()
    )

    total_spend = float(grouped["spend"].sum())
    total_conv = float(grouped["conversions"].sum())
    if total_spend <= 0 or total_conv <= 0:
        return SegmentAnalysis(spec, pd.DataFrame(), 0.0, 1.0, total_spend, total_conv)

    global_rate = total_conv / total_spend
    grouped["expected"] = grouped["spend"] * global_rate

    # Two independent floors. ``min_expected`` guards the normal approximation,
    # but it scales with the outcome: at ~18 link clicks per dollar a segment
    # with 1 $ of spend clears it, and a 1 $ segment must never drive advice.
    # ``min_spend_share`` is scale-free and catches exactly that case.
    # Excluded segments are reported rather than silently dropped — a hidden
    # filter is a hidden bias.
    keep = (grouped["expected"] >= min_expected) & (
        grouped["spend"] >= min_spend_share * total_spend
    )
    excluded = grouped[~keep].copy()
    frame = grouped[keep].copy()
    if frame.empty:
        return SegmentAnalysis(spec, frame, global_rate, 1.0, total_spend, total_conv, excluded)

    # Quasi-Poisson dispersion is reported as a diagnostic only. Estimating it
    # from between-segment deviations would absorb the very effect we are
    # testing for, so it does not drive significance here.
    dispersion = pearson_dispersion(frame["conversions"].to_numpy(), frame["expected"].to_numpy())
    frame["z_poisson"], _ = rate_test(
        frame["conversions"].to_numpy(),
        frame["expected"].to_numpy(),
        dispersion=dispersion,
    )

    # Performance index: 1.0 == exactly the account average.
    frame["perf_index"] = frame["conversions"] / frame["expected"]
    frame["rate"] = frame["conversions"] / frame["spend"]
    frame["cost_per_conv"] = np.where(
        frame["conversions"] > 0, frame["spend"] / frame["conversions"], np.nan
    )
    frame["spend_share"] = frame["spend"] / total_spend

    # Inference is done by cluster bootstrap over ads. Each segment is compared
    # against the rate of every *other* segment (leave-one-out), so a large
    # segment is not diluted by being measured against a mean it dominates.
    lows: list[float] = []
    highs: list[float] = []
    pvals: list[float] = []
    n_ads: list[int] = []

    for _, seg in frame.iterrows():
        mask = np.ones(len(rows), dtype=bool)
        for dim in dims:
            mask &= (rows[dim] == seg[dim]).to_numpy()

        sub = rows.loc[mask]
        boot = cluster_bootstrap_rates(
            sub["conversions"].to_numpy(),
            sub["spend"].to_numpy(),
            sub["cluster_id"].to_numpy(),
            n_boot=n_boot,
        )
        n_ads.append(int(sub["cluster_id"].nunique()))

        other_spend = total_spend - float(seg["spend"])
        other_conv = total_conv - float(seg["conversions"])
        reference = other_conv / other_spend if other_spend > 0 else np.nan

        if boot.size == 0:
            lows.append(np.nan)
            highs.append(np.nan)
            pvals.append(1.0)
            continue

        lo, hi = np.nanpercentile(boot, [2.5, 97.5])
        lows.append(float(lo))
        highs.append(float(hi))
        pvals.append(bootstrap_pvalue(boot, reference))

    frame["n_ads"] = n_ads
    frame["rate_ci_low"] = lows
    frame["rate_ci_high"] = highs
    frame["p_value"] = pvals
    frame["q_value"] = benjamini_hochberg(np.asarray(pvals, dtype=float))
    frame["significant"] = frame["q_value"] < fdr_alpha

    with np.errstate(divide="ignore", invalid="ignore"):
        arr_low = np.asarray(lows, dtype=float)
        arr_high = np.asarray(highs, dtype=float)
        frame["cpc_ci_low"] = np.where(arr_high > 0, 1.0 / arr_high, np.nan)
        frame["cpc_ci_high"] = np.where(arr_low > 0, 1.0 / arr_low, np.nan)

    frame = frame.sort_values("perf_index", ascending=False).reset_index(drop=True)
    frame.insert(0, "segment", frame[dims].astype(str).agg(" / ".join, axis=1))

    return SegmentAnalysis(
        spec=spec,
        frame=frame,
        global_rate=global_rate,
        dispersion=dispersion,
        total_spend=total_spend,
        total_conversions=total_conv,
        excluded=excluded,
    )
