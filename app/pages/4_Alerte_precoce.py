"""Day-2 early-warning model — the module that rejected creative fatigue and
compares a trained model against the simple "checkouts per dollar" rule.

Two caches on purpose: refitting (early_days/min_days/cutoff) is the real
cost — GroupKFold, two logistic regressions per fold, a full bootstrap CI.
Moving the saturation slider only recomputes decision_curve(), which reuses
the already-fitted model and costs almost nothing.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plotly.graph_objects as go
import streamlit as st

from analysis.early_warning import (
    DEFAULT_CUTOFF,
    EARLY_DAYS,
    MIN_DAYS,
    SATURATION,
    bootstrap_auc_ci,
    build_dataset,
    decision_curve,
    fit_and_evaluate,
    grouped_cv_auc,
    temporal_split,
    within_campaign_signal,
)
from app.lib.data_access import get_connection, open_connection, require_views, source_info
from run_early_warning import build_report

INK = "#1f2933"
ACCENT = "#2f6f9f"
WARN = "#c0623d"
GOOD = "#3f7d5a"
GREY = "#9aa5b1"

st.set_page_config(page_title="Alerte précoce — Meta Ads Cockpit", page_icon="📊", layout="wide")
st.title("Alerte précoce (J+2)")
st.caption(
    "Une annonce vaut-elle la peine d'être continuée, à partir de ses deux "
    "premiers jours de diffusion ? Modèle entraîné et comparé à une règle simple."
)

con = get_connection()
if not require_views(con, "ads_daily"):
    st.stop()

# ------------------------------------------------------------- controls ---
c1, c2, c3, c4 = st.columns(4)
early_days = c1.number_input("Jours de fenêtre précoce", value=EARLY_DAYS, min_value=1, max_value=5, step=1)
min_days = c2.number_input("Durée de vie minimale", value=MIN_DAYS, min_value=int(early_days) + 1, max_value=15, step=1)
cutoff = c3.date_input("Frontière temporelle", value=dt.date.fromisoformat(DEFAULT_CUTOFF))
saturation = c4.slider("Saturation (courbe de décision)", 0.5, 1.0, SATURATION, 0.05)


@st.cache_data(show_spinner="Entraînement et validation croisée groupée…")
def cached_fit(db_path: str, mtime: float, early_days: int, min_days: int, cutoff: str) -> dict:
    connection = open_connection(db_path, mtime)
    df = build_dataset(connection, early_days=early_days, min_days=min_days)
    train, test, boundary = temporal_split(df, cutoff)

    result = {"df": df, "train": train, "test": test, "boundary": boundary, "ok": False}
    if len(train) < 40 or len(test) < 15:
        return result

    model, ev = fit_and_evaluate(train, test)
    y_full = (df.late_icr > df.late_icr.median()).astype(int).to_numpy()
    uni_ci = bootstrap_auc_ci(y_full, df.e_icr.to_numpy())

    result.update(
        ok=True, model=model, ev=ev,
        wc=within_campaign_signal(df),
        cv=grouped_cv_auc(df),
        uni_ci=uni_ci,
    )
    return result


@st.cache_data(show_spinner=False)
def cached_curve(db_path: str, mtime: float, early_days: int, min_days: int, cutoff: str, saturation: float):
    fit = cached_fit(db_path, mtime, early_days, min_days, cutoff)
    if not fit["ok"]:
        return None
    return decision_curve(fit["model"], fit["test"], saturation=saturation)


_info = source_info()
_db_path, _mtime = str(_info["path"]), _info["path"].stat().st_mtime
_cutoff_str = cutoff.isoformat()

fit = cached_fit(_db_path, _mtime, int(early_days), int(min_days), _cutoff_str)

st.caption(
    f"{len(fit['train'])} entraînement / {len(fit['test'])} test "
    f"(frontière au {fit['boundary'].date()})"
)

if not fit["ok"]:
    st.warning(
        f"Échantillon trop petit pour entraîner ({len(fit['train'])} train, "
        f"{len(fit['test'])} test — minimum 40/15). Élargissez la fenêtre "
        f"précoce, réduisez la durée de vie minimale, ou déplacez la frontière."
    )
    st.stop()

ev = fit["ev"]
cv = fit["cv"]
wc = fit["wc"]

st.divider()

# ------------------------------------------------------------------ KPIs ---
k1, k2, k3, k4 = st.columns(4)
k1.metric("Entraînement / test", f"{ev.n_train} / {ev.n_test}")
k2.metric("Seuil « bonne annonce »", f"{ev.threshold:.4f} conv/$")
k3.metric("Part de positifs (test)", f"{ev.positive_rate_test:.0%}")
k4.metric("Score de Brier", f"{ev.brier:.3f}")

st.divider()

# ------------------------------------------------------------ forest plot ---
st.subheader("AUC — modèle contre règle simple")
rows = [
    ("Checkouts/$ seul\n(VC groupée)", cv.get("auc_univariate"), fit["uni_ci"]),
    ("Modèle 6 variables\n(VC groupée)", cv.get("auc"), cv.get("ci")),
    (f"Modèle 6 variables\n(avant/après {fit['boundary'].date()})", ev.auc_model, ev.auc_ci),
]
rows = [r for r in rows if r[1] is not None]

fig = go.Figure()
for i, (label, auc, ci) in enumerate(rows):
    lo, hi = ci
    colour = GOOD if lo > 0.5 else WARN
    fig.add_trace(go.Scatter(
        x=[lo, hi], y=[label, label], mode="lines",
        line=dict(color=colour, width=4), showlegend=False,
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=[auc], y=[label], mode="markers+text",
        marker=dict(color=colour, size=11),
        text=[f"{auc:.2f}"], textposition="middle right",
        showlegend=False,
        hovertemplate=f"{label}<br>AUC %{{x:.3f}}<extra></extra>",
    ))
fig.add_vline(x=0.5, line_dash="dash", line_color=INK)
fig.update_layout(
    xaxis_title="AUC (intervalle de confiance à 95 %)",
    xaxis_range=[0.3, 0.95],
    height=260, margin=dict(l=10, r=60, t=10, b=10),
    plot_bgcolor="white",
)
st.plotly_chart(fig, width="stretch")

if cv.get("auc_univariate") is not None and cv.get("auc") is not None:
    if cv["auc_univariate"] > cv["auc"]:
        st.info(
            f"La règle simple (checkouts/\\$, AUC {cv['auc_univariate']:.2f}) bat le "
            f"modèle à {len(fit['ev'].coefficients)} variables (AUC {cv['auc']:.2f}) "
            f"en validation croisée. **Recommandation : déployer la règle, pas le modèle.**"
        )

st.divider()

# --------------------------------------------------- coefficients / calib ---
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("Coefficients (variables standardisées)")
    coefs = ev.coefficients
    colors = [GOOD if v > 0 else WARN for v in coefs.values]
    fig_c = go.Figure(go.Bar(
        x=coefs.values, y=coefs.index, orientation="h", marker_color=colors,
        hovertemplate="%{y} : %{x:.3f}<extra></extra>",
    ))
    fig_c.add_vline(x=0, line_color=INK, line_width=1)
    fig_c.update_layout(
        yaxis={"autorange": "reversed"}, height=260,
        margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="white",
    )
    st.plotly_chart(fig_c, width="stretch")

with col_b:
    st.subheader("Calibration (test)")
    if ev.calibration.empty:
        st.info("Pas assez d'observations pour des tranches de calibration.")
    else:
        st.dataframe(
            ev.calibration, hide_index=True, width="stretch",
            column_config={
                "n": st.column_config.NumberColumn("N", format="%d"),
                "predicted": st.column_config.NumberColumn("Prédit", format="%.3f"),
                "observed": st.column_config.NumberColumn("Observé", format="%.3f"),
            },
        )
        st.caption("Une bonne calibration a « prédit » proche d'« observé » sur chaque ligne.")

st.divider()

# --------------------------------------------------------- decision curve ---
st.subheader("Courbe de décision (test uniquement)")
st.caption(
    "Gain projeté en coupant les annonces les moins bien notées au jour "
    f"{int(early_days)}. La courbe entière est montrée volontairement : choisir "
    "le meilleur point en la lisant réintroduirait le biais de sélection que "
    "la séparation temporelle sert à éliminer."
)
curve = cached_curve(_db_path, _mtime, int(early_days), int(min_days), _cutoff_str, saturation)
if curve is None or curve.empty:
    st.info("Pas assez de données de test pour construire la courbe.")
else:
    st.dataframe(
        curve, hide_index=True, width="stretch",
        column_config={
            "cut_fraction": st.column_config.NumberColumn("Fraction coupée", format="%.0f %%"),
            "ads_cut": st.column_config.NumberColumn("Annonces coupées", format="%d"),
            "spend_freed": st.column_config.NumberColumn("Budget libéré", format="$ %.0f"),
            "conv_lost": st.column_config.NumberColumn("Conversions perdues", format="%.0f"),
            "projected_conv": st.column_config.NumberColumn("Conversions projetées", format="%.0f"),
            "gain_pct": st.column_config.NumberColumn("Gain", format="%+.1f %%"),
        },
    )
    st.caption(
        f"Référence : {curve.attrs.get('baseline_conv', 0):.0f} checkouts pour "
        f"{curve.attrs.get('baseline_spend', 0):.0f} $."
    )

st.divider()

# ----------------------------------------------- rapport complet / verdict ---
st.subheader("Rapport complet")
args = SimpleNamespace(early_days=int(early_days), min_days=int(min_days), saturation=saturation)
report_text = build_report(args, fit["df"], fit["train"], fit["test"], ev, curve, wc, cv, fit["boundary"])

st.download_button(
    "Télécharger le rapport (.md)",
    data=report_text,
    file_name="rapport_early_warning.md",
    mime="text/markdown",
)
with st.expander("Afficher le rapport complet", expanded=False):
    st.markdown(report_text)
