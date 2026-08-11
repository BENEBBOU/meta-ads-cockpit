"""Early detection of under-performing ads from their first days of delivery.

The question this answers is the one a media buyer actually asks on day two:
*is this ad worth continuing to fund?*

Design constraints, each of which rules out a more convenient shortcut:

**Disjoint windows.** Features come from days 1..``early_days``; the target is
computed on day ``early_days+1`` onwards. No observation feeds both sides, so a
good score cannot come from predicting a quantity that is partly already in the
inputs.

**Temporal split.** Training uses ads that started before a cutoff date, testing
uses ads that started after. A random split would let the model see ads from the
same campaigns, the same events and the same week as its test cases, and would
report a score that deployment could never reproduce.

**Train-derived threshold.** The "good ad" cutoff is the median late efficiency
*of the training set*, applied unchanged to the test set. Recomputing it on the
test set would leak the answer.

**Known limitation.** Most of the signal separates campaigns rather than
creatives inside one campaign (within-campaign Spearman rho is roughly 0.2
against 0.46 overall). The model is therefore a portfolio triage tool, not a
creative selector. :func:`within_campaign_signal` measures this on any dataset
so the caveat is never lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd
from scipy import stats as sps
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

EARLY_DAYS = 2
MIN_DAYS = 5
DEFAULT_CUTOFF = "2026-08-01"
SATURATION = 0.8

FEATURES = [
    "log_icr",     # early initiate_checkout per dollar — the dominant signal
    "log_atcr",    # early add_to_cart per dollar
    "log_lpvr",    # early landing page views per dollar
    "log_cpm",     # how expensive the impressions are
    "log_spend",   # scale of the early test
    "is_reel",     # creative format, parsed from the ad name
]

_FORMAT_RE = re.compile(r"(reel|post|story|carousel|video)", re.IGNORECASE)


def _creative_format(name: object) -> str:
    match = _FORMAT_RE.search(str(name))
    return match.group(1).lower() if match else "other"


def build_dataset(
    con: duckdb.DuckDBPyConnection,
    *,
    early_days: int = EARLY_DAYS,
    min_days: int = MIN_DAYS,
) -> pd.DataFrame:
    """One row per ad: early-window features and late-window outcome."""
    panel = con.sql(
        """
        SELECT ad_id, ad_name, campaign_id, date, spend, impressions, clicks, cpm,
               COALESCE(landing_page_view, 0) AS lpv,
               COALESCE(add_to_cart, 0)       AS atc,
               COALESCE(initiate_checkout, 0) AS ic,
               ROW_NUMBER() OVER (PARTITION BY ad_id ORDER BY date) AS day_n
        FROM ads_daily
        WHERE spend > 0 AND impressions > 0
        """
    ).df()

    lifetime = panel.groupby("ad_id").day_n.max()
    eligible = lifetime[lifetime >= early_days + (min_days - early_days)].index
    panel = panel[panel.ad_id.isin(eligible)]

    early = (
        panel[panel.day_n <= early_days]
        .groupby(["ad_id", "campaign_id", "ad_name"])
        .agg(
            e_spend=("spend", "sum"),
            e_imp=("impressions", "sum"),
            e_clicks=("clicks", "sum"),
            e_cpm=("cpm", "mean"),
            e_lpv=("lpv", "sum"),
            e_atc=("atc", "sum"),
            e_ic=("ic", "sum"),
            first_day=("date", "min"),
        )
        .reset_index()
    )
    late = (
        panel[panel.day_n > early_days]
        .groupby("ad_id")
        .agg(l_spend=("spend", "sum"), l_ic=("ic", "sum"), l_days=("day_n", "count"))
        .reset_index()
    )

    df = early.merge(late, on="ad_id")
    df = df[(df.e_spend > 0) & (df.l_spend > 0)].copy()

    df["e_icr"] = df.e_ic / df.e_spend
    df["e_atcr"] = df.e_atc / df.e_spend
    df["e_lpvr"] = df.e_lpv / df.e_spend
    df["late_icr"] = df.l_ic / df.l_spend

    # Rates are heavily right-skewed; log1p keeps the tail from dominating the
    # fit without discarding the ordering.
    df["log_icr"] = np.log1p(df.e_icr)
    df["log_atcr"] = np.log1p(df.e_atcr)
    df["log_lpvr"] = np.log1p(df.e_lpvr)
    df["log_cpm"] = np.log1p(df.e_cpm.fillna(df.e_cpm.median()))
    df["log_spend"] = np.log1p(df.e_spend)
    df["format"] = df.ad_name.map(_creative_format)
    df["is_reel"] = (df["format"] == "reel").astype(int)
    df["first_day"] = pd.to_datetime(df.first_day)

    return df.reset_index(drop=True)


def temporal_split(
    df: pd.DataFrame,
    cutoff: str | None = DEFAULT_CUTOFF,
    *,
    test_fraction: float = 0.25,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Ads that started before the cutoff train; those that started after test.

    A fixed calendar date is the right default while the reported figures refer
    to one specific split, but it is brittle: point the tool at a different
    account, at synthetic data, or simply run it a year later, and every ad can
    land on the same side. When that happens the boundary falls back to a
    quantile of the start dates, which always yields a usable split.

    Returns the two frames and the boundary actually used.
    """
    boundary = pd.Timestamp(cutoff) if cutoff else None
    if boundary is not None:
        train = df[df.first_day < boundary]
        test = df[df.first_day >= boundary]
        if len(train) and len(test):
            return train.copy(), test.copy(), boundary

    boundary = df.first_day.quantile(1.0 - test_fraction)
    train = df[df.first_day < boundary].copy()
    test = df[df.first_day >= boundary].copy()
    return train, test, boundary


def within_campaign_signal(df: pd.DataFrame, *, min_ads: int = 3) -> dict:
    """Compare raw and within-campaign correlation of early vs late efficiency.

    A large gap means the model mostly ranks campaigns, not creatives.
    """
    counts = df.groupby("campaign_id").ad_id.count()
    keep = counts[counts >= min_ads].index
    sub = df[df.campaign_id.isin(keep)].copy()
    if len(sub) < 10:
        return {"n_ads": len(sub), "n_campaigns": len(keep)}

    raw_rho, raw_p = sps.spearmanr(sub.e_icr, sub.late_icr)
    sub["e_c"] = sub.e_icr - sub.groupby("campaign_id").e_icr.transform("mean")
    sub["l_c"] = sub.late_icr - sub.groupby("campaign_id").late_icr.transform("mean")
    wc_rho, wc_p = sps.spearmanr(sub.e_c, sub.l_c)
    return {
        "n_ads": len(sub),
        "n_campaigns": len(keep),
        "raw_rho": float(raw_rho), "raw_p": float(raw_p),
        "within_rho": float(wc_rho), "within_p": float(wc_p),
    }


def bootstrap_auc_ci(
    y: np.ndarray, scores: np.ndarray, *, n_boot: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """Percentile interval for the AUC — essential when the test set is small."""
    rng = np.random.default_rng(seed)
    n = len(y)
    values = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        if len(np.unique(y[idx])) < 2:
            continue
        values.append(roc_auc_score(y[idx], scores[idx]))
    if not values:
        return (float("nan"), float("nan"))
    return tuple(float(v) for v in np.percentile(values, [2.5, 97.5]))


def grouped_cv_auc(
    df: pd.DataFrame, *, n_splits: int = 5, seed: int = 0
) -> dict:
    """Out-of-fold AUC with folds split by campaign.

    A single small temporal holdout is a high-variance estimate: with a few
    dozen test ads the confidence interval can span from chance to strong, and
    settle nothing. Cross-validation uses every ad as a test case once, which
    buys statistical power.

    Folds are grouped by campaign so that no campaign appears in both train and
    test. Without that, ads from the same event and audience would sit on both
    sides and the score would be inflated.

    This trades away temporal generalisation — folds mix July and August — so it
    answers "is there ad-level signal at all?" rather than "does a model trained
    on the past work on the future?". Both numbers are needed.
    """
    from sklearn.model_selection import GroupKFold

    threshold = float(df.late_icr.median())
    y = (df.late_icr > threshold).astype(int).to_numpy()
    groups = df.campaign_id.to_numpy()
    X = df[FEATURES]

    n_splits = min(n_splits, len(np.unique(groups)))
    oof = np.full(len(df), np.nan)

    for train_idx, test_idx in GroupKFold(n_splits=n_splits).split(X, y, groups):
        if len(np.unique(y[train_idx])) < 2:
            continue
        model = Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(C=0.5, max_iter=2000)),
        ])
        model.fit(X.iloc[train_idx], y[train_idx])
        oof[test_idx] = model.predict_proba(X.iloc[test_idx])[:, 1]

    mask = ~np.isnan(oof)
    if mask.sum() < 20 or len(np.unique(y[mask])) < 2:
        return {"n": int(mask.sum())}

    auc = roc_auc_score(y[mask], oof[mask])
    lo, hi = bootstrap_auc_ci(y[mask], oof[mask], seed=seed)
    auc_uni = roc_auc_score(y[mask], df.e_icr.to_numpy()[mask])
    return {
        "n": int(mask.sum()),
        "n_splits": n_splits,
        "auc": float(auc),
        "ci": (lo, hi),
        "auc_univariate": float(auc_uni),
    }


@dataclass
class Evaluation:
    n_train: int
    n_test: int
    threshold: float
    auc_model: float
    auc_ci: tuple[float, float]
    auc_univariate: float
    auc_train: float
    brier: float
    positive_rate_test: float
    coefficients: pd.Series = field(default_factory=pd.Series)
    calibration: pd.DataFrame = field(default_factory=pd.DataFrame)


def fit_and_evaluate(train: pd.DataFrame, test: pd.DataFrame) -> tuple[Pipeline, Evaluation]:
    threshold = float(train.late_icr.median())
    y_train = (train.late_icr > threshold).astype(int).to_numpy()
    y_test = (test.late_icr > threshold).astype(int).to_numpy()

    model = Pipeline([
        ("scale", StandardScaler()),
        # Small sample, six correlated features: regularisation is not optional.
        ("clf", LogisticRegression(C=0.5, max_iter=2000)),
    ])
    model.fit(train[FEATURES], y_train)

    p_test = model.predict_proba(test[FEATURES])[:, 1]
    p_train = model.predict_proba(train[FEATURES])[:, 1]

    auc_model = roc_auc_score(y_test, p_test)
    auc_uni = roc_auc_score(y_test, test.e_icr.to_numpy())

    # Calibration in coarse bins — with a few dozen test ads, finer is noise.
    bins = pd.qcut(p_test, q=min(4, max(2, len(p_test) // 8)), duplicates="drop")
    calib = (
        pd.DataFrame({"p": p_test, "y": y_test, "bin": bins})
        .groupby("bin", observed=True)
        .agg(n=("y", "size"), predicted=("p", "mean"), observed=("y", "mean"))
        .reset_index(drop=True)
    )

    coefs = pd.Series(
        model.named_steps["clf"].coef_[0], index=FEATURES
    ).sort_values(key=abs, ascending=False)

    return model, Evaluation(
        n_train=len(train),
        n_test=len(test),
        threshold=threshold,
        auc_model=float(auc_model),
        auc_ci=bootstrap_auc_ci(y_test, p_test),
        auc_univariate=float(auc_uni),
        auc_train=float(roc_auc_score(y_train, p_train)),
        brier=float(brier_score_loss(y_test, p_test)),
        positive_rate_test=float(y_test.mean()),
        coefficients=coefs,
        calibration=calib,
    )


def decision_curve(
    model: Pipeline,
    test: pd.DataFrame,
    *,
    saturation: float = SATURATION,
    fractions: tuple[float, ...] = (0.0, 0.1, 0.2, 0.25, 0.33, 0.4, 0.5),
) -> pd.DataFrame:
    """Out-of-sample gain from cutting the lowest-scoring ads on day two.

    The whole curve is returned rather than its best point: choosing the cut
    fraction by looking at this table would re-introduce the selection bias the
    temporal split was built to remove.
    """
    scored = test.copy()
    scored["p"] = model.predict_proba(scored[FEATURES])[:, 1]
    scored = scored.sort_values("p").reset_index(drop=True)

    base_conv = float(scored.l_ic.sum())
    base_spend = float(scored.l_spend.sum())

    rows = []
    for q in fractions:
        k = int(round(len(scored) * q))
        cut, keep = scored.iloc[:k], scored.iloc[k:]
        if keep.empty or keep.l_spend.sum() <= 0:
            continue
        freed = float(cut.l_spend.sum())
        growth = (keep.l_spend.sum() + freed) / keep.l_spend.sum()
        projected = float(keep.l_ic.sum()) * growth ** saturation
        rows.append({
            "cut_fraction": q,
            "ads_cut": k,
            "spend_freed": freed,
            "conv_lost": float(cut.l_ic.sum()),
            "projected_conv": projected,
            "gain_pct": (projected / base_conv - 1) * 100 if base_conv else np.nan,
        })

    out = pd.DataFrame(rows)
    out.attrs["baseline_conv"] = base_conv
    out.attrs["baseline_spend"] = base_spend
    return out
