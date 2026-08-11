#!/usr/bin/env python3
"""Which creative attributes drive attention, and do they drive conversion?

    python run_creative_analysis.py
    python run_creative_analysis.py --min-spend 50 --report rapport_creative.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from analysis.creative import (
    ALL_FEATURES,
    IMAGE_FEATURES,
    TEXT_FEATURES,
    build_dataset,
    model_cv,
    univariate_associations,
)
from meta_backfill.config import ConfigError, load_settings

TARGETS = {
    "ctr": "CTR — l'attention gagnée",
    "icr": "Checkouts par dollar — la conversion obtenue",
}


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def build_report(df, results) -> str:
    L: list[str] = []
    L.append("# Analyse des créatives publicitaires")
    L.append("")
    L.append(
        f"{len(df)} annonces avec créative et diffusion suffisante · "
        f"{df.campaign_id.nunique()} campagnes · "
        f"{len(TEXT_FEATURES)} variables de texte, {len(IMAGE_FEATURES)} d'image, "
        f"2 de format."
    )
    L.append("")

    for target, (assoc, cv) in results.items():
        L.append(f"## {TARGETS[target]}")
        L.append("")
        if assoc.empty:
            L.append("_Aucune variable exploitable._")
            L.append("")
            continue

        L.append("### Associations univariées (Spearman, corrigées FDR)")
        L.append("")
        show = assoc.head(10).copy()
        show["rho"] = show.rho.round(3)
        show["intra"] = show.rho_within.round(3)
        show["q_value"] = show.q_value.map(lambda v: f"{v:.3g}")
        show["sig"] = show.significant.map({True: "oui", False: ""})
        show["intra_ok"] = show.holds_within.map({True: "oui", False: ""})
        L.append("```")
        L.append(show[["feature", "n", "rho", "q_value", "sig", "intra", "intra_ok"]]
                 .to_string(index=False))
        L.append("```")
        L.append("")
        n_sig = int(assoc.significant.sum())
        n_within = int(assoc.holds_within.sum())
        L.append(
            f"_{n_sig} variable(s) sur {len(assoc)} survivent au contrôle du taux "
            f"de fausses découvertes ; {n_within} tiennent encore une fois "
            f"l'effet campagne retiré (colonne `intra_ok`)._"
        )
        L.append("")

        L.append("### Modèle multivarié (validation croisée groupée par campagne)")
        L.append("")
        L.append("```")
        for name in ("logistic", "gradient_boosting"):
            if name in cv:
                r = cv[name]
                L.append(
                    f"  {name:<20} AUC {r['auc']:.3f}   "
                    f"IC 95% [{r['ci'][0]:.3f}, {r['ci'][1]:.3f}]   n={r['n']}"
                )
        L.append(f"  ({cv.get('n_features')} variables, {cv.get('n_splits')} plis)")
        L.append("```")
        L.append("")
    return "\n".join(L)


def main() -> int:
    configure_console()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--min-spend", type=float, default=20.0,
                   help="dépense minimale pour retenir une annonce (défaut %(default)s $)")
    p.add_argument("--report", type=Path)
    args = p.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    creatives_path = settings.data_dir / "creatives.parquet"
    thumbs_dir = settings.data_dir / "thumbnails"
    if not creatives_path.exists():
        print("No creatives — run `python run_backfill.py creatives` first.", file=sys.stderr)
        return 1
    if not settings.warehouse_path.exists():
        print("No warehouse — run `python build_warehouse.py` first.", file=sys.stderr)
        return 1

    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    df = build_dataset(con, creatives_path, thumbs_dir, min_spend=args.min_spend)
    if len(df) < 40:
        print(f"Only {len(df)} ads after filtering — too few.", file=sys.stderr)
        return 1

    results = {
        target: (univariate_associations(df, target), model_cv(df, target))
        for target in TARGETS
    }

    report = build_report(df, results)
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
        print(f"\nÉcrit dans {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
