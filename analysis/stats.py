"""Statistical primitives for comparing conversion rates across ad segments.

Segments must be compared on **rate**, not raw counts: a segment that received
ten times the budget should convert ten times as often before we call it good.
The natural model treats spend as exposure:

    conversions_i ~ Poisson(lambda_i * spend_i)

Testing each ``lambda_i`` against the account-wide rate gives a per-segment
p-value. Two corrections keep that honest, and skipping either is how a scan
over hundreds of segments manufactures findings out of noise:

**Overdispersion.** Ad segments are not homogeneous, so observed variance
exceeds the Poisson mean. A Pearson dispersion estimate widens the standard
errors (quasi-Poisson). Without it, almost everything looks significant.

**Multiplicity.** Testing 150 segments at alpha = 0.05 yields ~7 false
positives by construction. p-values are adjusted with Benjamini-Hochberg,
which controls the false discovery rate rather than the family-wise error
rate — the right trade-off when the goal is a ranked shortlist to act on.
"""

from __future__ import annotations

import numpy as np
from scipy import stats as _sps

__all__ = [
    "pearson_dispersion",
    "rate_test",
    "benjamini_hochberg",
    "bootstrap_rate_ci",
    "cluster_bootstrap_rates",
    "bootstrap_pvalue",
]


def pearson_dispersion(
    observed: np.ndarray,
    expected: np.ndarray,
    *,
    n_params: int = 1,
) -> float:
    """Estimate the quasi-Poisson dispersion phi.

    ``phi = chi2_pearson / degrees_of_freedom``. Values above 1 mean the data
    are more variable than Poisson allows. The result is floored at 1.0: we
    are willing to widen confidence intervals, never to narrow them below the
    Poisson baseline.
    """
    obs = np.asarray(observed, dtype=float)
    exp = np.asarray(expected, dtype=float)
    mask = exp > 0
    dof = int(mask.sum()) - n_params
    if dof <= 0:
        return 1.0
    chi2 = float(np.sum((obs[mask] - exp[mask]) ** 2 / exp[mask]))
    return max(chi2 / dof, 1.0)


def rate_test(
    observed: np.ndarray,
    expected: np.ndarray,
    *,
    dispersion: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Two-sided score test of observed vs expected counts.

    Returns ``(z, p)``. Under quasi-Poisson the variance is ``phi * mu``, so
    the standard error is ``sqrt(phi * expected)``.
    """
    obs = np.asarray(observed, dtype=float)
    exp = np.asarray(expected, dtype=float)
    phi = max(float(dispersion), 1.0)

    se = np.sqrt(phi * exp)
    z = np.divide(obs - exp, se, out=np.zeros_like(obs, dtype=float), where=se > 0)
    p = 2.0 * _sps.norm.sf(np.abs(z))
    return z, p


def benjamini_hochberg(pvalues: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (q-values), order preserved."""
    p = np.asarray(pvalues, dtype=float)
    n = p.size
    if n == 0:
        return p.copy()

    order = np.argsort(p)
    ranked = p[order]
    scaled = ranked * n / np.arange(1, n + 1)
    # Enforce monotonicity from the largest p-value downwards.
    monotone = np.minimum.accumulate(scaled[::-1])[::-1]

    out = np.empty(n, dtype=float)
    out[order] = np.clip(monotone, 0.0, 1.0)
    return out


def bootstrap_rate_ci(
    conversions: np.ndarray,
    spend: np.ndarray,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap interval for ``sum(conversions) / sum(spend)``.

    Resampling happens over the underlying (ad, day) rows rather than assuming
    a parametric form, so within-segment heterogeneity is reflected in the
    interval. This is the number to quote to a non-statistician: it answers
    "how confident are we in this segment's cost per conversion?" directly.
    """
    conv = np.asarray(conversions, dtype=float)
    sp = np.asarray(spend, dtype=float)
    n = conv.size
    if n == 0 or not np.isfinite(sp).any() or sp.sum() <= 0:
        return (float("nan"), float("nan"))

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    boot_conv = conv[idx].sum(axis=1)
    boot_spend = sp[idx].sum(axis=1)

    with np.errstate(divide="ignore", invalid="ignore"):
        rates = np.where(boot_spend > 0, boot_conv / boot_spend, np.nan)

    if np.isnan(rates).all():
        return (float("nan"), float("nan"))

    lo, hi = np.nanpercentile(rates, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def cluster_bootstrap_rates(
    conversions: np.ndarray,
    spend: np.ndarray,
    clusters: np.ndarray,
    *,
    n_boot: int = 2000,
    seed: int = 0,
) -> np.ndarray:
    """Bootstrap distribution of ``sum(conv) / sum(spend)``, resampling clusters.

    Rows are (ad, day) observations, and the days of one ad are correlated:
    the same creative, audience and bid carry over. Resampling rows would
    treat those repeats as independent evidence and produce intervals that are
    far too narrow. Resampling whole *ads* respects the dependence.

    Clusters are pre-aggregated before resampling, so cost is O(n_boot x
    n_clusters) regardless of how many daily rows each ad contributes.
    """
    conv = np.asarray(conversions, dtype=float)
    sp = np.asarray(spend, dtype=float)
    keys = np.asarray(clusters)

    if conv.size == 0 or sp.sum() <= 0:
        return np.array([], dtype=float)

    _, inverse = np.unique(keys, return_inverse=True)
    n_clusters = int(inverse.max()) + 1
    if n_clusters < 2:
        return np.array([], dtype=float)

    conv_by_cluster = np.bincount(inverse, weights=conv, minlength=n_clusters)
    spend_by_cluster = np.bincount(inverse, weights=sp, minlength=n_clusters)

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_clusters, size=(n_boot, n_clusters))
    boot_conv = conv_by_cluster[idx].sum(axis=1)
    boot_spend = spend_by_cluster[idx].sum(axis=1)

    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(boot_spend > 0, boot_conv / boot_spend, np.nan)


def bootstrap_pvalue(rates: np.ndarray, reference: float) -> float:
    """Two-sided bootstrap p-value for a rate differing from ``reference``.

    Floored at ``1 / (B + 1)``: with a finite number of resamples we can never
    honestly report p = 0.
    """
    valid = np.asarray(rates, dtype=float)
    valid = valid[np.isfinite(valid)]
    if valid.size == 0 or not np.isfinite(reference):
        return 1.0
    below = float((valid <= reference).mean())
    above = float((valid >= reference).mean())
    p = 2.0 * min(below, above)
    return float(min(1.0, max(p, 1.0 / (valid.size + 1))))
