"""Relate what an ad *is* to how it performed.

Insights measure outcomes; the creative is the input that produced them. This
module turns each ad's copy and thumbnail into numeric features, joins them to
lifetime performance, and asks which creative attributes carry signal.

Two targets are modelled on purpose, because they answer different questions:

* **CTR** — does the creative earn attention?
* **Checkouts per dollar** — does that attention convert?

Section 7 of the report established that CTR does not predict conversion
efficiency on this account. Modelling both here tests the natural follow-up:
whether creative choices drive clicks without moving the business outcome.

Everything is evaluated with folds grouped by campaign, as elsewhere in this
project. Ads inside one campaign share an event, an audience and a budget; let
them straddle a fold boundary and the score measures memorisation.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from PIL import Image
from scipy import stats as sps

from .stats import benjamini_hochberg

# Words a Moroccan events advertiser uses to create urgency, accents stripped
# before matching so "dernieres" and "dernières" both hit.
URGENCY_TERMS = (
    "derniere", "dernier", "derniers", "dernieres", "vite", "urgent",
    "complet", "limite", "limitee", "plus que", "ne ratez pas", "reservez",
    "depechez", "bientot", "aujourd hui", "ce soir", "stock",
)
PRICE_RE = re.compile(r"\d+\s*(dh|dhs|mad|dirham)", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b\d{1,2}\s*(janvier|fevrier|mars|avril|mai|juin|juillet|aout|"
    r"septembre|octobre|novembre|decembre)\b",
    re.IGNORECASE,
)
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

TEXT_FEATURES = [
    "n_chars", "n_words", "n_lines", "n_emoji", "n_hashtags", "n_mentions",
    "n_exclaim", "n_question", "caps_ratio", "has_urgency", "has_price",
    "has_date",
]
IMAGE_FEATURES = [
    "aspect_ratio", "brightness", "saturation", "contrast",
    "colorfulness", "edge_density", "dark_ratio",
]
META_FEATURES = ["is_video", "is_reel"]

ALL_FEATURES = TEXT_FEATURES + IMAGE_FEATURES + META_FEATURES


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def text_features(body: object) -> dict:
    raw = body if isinstance(body, str) else ""
    flat = _strip_accents(raw.lower())
    letters = [c for c in raw if c.isalpha()]
    return {
        "n_chars": len(raw),
        "n_words": len(raw.split()),
        "n_lines": raw.count("\n") + 1 if raw else 0,
        "n_emoji": len(EMOJI_RE.findall(raw)),
        "n_hashtags": raw.count("#"),
        "n_mentions": raw.count("@"),
        "n_exclaim": raw.count("!"),
        "n_question": raw.count("?"),
        "caps_ratio": (sum(c.isupper() for c in letters) / len(letters)) if letters else 0.0,
        "has_urgency": int(any(term in flat for term in URGENCY_TERMS)),
        "has_price": int(bool(PRICE_RE.search(flat))),
        "has_date": int(bool(DATE_RE.search(flat))),
    }


def image_features(path: Path, *, max_side: int = 320) -> dict:
    """Cheap perceptual statistics from a thumbnail.

    No pretrained vision model is used: with 360 images and a handful of
    campaigns, a deep embedding would supply hundreds of dimensions the data
    cannot support. These features are few, interpretable, and each maps to a
    decision a designer can act on.
    """
    try:
        with Image.open(path) as handle:
            image = handle.convert("RGB")
            width, height = image.size
            image.thumbnail((max_side, max_side))
            arr = np.asarray(image, dtype=np.float64) / 255.0
    except (OSError, ValueError):
        return {name: np.nan for name in IMAGE_FEATURES}

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b

    channel_max = arr.max(axis=2)
    channel_min = arr.min(axis=2)
    # np.where evaluates both branches, so the divisor is guarded rather than
    # the result: dividing by zero first and masking after still warns.
    lit = channel_max > 0
    saturation = np.where(
        lit, (channel_max - channel_min) / np.where(lit, channel_max, 1.0), 0.0
    )

    # Hasler & Susstrunk (2003): a perceptual colourfulness metric that needs
    # no colour-space conversion.
    rg = r - g
    yb = 0.5 * (r + g) - b
    colourfulness = float(
        np.sqrt(rg.std() ** 2 + yb.std() ** 2)
        + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )

    gy, gx = np.gradient(luminance)
    edge_density = float(np.sqrt(gx ** 2 + gy ** 2).mean())

    return {
        "aspect_ratio": width / height if height else np.nan,
        "brightness": float(luminance.mean()),
        "saturation": float(saturation.mean()),
        "contrast": float(luminance.std()),
        "colorfulness": colourfulness,
        "edge_density": edge_density,
        "dark_ratio": float((luminance < 0.25).mean()),
    }


def build_dataset(
    con: duckdb.DuckDBPyConnection,
    creatives_path: Path,
    thumbnails_dir: Path,
    *,
    min_spend: float = 20.0,
) -> pd.DataFrame:
    """Join creative attributes to each ad's lifetime performance."""
    perf = con.sql(
        """
        SELECT ad_id,
               ANY_VALUE(campaign_id)  AS campaign_id,
               ANY_VALUE(ad_name)      AS ad_name,
               SUM(spend)              AS spend,
               SUM(impressions)        AS impressions,
               SUM(clicks)             AS clicks,
               SUM(initiate_checkout)  AS checkouts
        FROM ads_daily
        WHERE spend > 0
        GROUP BY ad_id
        """
    ).df()

    creatives = pd.read_parquet(creatives_path)
    df = perf.merge(
        creatives[["ad_id", "body", "object_type", "cta"]], on="ad_id", how="inner"
    )

    # Ads below a floor of spend produce rates dominated by noise.
    df = df[df.spend >= min_spend].copy()
    df["ctr"] = df.clicks / df.impressions * 100
    df["icr"] = df.checkouts.fillna(0) / df.spend
    df["is_video"] = (df.object_type == "VIDEO").astype(int)
    df["is_reel"] = df.ad_name.str.contains("reel", case=False, na=False).astype(int)

    text = pd.DataFrame([text_features(b) for b in df.body], index=df.index)
    images = pd.DataFrame(
        [image_features(thumbnails_dir / f"{a}.jpg") for a in df.ad_id], index=df.index
    )
    df = pd.concat([df, text, images], axis=1)

    return df.reset_index(drop=True)


def univariate_associations(
    df: pd.DataFrame, target: str, *, features: list[str] | None = None
) -> pd.DataFrame:
    """Spearman correlation of each feature with the target, FDR-corrected.

    Scanning twenty features guarantees a false positive at alpha = 0.05, so
    the q-value column is the one to read, not p.
    """
    features = features or ALL_FEATURES
    rows = []
    y = df[target].to_numpy()

    # Ads inside one campaign share an event, an audience and a budget. A raw
    # correlation can therefore reflect "this event sold well" rather than
    # "this creative works". Centring both sides within campaign removes the
    # campaign effect and isolates the creative one.
    centred = df.copy()
    big = centred.groupby("campaign_id").ad_id.transform("size") >= 2
    for name in features + [target]:
        centred[name] = pd.to_numeric(centred[name], errors="coerce")
    grouped = centred[big].groupby("campaign_id")

    for name in features:
        x = df[name].to_numpy(dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < 20 or np.nanstd(x[mask]) == 0:
            continue
        rho, p = sps.spearmanr(x[mask], y[mask])

        xc = (centred.loc[big, name] - grouped[name].transform("mean")).to_numpy()
        yc = (centred.loc[big, target] - grouped[target].transform("mean")).to_numpy()
        wmask = np.isfinite(xc) & np.isfinite(yc)
        if wmask.sum() >= 20 and np.nanstd(xc[wmask]) > 0:
            w_rho, w_p = sps.spearmanr(xc[wmask], yc[wmask])
        else:
            w_rho, w_p = np.nan, np.nan

        rows.append({
            "feature": name, "n": int(mask.sum()), "rho": rho, "p_value": p,
            "rho_within": w_rho, "p_within": w_p,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["q_value"] = benjamini_hochberg(out.p_value.to_numpy())
    out["significant"] = out.q_value < 0.05
    inner = out.p_within.to_numpy(dtype=float)
    finite = np.isfinite(inner)
    q_within = np.full(len(out), np.nan)
    if finite.any():
        q_within[finite] = benjamini_hochberg(inner[finite])
    out["q_within"] = q_within
    out["holds_within"] = (out.q_within < 0.05) & (
        np.sign(out.rho_within) == np.sign(out.rho)
    )
    return out.sort_values("rho", key=abs, ascending=False).reset_index(drop=True)


def model_cv(
    df: pd.DataFrame, target: str, *, features: list[str] | None = None, n_splits: int = 5
) -> dict:
    """Grouped-by-campaign cross-validated AUC for predicting above-median target."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    from .early_warning import bootstrap_auc_ci

    features = features or ALL_FEATURES
    usable = [f for f in features if df[f].notna().sum() > len(df) * 0.8]
    X = df[usable]
    y = (df[target] > df[target].median()).astype(int).to_numpy()
    groups = df.campaign_id.to_numpy()

    n_splits = min(n_splits, len(np.unique(groups)))
    results = {}

    for label, estimator in [
        ("logistic", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(C=0.5, max_iter=2000)),
        ])),
        # A non-linear learner is included as a check: if it does no better,
        # the ceiling is the data, not the model class.
        ("gradient_boosting", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("clf", HistGradientBoostingClassifier(
                max_depth=3, max_iter=150, learning_rate=0.06,
                min_samples_leaf=15, random_state=0)),
        ])),
    ]:
        oof = np.full(len(df), np.nan)
        for train_idx, test_idx in GroupKFold(n_splits=n_splits).split(X, y, groups):
            if len(np.unique(y[train_idx])) < 2:
                continue
            model = estimator
            model.fit(X.iloc[train_idx], y[train_idx])
            oof[test_idx] = model.predict_proba(X.iloc[test_idx])[:, 1]

        mask = np.isfinite(oof)
        if mask.sum() < 20 or len(np.unique(y[mask])) < 2:
            continue
        results[label] = {
            "auc": float(roc_auc_score(y[mask], oof[mask])),
            "ci": bootstrap_auc_ci(y[mask], oof[mask]),
            "n": int(mask.sum()),
        }

    results["n_features"] = len(usable)
    results["n_splits"] = n_splits
    return results
