#!/usr/bin/env python3
"""Generate the figures used by the LaTeX report, straight from the warehouse.

Every number in the report therefore traces back to the extracted data; none
is typed by hand. Re-running this after a new extraction refreshes the report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from meta_backfill.config import load_settings

OUT = Path("figures")
INK = "#1f2933"
ACCENT = "#2f6f9f"
WARN = "#c0623d"
GOOD = "#3f7d5a"
GREY = "#9aa5b1"

plt.rcParams.update({
    "figure.dpi": 160,
    "savefig.dpi": 160,
    "font.size": 9,
    "axes.edgecolor": INK,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": INK,
    "ytick.color": INK,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.autolayout": True,
})


def fig_funnel(con) -> None:
    row = con.sql("""
        SELECT SUM(impressions) AS imp, SUM(link_click) AS lc,
               SUM(landing_page_view) AS lpv, SUM(add_to_cart) AS atc,
               SUM(initiate_checkout) AS ic, SUM(purchase) AS p
        FROM ads_daily WHERE spend > 0
    """).df().iloc[0]

    labels = ["Impressions", "Clics lien", "Vues de page", "Panier", "Checkout", "Achat"]
    values = [row.imp, row.lc, row.lpv, row.atc, row.ic, max(row.p, 1)]
    colors = [ACCENT] * 5 + [WARN]

    fig, ax = plt.subplots(figsize=(6.2, 3.1))
    bars = ax.barh(range(len(labels)), values, color=colors, height=0.62)
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("Volume (échelle logarithmique)")
    ax.grid(axis="x", alpha=0.25, linewidth=0.6)

    for bar, value in zip(bars, values):
        ax.text(value * 1.35, bar.get_y() + bar.get_height() / 2,
                f"{int(value):,}".replace(",", " "),
                va="center", fontsize=8.5)
    ax.set_xlim(right=max(values) * 12)
    fig.savefig(OUT / "funnel.png", bbox_inches="tight")
    plt.close(fig)


def fig_placement(con) -> None:
    df = con.sql("""
        SELECT publisher_platform || ' / ' || platform_position AS seg,
               SUM(spend) AS spend, SUM(initiate_checkout) AS conv
        FROM ads_by_placement
        WHERE spend > 0
        GROUP BY 1 HAVING SUM(spend) > 400 AND SUM(initiate_checkout) > 0
        ORDER BY SUM(spend) / SUM(initiate_checkout)
    """).df()
    df["cpa"] = df.spend / df.conv
    avg = float(df.spend.sum() / df.conv.sum())

    fig, ax = plt.subplots(figsize=(6.2, 3.2))
    colors = [GOOD if c < avg else WARN for c in df.cpa]
    bars = ax.barh(range(len(df)), df.cpa, color=colors, height=0.62)
    ax.set_yticks(range(len(df)), df.seg)
    ax.invert_yaxis()
    ax.axvline(avg, color=INK, linestyle="--", linewidth=1)
    ax.text(avg, -0.85, f" moyenne {avg:.2f} $", fontsize=8, color=INK)
    ax.set_xlabel("Coût par checkout ($)")
    ax.grid(axis="x", alpha=0.25, linewidth=0.6)
    for bar, cpa in zip(bars, df.cpa):
        ax.text(cpa + max(df.cpa) * 0.025, bar.get_y() + bar.get_height() / 2,
                f"{cpa:.2f}", va="center", fontsize=8.5)
    ax.set_xlim(right=max(df.cpa) * 1.18)
    fig.savefig(OUT / "placement.png", bbox_inches="tight")
    plt.close(fig)


def fig_hourly(con) -> None:
    df = con.sql("""
        SELECT CAST(SUBSTR(hourly_stats_aggregated_by_advertiser_time_zone, 1, 2) AS INT) AS hour,
               SUM(spend) AS spend, SUM(initiate_checkout) AS conv
        FROM campaigns_by_hour WHERE spend > 0 GROUP BY 1 ORDER BY 1
    """).df()
    df["cpa"] = df.spend / df.conv
    avg = float(df.spend.sum() / df.conv.sum())

    fig, ax = plt.subplots(figsize=(6.2, 2.7))
    colors = [GOOD if c < avg else WARN for c in df.cpa]
    ax.bar(df.hour, df.cpa, color=colors, width=0.72)
    ax.axhline(avg, color=INK, linestyle="--", linewidth=1)
    ax.text(23.4, avg * 1.04, f"moyenne {avg:.2f} $", fontsize=8, ha="right")
    ax.set_xlabel("Heure de la journée (fuseau du compte)")
    ax.set_ylabel("Coût par checkout ($)")
    ax.set_xticks(range(0, 24, 2))
    ax.grid(axis="y", alpha=0.25, linewidth=0.6)
    fig.savefig(OUT / "hourly.png", bbox_inches="tight")
    plt.close(fig)


def fig_lifetime(con) -> None:
    df = con.sql("SELECT active_days FROM ad_lifecycle ORDER BY 1").df()
    days = df.active_days.to_numpy()

    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.6))
    axes[0].hist(days, bins=range(1, int(days.max()) + 2), color=ACCENT, edgecolor="white")
    axes[0].set_xlabel("Jours de diffusion")
    axes[0].set_ylabel("Nombre d'annonces")
    axes[0].grid(axis="y", alpha=0.25, linewidth=0.6)

    # Empirical survival: share of ads still delivering after d days.
    grid = np.arange(1, int(days.max()) + 1)
    surv = [(days >= d).mean() for d in grid]
    axes[1].step(grid, surv, where="post", color=WARN, linewidth=1.8)
    axes[1].set_xlabel("Jours de diffusion")
    axes[1].set_ylabel("Part encore active")
    axes[1].set_ylim(0, 1.02)
    axes[1].grid(alpha=0.25, linewidth=0.6)
    fig.savefig(OUT / "lifetime.png", bbox_inches="tight")
    plt.close(fig)


def fig_demographics(con) -> None:
    df = con.sql("""
        SELECT age, gender, SUM(spend) AS spend, SUM(initiate_checkout) AS conv
        FROM ads_by_age_gender
        WHERE gender IN ('female', 'male') AND spend > 0
        GROUP BY 1, 2 HAVING SUM(initiate_checkout) > 30 ORDER BY age
    """).df()
    ages = sorted(df.age.unique())
    x = np.arange(len(ages))
    width = 0.38

    fig, ax = plt.subplots(figsize=(6.2, 2.7))
    for offset, gender, color, label in [
        (-width / 2, "female", ACCENT, "Femmes"),
        (width / 2, "male", GREY, "Hommes"),
    ]:
        sub = df[df.gender == gender].set_index("age").reindex(ages)
        cpa = (sub.spend / sub.conv).to_numpy()
        ax.bar(x + offset, cpa, width, color=color, label=label)
        for xi, value in zip(x + offset, cpa):
            if np.isfinite(value):
                ax.text(xi, value + 0.12, f"{value:.2f}", ha="center", fontsize=7.6)

    ax.set_xticks(x, ages)
    ax.set_xlabel("Tranche d'âge")
    ax.set_ylabel("Coût par checkout ($)")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(axis="y", alpha=0.25, linewidth=0.6)
    fig.savefig(OUT / "demographics.png", bbox_inches="tight")
    plt.close(fig)


def fig_fatigue(con) -> None:
    """Evidence that creative fatigue is absent: flat CTR, null slope test."""
    df = con.sql("""
        SELECT ad_id, ctr, spend,
               ROW_NUMBER() OVER (PARTITION BY ad_id ORDER BY date) AS day_n
        FROM ads_daily WHERE spend > 0 AND impressions > 0
    """).df()
    life = df.groupby("ad_id").day_n.max()
    long_ads = life[life >= 12].index
    sub = df[df.ad_id.isin(long_ads)]

    prof = sub.groupby("day_n").agg(n=("ad_id", "nunique"), ctr=("ctr", "median")).reset_index()
    prof = prof[prof.n >= 10]

    slopes = []
    for _, g in sub.groupby("ad_id"):
        g = g.sort_values("day_n")
        if len(g) >= 8 and g.ctr.std() > 0:
            slopes.append(np.polyfit(g.day_n, g.ctr, 1)[0] / g.ctr.mean() * 100)
    slopes = np.array(slopes)

    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.6))
    axes[0].plot(prof.day_n, prof.ctr, marker="o", ms=3.5, color=ACCENT, linewidth=1.6)
    axes[0].set_xlabel("Jour de diffusion")
    axes[0].set_ylabel("CTR médian (%)")
    axes[0].set_ylim(bottom=0)
    axes[0].grid(alpha=0.25, linewidth=0.6)

    axes[1].hist(slopes, bins=14, color=GREY, edgecolor="white")
    axes[1].axvline(0, color=INK, linestyle="--", linewidth=1.2)
    axes[1].axvline(slopes.mean(), color=WARN, linewidth=1.6)
    axes[1].set_xlabel("Pente du CTR par annonce (%/jour)")
    axes[1].set_ylabel("Nombre d'annonces")
    axes[1].grid(axis="y", alpha=0.25, linewidth=0.6)
    fig.savefig(OUT / "fatigue.png", bbox_inches="tight")
    plt.close(fig)


def fig_early_warning(con) -> None:
    """Forest plot of the three AUC estimates with their intervals."""
    from analysis.early_warning import (
        bootstrap_auc_ci, build_dataset, fit_and_evaluate,
        grouped_cv_auc, temporal_split,
    )
    from sklearn.metrics import roc_auc_score

    df = build_dataset(con)
    # temporal_split returns the boundary actually used alongside the two
    # frames (it falls back to a data-derived quantile when the fixed
    # calendar cutoff does not separate the data — see its docstring), so the
    # label below stays accurate instead of naming a specific pair of months
    # that a re-run on different data would silently outgrow.
    train, test, boundary = temporal_split(df)
    _, ev = fit_and_evaluate(train, test)
    cv = grouped_cv_auc(df)

    y = (df.late_icr > df.late_icr.median()).astype(int).to_numpy()
    uni_ci = bootstrap_auc_ci(y, df.e_icr.to_numpy())

    rows = [
        ("Checkouts/$ seul\n(VC groupée)", cv["auc_univariate"], uni_ci),
        ("Modèle 6 variables\n(VC groupée)", cv["auc"], cv["ci"]),
        (f"Modèle 6 variables\n(avant/après {boundary.date()})", ev.auc_model, ev.auc_ci),
    ]
    fig, ax = plt.subplots(figsize=(6.2, 2.4))
    for i, (label, auc, (lo, hi)) in enumerate(rows):
        colour = GOOD if lo > 0.5 else WARN
        ax.plot([lo, hi], [i, i], color=colour, linewidth=2.4, solid_capstyle="round")
        ax.plot(auc, i, "o", color=colour, ms=7)
        ax.text(hi + 0.012, i, f"{auc:.2f}", va="center", fontsize=8.5)
    ax.axvline(0.5, color=INK, linestyle="--", linewidth=1)
    ax.text(0.5, len(rows) - 0.42, " hasard", fontsize=8, color=INK)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows], fontsize=8)
    ax.set_ylim(-0.6, len(rows) - 0.35)
    ax.invert_yaxis()
    ax.set_xlabel("AUC (intervalle de confiance à 95 %)")
    ax.set_xlim(0.33, 0.90)
    ax.grid(axis="x", alpha=0.25, linewidth=0.6)
    fig.savefig(OUT / "early_warning.png", bbox_inches="tight")
    plt.close(fig)


def fig_creative(con) -> None:
    """Slope chart: the same creative trait pushes attention and conversion apart."""
    from analysis.creative import build_dataset, univariate_associations
    from meta_backfill.config import load_settings

    settings = load_settings()
    df = build_dataset(
        con, settings.data_dir / "creatives.parquet", settings.data_dir / "thumbnails"
    )
    ctr = univariate_associations(df, "ctr").set_index("feature")
    icr = univariate_associations(df, "icr").set_index("feature")

    labels = {
        "is_video": "Vidéo", "aspect_ratio": "Format vertical", "is_reel": "Reel",
        "contrast": "Contraste", "colorfulness": "Colorfulness",
        "saturation": "Saturation", "has_date": "Mention de date",
        "n_emoji": "Nombre d'emojis", "n_question": "Question",
    }
    keep = [
        f for f in labels
        if f in ctr.index and f in icr.index
        and (bool(ctr.loc[f, "holds_within"]) or bool(icr.loc[f, "holds_within"]))
    ]

    left = np.array([float(ctr.loc[f, "rho_within"]) for f in keep])
    right = np.array([float(icr.loc[f, "rho_within"]) for f in keep])

    def spread(values: np.ndarray, min_gap: float) -> np.ndarray:
        """Nudge label positions apart so near-equal values stay readable.

        Several traits land within 0.01 of each other; drawn at their true
        height the captions overprint and the chart becomes unreadable. Only
        the text moves — the plotted points keep their exact values.
        """
        out = values.astype(float).copy()
        order = np.argsort(out)
        for previous, current in zip(order, order[1:]):
            if out[current] - out[previous] < min_gap:
                out[current] = out[previous] + min_gap
        return out

    label_y = spread(left, 0.033)
    value_y = spread(right, 0.033)

    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    for i, f in enumerate(keep):
        colour = WARN if left[i] > right[i] else GOOD
        ax.plot([0, 1], [left[i], right[i]], color=colour,
                linewidth=1.7, marker="o", ms=4.5)
        ax.text(-0.05, label_y[i], labels[f], ha="right", va="center", fontsize=8)
        ax.text(1.05, value_y[i], f"{right[i]:+.2f}", ha="left", va="center", fontsize=8)

    ax.axhline(0, color=INK, linewidth=1, linestyle="--")
    ax.set_xticks([0, 1], ["CTR\n(attention)", "Checkouts/$\n(conversion)"], fontsize=9)
    ax.set_xlim(-0.62, 1.30)
    ax.set_ylabel("Corrélation intra-campagne (rho)")
    ax.grid(axis="y", alpha=0.25, linewidth=0.6)
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    fig.savefig(OUT / "creative.png", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    settings = load_settings()
    if not settings.warehouse_path.exists():
        print("No warehouse — run build_warehouse.py first.", file=sys.stderr)
        return 1
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(settings.warehouse_path), read_only=True)

    for name, fn in [
        ("funnel", fig_funnel),
        ("placement", fig_placement),
        ("hourly", fig_hourly),
        ("lifetime", fig_lifetime),
        ("demographics", fig_demographics),
        ("fatigue", fig_fatigue),
        ("early_warning", fig_early_warning),
        ("creative", fig_creative),
    ]:
        fn(con)
        print(f"  figures/{name}.png")
    print(f"\nWritten to {OUT.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
