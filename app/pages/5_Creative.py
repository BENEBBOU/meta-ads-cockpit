"""Creative content analysis — the attention/conversion inversion and the
caps_ratio confound story.

Two targets are always computed for the slope chart (it is the comparison
between CTR and checkouts/$ that carries the finding); the table, AUC and
thumbnail gallery below focus on whichever target is currently selected.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import plotly.graph_objects as go
import streamlit as st

from analysis.creative import ALL_FEATURES, IMAGE_FEATURES, TEXT_FEATURES, build_dataset, model_cv, univariate_associations
from app.lib.data_access import active_data_dir, get_connection, open_connection, require_views, source_info
from run_creative_analysis import TARGETS, build_report

INK = "#1f2933"
ACCENT = "#2f6f9f"
WARN = "#c0623d"
GOOD = "#3f7d5a"
GREY = "#9aa5b1"

# Mirrors make_figures.py::fig_creative — a display label mapping, not a
# statistical computation, so duplicating it here (rather than importing a
# name that function does not expose at module level) does not risk the two
# ever disagreeing on anything that matters.
SLOPE_LABELS = {
    "is_video": "Vidéo", "aspect_ratio": "Format vertical", "is_reel": "Reel",
    "contrast": "Contraste", "colorfulness": "Colorfulness",
    "saturation": "Saturation", "has_date": "Mention de date",
    "n_emoji": "Nombre d'emojis", "n_question": "Question",
}

st.set_page_config(page_title="Créative — Meta Ads Cockpit", page_icon="📊", layout="wide")
st.title("Analyse créative")
st.caption(
    "Ce que le texte et l'image d'une annonce expliquent de sa performance — "
    "et l'inversion entre ce qui attire l'attention et ce qui convertit."
)

con = get_connection()
if not require_views(con, "ads_daily"):
    st.stop()

data_dir = active_data_dir()
creatives_path = data_dir / "creatives.parquet"
thumbs_dir = data_dir / "thumbnails"

if not creatives_path.exists():
    st.info(
        "Aucune créative extraite pour cette source. Lancez "
        "`python run_backfill.py creatives` (données réelles) ou régénérez "
        "le jeu synthétique depuis la page Pipeline."
    )
    st.stop()

min_spend = st.number_input("Dépense minimale par annonce ($)", value=20.0, min_value=0.0, step=5.0)


@st.cache_data(show_spinner="Chargement des créatives (texte + vignettes)…")
def cached_dataset(db_path: str, mtime: float, creatives_path: str, thumbs_dir: str, min_spend: float):
    connection = open_connection(db_path, mtime)
    return build_dataset(connection, Path(creatives_path), Path(thumbs_dir), min_spend=min_spend)


@st.cache_data(show_spinner=False)
def cached_associations(db_path: str, mtime: float, creatives_path: str, thumbs_dir: str, min_spend: float, target: str):
    df = cached_dataset(db_path, mtime, creatives_path, thumbs_dir, min_spend)
    return univariate_associations(df, target)


@st.cache_data(show_spinner="Validation croisée (logistique + gradient boosting)…")
def cached_model_cv(db_path: str, mtime: float, creatives_path: str, thumbs_dir: str, min_spend: float, target: str):
    df = cached_dataset(db_path, mtime, creatives_path, thumbs_dir, min_spend)
    return model_cv(df, target)


_info = source_info()
_db_path, _mtime = str(_info["path"]), _info["path"].stat().st_mtime
_creatives_str, _thumbs_str = str(creatives_path), str(thumbs_dir)

df = cached_dataset(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend)

if len(df) < 40:
    st.warning(f"Seulement {len(df)} annonce(s) après filtrage — trop peu pour une analyse fiable.")
    st.stop()

st.caption(f"{len(df)} annonces · {df.campaign_id.nunique()} campagnes")
st.divider()

# ------------------------------------------------------ slope chart ---
st.subheader("Ce qui gagne l'attention n'est pas ce qui convertit")

assoc_ctr = cached_associations(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend, "ctr").set_index("feature")
assoc_icr = cached_associations(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend, "icr").set_index("feature")

keep = [
    f for f in SLOPE_LABELS
    if f in assoc_ctr.index and f in assoc_icr.index
    and (bool(assoc_ctr.loc[f, "holds_within"]) or bool(assoc_icr.loc[f, "holds_within"]))
]

if not keep:
    st.info("Aucun trait créatif ne tient encore une fois l'effet campagne retiré, avec ces réglages.")
else:
    left = np.array([float(assoc_ctr.loc[f, "rho_within"]) for f in keep])
    right = np.array([float(assoc_icr.loc[f, "rho_within"]) for f in keep])

    fig = go.Figure()
    for i, f in enumerate(keep):
        colour = WARN if left[i] > right[i] else GOOD
        fig.add_trace(go.Scatter(
            x=[0, 1], y=[left[i], right[i]], mode="lines+markers",
            line=dict(color=colour, width=2.2), marker=dict(size=7),
            showlegend=False,
            hovertemplate=f"{SLOPE_LABELS[f]}<br>CTR : {left[i]:+.2f}<br>"
                           f"Checkouts/$ : {right[i]:+.2f}<extra></extra>",
        ))
        fig.add_annotation(x=-0.05, y=left[i], text=SLOPE_LABELS[f],
                            showarrow=False, xanchor="right", font=dict(size=11))
        fig.add_annotation(x=1.05, y=right[i], text=f"{right[i]:+.2f}",
                            showarrow=False, xanchor="left", font=dict(size=11))
    fig.add_hline(y=0, line_dash="dash", line_color=INK)
    fig.update_xaxes(
        tickvals=[0, 1], ticktext=["CTR<br>(attention)", "Checkouts/$<br>(conversion)"],
        range=[-0.45, 1.45],
    )
    fig.update_yaxes(title_text="Corrélation intra-campagne (rho)")
    fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="white",
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Rouge : le trait gagne en attention et perd en conversion. Vert : "
        "l'inverse. Seuls les traits qui tiennent encore une fois l'effet "
        "campagne retiré (`holds_within`) sont affichés."
    )

st.divider()

# --------------------------------------------------------- target detail ---
target = st.radio(
    "Détail pour la cible :", options=list(TARGETS), format_func=lambda t: TARGETS[t],
    horizontal=True,
)

assoc = cached_associations(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend, target)

col_a, col_b = st.columns([3, 2])

with col_a:
    st.subheader("Associations univariées")
    if assoc.empty:
        st.info("Aucune variable exploitable pour cette cible.")
    else:
        st.dataframe(
            assoc[["feature", "n", "rho", "q_value", "significant", "rho_within", "holds_within"]],
            hide_index=True, width="stretch",
            column_config={
                "feature": st.column_config.TextColumn("Variable"),
                "n": st.column_config.NumberColumn("N", format="%d"),
                "rho": st.column_config.NumberColumn("rho (brut)", format="%.3f"),
                "q_value": st.column_config.NumberColumn("q (FDR)", format="%.3g"),
                "significant": st.column_config.CheckboxColumn("Sig. (brut)"),
                "rho_within": st.column_config.NumberColumn("rho (intra-campagne)", format="%.3f"),
                "holds_within": st.column_config.CheckboxColumn("Tient en intra"),
            },
        )
        n_sig = int(assoc.significant.sum())
        n_within = int(assoc.holds_within.sum())
        st.caption(
            f"{n_sig} variable(s) sur {len(assoc)} survivent au contrôle FDR ; "
            f"{n_within} tiennent encore une fois l'effet de campagne retiré."
        )

with col_b:
    st.subheader("Modèle (VC groupée par campagne)")
    cv = cached_model_cv(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend, target)
    rows = [
        {"modèle": name, "AUC": r["auc"], "IC bas": r["ci"][0], "IC haut": r["ci"][1], "n": r["n"]}
        for name, r in cv.items() if isinstance(r, dict)
    ]
    if not rows:
        st.info("Pas assez de données pour un modèle sur cette cible.")
    else:
        import pandas as pd
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch",
            column_config={
                "AUC": st.column_config.NumberColumn(format="%.3f"),
                "IC bas": st.column_config.NumberColumn(format="%.3f"),
                "IC haut": st.column_config.NumberColumn(format="%.3f"),
                "n": st.column_config.NumberColumn(format="%d"),
            },
        )
        st.caption(
            f"{cv.get('n_features')} variable(s), {cv.get('n_splits')} plis. "
            "Le gradient boosting est inclus comme témoin : s'il ne fait pas "
            "mieux que la régression logistique, le plafond vient des données, "
            "pas du modèle."
        )

st.divider()

# -------------------------------------------------------------- gallery ---
st.subheader(f"Meilleures / pires créatives — {TARGETS[target]}")
metric_col = "ctr" if target == "ctr" else "icr"
ranked = df.sort_values(metric_col, ascending=False)


def _show_row(rows, label):
    st.caption(label)
    cols = st.columns(5)
    for col, (_, row) in zip(cols, rows.iterrows()):
        thumb = thumbs_dir / f"{row.ad_id}.jpg"
        with col:
            if thumb.exists():
                st.image(str(thumb), width="stretch")
            else:
                st.caption("vignette indisponible")
            unit = "%" if target == "ctr" else "$/$"
            st.caption(f"{row[metric_col]:.2f} {unit}\n\n{str(row.ad_name)[:40]}")


_show_row(ranked.head(5), "Meilleures")
_show_row(ranked.tail(5).iloc[::-1], "Pires")

st.divider()

# ---------------------------------------------------------------- report ---
st.subheader("Rapport complet")
full_results = {
    t: (
        cached_associations(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend, t),
        cached_model_cv(_db_path, _mtime, _creatives_str, _thumbs_str, min_spend, t),
    )
    for t in TARGETS
}
report_text = build_report(df, full_results)

st.download_button(
    "Télécharger le rapport (.md)",
    data=report_text,
    file_name="rapport_creative.md",
    mime="text/markdown",
)
with st.expander("Afficher le rapport complet", expanded=False):
    st.markdown(report_text)
