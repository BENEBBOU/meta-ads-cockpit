"""Safety net: the exact 8 static figures from the LaTeX report, regenerated
on demand from make_figures.py — not a single line of plotting logic is
duplicated here.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from app.lib.data_access import get_connection, require_views
from make_figures import OUT

FIGURES = [
    ("fig_funnel", "funnel.png", "Entonnoir"),
    ("fig_placement", "placement.png", "Emplacements"),
    ("fig_hourly", "hourly.png", "Heure de la journée"),
    ("fig_lifetime", "lifetime.png", "Durée de vie des annonces"),
    ("fig_demographics", "demographics.png", "Démographie"),
    ("fig_fatigue", "fatigue.png", "Absence de fatigue créative"),
    ("fig_early_warning", "early_warning.png", "Alerte précoce"),
    ("fig_creative", "creative.png", "Créative"),
]

st.set_page_config(page_title="Figures — Meta Ads Cockpit", page_icon="📊", layout="wide")
st.title("Figures du rapport")
st.caption(
    "Les 8 figures exactes du rapport LaTeX, produites par make_figures.py — "
    "utile pour retrouver le rendu statique publié, ou vérifier qu'il "
    "correspond toujours à l'entrepôt actuel."
)

con = get_connection()
if not require_views(con, "ads_daily"):
    st.stop()

st.caption(
    "`fig_creative` lit toujours `creatives.parquet` depuis le dossier de "
    "données réel (`load_settings().data_dir`), quelle que soit la source "
    "choisie ci-contre — c'est make_figures.py lui-même qui fonctionne ainsi, "
    "volontairement inchangé ici."
)

if st.button("Régénérer les figures"):
    import make_figures

    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    with st.spinner("Génération des 8 figures…"):
        for func_name, filename, label in FIGURES:
            try:
                getattr(make_figures, func_name)(con)
                results.append((label, True, ""))
            except Exception as exc:  # noqa: BLE001 — one figure's data gap must not stop the rest
                results.append((label, False, str(exc)[:200]))

    ok = sum(1 for _, success, _ in results if success)
    st.success(f"{ok}/{len(results)} figure(s) régénérée(s).")
    for label, success, message in results:
        if not success:
            st.caption(f"⚠️ {label} : {message}")

st.divider()

for func_name, filename, label in FIGURES:
    path = OUT / filename
    st.subheader(label)
    if path.exists():
        st.image(str(path), width="stretch")
    else:
        st.caption("Pas encore générée — clique sur « Régénérer les figures » ci-dessus.")
