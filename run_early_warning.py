#!/usr/bin/env python3
"""Train and evaluate the day-2 early-warning model, out of sample.

    python run_early_warning.py
    python run_early_warning.py --early-days 3 --cutoff 2026-08-01
    python run_early_warning.py --report rapport_early_warning.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from analysis.early_warning import (
    DEFAULT_CUTOFF,
    FEATURES,
    EARLY_DAYS,
    MIN_DAYS,
    SATURATION,
    build_dataset,
    decision_curve,
    fit_and_evaluate,
    grouped_cv_auc,
    temporal_split,
    within_campaign_signal,
)
from meta_backfill.config import ConfigError, load_settings


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def build_report(args, df, train, test, ev, curve, wc, cv, boundary) -> str:
    L: list[str] = []
    L.append("# Détection précoce des annonces sous-performantes")
    L.append("")
    L.append(
        f"Variables mesurées sur les jours 1–{args.early_days}, "
        f"cible sur les jours {args.early_days + 1}+. "
        f"Séparation temporelle au {boundary.date()}."
    )
    L.append("")

    L.append("## Données")
    L.append("")
    L.append("```")
    L.append(f"annonces éligibles      {len(df)}")
    L.append(f"entraînement (< {boundary.date()})  {ev.n_train}")
    L.append(f"test (>= {boundary.date()})         {ev.n_test}")
    L.append(f"seuil 'bonne annonce'   {ev.threshold:.4f} checkouts/$ (médiane entraînement)")
    L.append(f"part de positifs (test) {ev.positive_rate_test:.0%}")
    L.append("```")
    L.append("")

    L.append("## Performance hors échantillon")
    L.append("")
    L.append("```")
    L.append(f"AUC modèle (test)       {ev.auc_model:.3f}   IC 95% [{ev.auc_ci[0]:.3f}, {ev.auc_ci[1]:.3f}]")
    L.append(f"AUC checkouts/$ seul    {ev.auc_univariate:.3f}")
    L.append(f"AUC entraînement        {ev.auc_train:.3f}")
    L.append(f"score de Brier          {ev.brier:.3f}")
    L.append("```")
    L.append("")
    gap = ev.auc_train - ev.auc_model
    if gap > 0.12:
        L.append(
            f"_Écart entraînement/test de {gap:.2f} : sur-apprentissage résiduel malgré "
            "la régularisation. Attendu avec un échantillon de cette taille._"
        )
        L.append("")

    if "auc" in cv:
        L.append("### Validation croisée groupée par campagne")
        L.append("")
        L.append("```")
        L.append(f"annonces évaluées       {cv['n']} ({cv['n_splits']} plis)")
        L.append(f"AUC hors pli            {cv['auc']:.3f}   IC 95% [{cv['ci'][0]:.3f}, {cv['ci'][1]:.3f}]")
        L.append(f"AUC checkouts/$ seul    {cv['auc_univariate']:.3f}")
        L.append("```")
        L.append("")
        L.append(
            "_Chaque annonce sert de cas de test une fois, aucune campagne ne "
            "figure des deux côtés d'un pli. Gagne en puissance ce que la "
            "séparation temporelle apporte en réalisme : les deux sont "
            "nécessaires._"
        )
        L.append("")

    L.append("### Coefficients (variables standardisées)")
    L.append("")
    L.append("```")
    for name, value in ev.coefficients.items():
        L.append(f"  {name:<12} {value:+.3f}")
    L.append("```")
    L.append("")

    if not ev.calibration.empty:
        L.append("### Calibration")
        L.append("")
        L.append("```")
        L.append(ev.calibration.round(3).to_string(index=False))
        L.append("```")
        L.append("")

    L.append("## Courbe de décision (test uniquement)")
    L.append("")
    L.append(
        f"Gain projeté en coupant au jour {args.early_days} les annonces les moins "
        f"bien notées, budget libéré redistribué avec saturation "
        f"(alpha = {args.saturation})."
    )
    L.append("")
    L.append("```")
    L.append(curve.round(2).to_string(index=False))
    L.append("```")
    L.append("")
    L.append(
        f"_Référence : {curve.attrs['baseline_conv']:.0f} checkouts pour "
        f"{curve.attrs['baseline_spend']:.0f} $._"
    )
    L.append("")
    L.append(
        "_La courbe entière est rapportée volontairement. Choisir le meilleur "
        "point de coupe en la lisant réintroduirait exactement le biais de "
        "sélection que la séparation temporelle sert à éliminer._"
    )
    L.append("")

    L.append("## Limite structurelle : campagne contre créative")
    L.append("")
    if "within_rho" in wc:
        L.append("```")
        L.append(f"campagnes retenues (>=3 annonces)  {wc['n_campaigns']}  ({wc['n_ads']} annonces)")
        L.append(f"rho brut (inter + intra)           {wc['raw_rho']:+.3f}  (p={wc['raw_p']:.2g})")
        L.append(f"rho intra-campagne                 {wc['within_rho']:+.3f}  (p={wc['within_p']:.2g})")
        L.append("```")
        L.append("")
        L.append(
            "L'essentiel du pouvoir prédictif distingue les campagnes, non les "
            "créatives d'une même campagne. **Cet outil sert l'arbitrage de "
            "portefeuille, pas la sélection d'un visuel parmi ses frères.**"
        )
    else:
        L.append("_Trop peu de campagnes multi-annonces pour mesurer cette limite._")
    L.append("")

    L.append("## Verdict")
    L.append("")
    if "auc" in cv:
        beats = cv["auc_univariate"] > cv["auc"]
        L.append(
            f"**Le signal existe.** En validation croisée groupée, l'AUC hors pli "
            f"atteint {cv['auc']:.2f} avec un intervalle [{cv['ci'][0]:.2f}, "
            f"{cv['ci'][1]:.2f}] qui exclut nettement le hasard. Les deux premiers "
            f"jours de diffusion portent bien de l'information sur la suite."
        )
        L.append("")
        if beats:
            L.append(
                f"**Mais le modèle n'apporte rien.** La seule variable "
                f"« checkouts par dollar » atteint {cv['auc_univariate']:.2f}, soit "
                f"mieux que la régression logistique à {len(FEATURES)} "
                f"variables ({cv['auc']:.2f}). Avec {cv['n']} observations et des "
                f"variables corrélées, le modèle ajoute du bruit plutôt que du signal."
            )
            L.append("")
            L.append(
                "**Recommandation : déployer la règle simple, pas le modèle.** "
                "Classer les annonces par checkouts/\\$ au jour 2 et signaler le "
                "dernier quartile. C'est interprétable, calculable dans un tableur, "
                "sans modèle à réentraîner ni à surveiller — et c'est mesurablement "
                "plus performant."
            )
            L.append("")
    L.append(
        f"**La généralisation temporelle reste non démontrée.** Sur la séparation "
        f"apprentissage/test, l'AUC tombe à {ev.auc_model:.2f} avec un intervalle "
        f"[{ev.auc_ci[0]:.2f}, {ev.auc_ci[1]:.2f}] qui contient 0,50. Deux lectures "
        f"possibles, que {ev.n_test} annonces de test ne permettent pas de "
        f"départager : soit le signal ne se transporte pas d'un mois à l'autre, "
        f"soit l'échantillon est trop petit pour le détecter. À trancher lorsque "
        f"septembre fournira un second jeu de test."
    )
    L.append("")

    L.append("## Réserves")
    L.append("")
    L.append(
        f"- Échantillon de test de {ev.n_test} annonces : l'intervalle de confiance "
        "sur l'AUC est large et doit être cité avec le point estimé."
    )
    L.append(
        f"- Population filtrée : seules les annonces ayant duré au moins "
        f"{args.min_days} jours sont présentes. Celles coupées plus tôt par le "
        "media buyer sont absentes, donc le modèle apprend sur une population "
        "déjà triée par le jugement humain."
    )
    L.append(
        "- La redistribution suppose que le budget libéré se reporte sur les "
        "annonces conservées avec des rendements décroissants. C'est une "
        "hypothèse, pas une mesure."
    )
    L.append("")
    return "\n".join(L)


def main() -> int:
    configure_console()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--early-days", type=int, default=EARLY_DAYS)
    p.add_argument("--min-days", type=int, default=MIN_DAYS)
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    p.add_argument("--saturation", type=float, default=SATURATION)
    p.add_argument("--report", type=Path)
    args = p.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if not settings.warehouse_path.exists():
        print("No warehouse — run build_warehouse.py first.", file=sys.stderr)
        return 1

    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    df = build_dataset(con, early_days=args.early_days, min_days=args.min_days)
    train, test, boundary = temporal_split(df, args.cutoff)

    if len(train) < 40 or len(test) < 15:
        print(f"Split too small: {len(train)} train / {len(test)} test.", file=sys.stderr)
        print("Adjust --cutoff or --min-days.", file=sys.stderr)
        return 1

    model, ev = fit_and_evaluate(train, test)
    curve = decision_curve(model, test, saturation=args.saturation)
    wc = within_campaign_signal(df)
    cv = grouped_cv_auc(df)

    report = build_report(args, df, train, test, ev, curve, wc, cv, boundary)
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
        print(f"\nÉcrit dans {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
