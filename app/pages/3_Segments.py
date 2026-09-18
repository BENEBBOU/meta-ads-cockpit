"""Segment ranking and reallocation sizing — the biggest analytical page.

A thin UI over analysis/segments.py::analyse and analysis/reallocation.py::
simulate. The bootstrap and FDR correction happen exactly once per parameter
set (cached); toggling "significant only" or sorting the table afterwards
never re-triggers them.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plotly.graph_objects as go
import streamlit as st

from analysis import reallocation
from analysis.segments import DEFAULT_CONVERSION, SEGMENT_SPECS, analyse, available_views
from app.lib.data_access import get_connection, open_connection, source_info
from run_segment_analysis import format_analysis

INK = "#1f2933"
ACCENT = "#2f6f9f"
WARN = "#c0623d"
GOOD = "#3f7d5a"
GREY = "#9aa5b1"

CONVERSION_OPTIONS = [
    "link_click", "landing_page_view", "add_to_cart", "initiate_checkout", "purchase",
]

st.set_page_config(page_title="Segments — Meta Ads Cockpit", page_icon="📊", layout="wide")
st.title("Segments & réallocation")

con = get_connection()
views = available_views(con)

runnable = sorted(name for name, spec in SEGMENT_SPECS.items() if spec.view in views)
skipped = sorted(name for name, spec in SEGMENT_SPECS.items() if spec.view not in views)

if not runnable:
    st.info(
        "Aucune vue de segmentation construite dans cette source. "
        "Extrayez une passe segmentée (`age_gender`, `placement`, `region` ou "
        "`hourly`) puis relancez `python build_warehouse.py`."
    )
    st.stop()

if skipped:
    st.caption("Pas encore extrait(e) : " + ", ".join(f"`{s}`" for s in skipped))


@st.cache_data(show_spinner="Bootstrap sur les segments…")
def _analyse(db_path, mtime, spec_name, conversion, min_expected, min_spend_share, fdr_alpha, n_boot):
    connection = open_connection(db_path, mtime)
    return analyse(
        connection, SEGMENT_SPECS[spec_name],
        conversion=conversion, min_expected=min_expected,
        min_spend_share=min_spend_share, fdr_alpha=fdr_alpha, n_boot=n_boot,
    )


_info = source_info()
_db_path, _mtime = str(_info["path"]), _info["path"].stat().st_mtime

# ------------------------------------------------------------- controls ---
c1, c2 = st.columns([2, 3])
with c1:
    dimension = st.selectbox(
        "Dimension", options=runnable,
        format_func=lambda n: f"{n} — {SEGMENT_SPECS[n].description}",
    )
spec = SEGMENT_SPECS[dimension]

with c2:
    if spec.conversion:
        st.selectbox("Conversion", options=[spec.conversion], disabled=True,
                     help="Imposée par cette segmentation — aucune conversion "
                          "pixel n'est disponible sur ce découpage.")
        conversion = spec.conversion
    else:
        conversion = st.selectbox(
            "Conversion", options=CONVERSION_OPTIONS,
            index=CONVERSION_OPTIONS.index(DEFAULT_CONVERSION),
        )

with st.expander("Paramètres statistiques", expanded=False):
    p1, p2, p3, p4 = st.columns(4)
    min_expected = p1.number_input("Min. conversions attendues", value=5.0, min_value=0.0, step=1.0)
    min_spend_share = p2.number_input(
        "Part min. de dépense", value=0.002, min_value=0.0, max_value=1.0,
        step=0.001, format="%.3f",
    )
    fdr_alpha = p3.number_input("Seuil FDR (alpha)", value=0.05, min_value=0.01, max_value=0.5, step=0.01)
    n_boot = p4.number_input("Tirages bootstrap", value=2000, min_value=200, max_value=5000, step=200)

if not spec.allow_reallocation:
    st.caption(
        "⚠️ Classement seul : ce découpage n'expose aucune conversion pixel, "
        "l'issue mesurée est un proxy — aucune recommandation budgétaire n'en "
        "est tirée."
    )

result = _analyse(_db_path, _mtime, dimension, conversion, min_expected, min_spend_share, fdr_alpha, int(n_boot))

if result.frame.empty:
    st.warning("Aucun segment n'a atteint le seuil minimal pour être testé.")
    st.stop()

st.caption(
    f"Taux compte : **{result.global_rate:.4f} conv/$** "
    f"({result.total_conversions:,.0f} conversions sur {result.total_spend:,.0f} $) · "
    f"dispersion φ = {result.dispersion:.1f} · {len(result.frame)} segment(s) testé(s)"
)

st.divider()

# --------------------------------------------------------------- table ---
frame = result.frame.copy()
frame["perf_ci_low"] = frame["rate_ci_low"] / result.global_rate
frame["perf_ci_high"] = frame["rate_ci_high"] / result.global_rate

only_significant = st.checkbox("Significatifs uniquement", value=False)
shown = frame[frame["significant"]] if only_significant else frame

st.subheader(f"Classement ({len(shown)} segment(s))")
st.dataframe(
    shown[["segment", "spend", "conversions", "cost_per_conv", "perf_index",
           "n_ads", "q_value", "significant"]],
    hide_index=True,
    width="stretch",
    column_config={
        "segment": st.column_config.TextColumn("Segment"),
        "spend": st.column_config.NumberColumn("Dépense", format="$ %.0f"),
        "conversions": st.column_config.NumberColumn("Conversions", format="%.0f"),
        "cost_per_conv": st.column_config.NumberColumn("Coût / conv.", format="$ %.2f"),
        "perf_index": st.column_config.NumberColumn("Index", format="%.2f",
                                                       help="1.0 = moyenne du compte"),
        "n_ads": st.column_config.NumberColumn("N annonces", format="%d"),
        "q_value": st.column_config.NumberColumn("q (FDR)", format="%.3g"),
        "significant": st.column_config.CheckboxColumn("Significatif"),
    },
)

# ---------------------------------------------------------- forest plot ---
def _color(row) -> str:
    if not row["significant"]:
        return GREY
    return GOOD if row["perf_index"] > 1 else WARN


colors = shown.apply(_color, axis=1)
fig = go.Figure(go.Scatter(
    x=shown["perf_index"], y=shown["segment"], mode="markers",
    marker=dict(color=colors, size=9),
    error_x=dict(
        type="data", symmetric=False,
        array=(shown["perf_ci_high"] - shown["perf_index"]).clip(lower=0),
        arrayminus=(shown["perf_index"] - shown["perf_ci_low"]).clip(lower=0),
        color=GREY, thickness=1.2, width=0,
    ),
    hovertemplate="%{y}<br>index %{x:.2f}<extra></extra>",
))
fig.add_vline(x=1.0, line_dash="dash", line_color=INK)
fig.update_layout(
    xaxis_title="Index de performance (IC 95 %, bootstrap par cluster)",
    yaxis={"autorange": "reversed"},
    height=max(320, 30 * len(shown) + 90),
    margin=dict(l=10, r=10, t=10, b=10),
    plot_bgcolor="white",
)
st.plotly_chart(fig, width="stretch")

if not result.excluded.empty:
    with st.expander(f"{len(result.excluded)} segment(s) exclu(s) — "
                      f"{result.excluded['spend'].sum():,.0f} $ de dépense"):
        st.dataframe(result.excluded, hide_index=True, width="stretch")

st.divider()

# ------------------------------------------------------------ download ---
top_n = st.number_input("Lignes par table dans le rapport téléchargé", value=10, min_value=3, max_value=50)
st.download_button(
    "Télécharger le rapport (.md)",
    data=format_analysis(result, top=int(top_n)),
    file_name=f"rapport_segments_{dimension}.md",
    mime="text/markdown",
)

# --------------------------------------------------------- réallocation ---
if spec.allow_reallocation:
    st.divider()
    st.subheader("Simulation de réallocation")
    st.caption(
        "Seuls les segments significatifs participent. Les receveurs héritent "
        "du budget libéré au prorata de leur dépense actuelle."
    )

    r1, r2, r3 = st.columns(3)
    saturation = r1.slider("Saturation (alpha)", 0.5, 1.0, reallocation.DEFAULT_SATURATION, 0.05)
    donor_index_max = r2.slider("Index max. donneur", 0.3, 1.0, 1.0, 0.05)
    recipient_index_min = r3.slider("Index min. receveur", 1.0, 2.0, 1.0, 0.05)

    sim = reallocation.simulate(
        result.frame, saturation=saturation,
        donor_index_max=donor_index_max, recipient_index_min=recipient_index_min,
    )

    if sim is None:
        st.info("Pas de paire donneur/receveur statistiquement séparée avec ces réglages.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Budget déplacé", f"{sim.freed_spend:,.0f} $")
        m2.metric("Donneurs / receveurs", f"{len(sim.donors)} / {len(sim.recipients)}")
        m3.metric(
            "Gain (plafond linéaire)",
            f"{sim.linear_gain:+,.0f}",
            f"{sim.linear_gain_pct:+.1f} %",
        )
        m4.metric(
            "Gain (avec saturation)",
            f"{sim.saturated_gain:+,.0f}",
            f"{sim.saturated_gain_pct:+.1f} %",
        )

        st.caption("Plus gros donneurs")
        st.dataframe(
            sim.donors[["segment", "spend", "conversions", "cost_per_conv"]].head(5),
            hide_index=True, width="stretch",
            column_config={
                "segment": st.column_config.TextColumn("Segment"),
                "spend": st.column_config.NumberColumn("Dépense", format="$ %.0f"),
                "conversions": st.column_config.NumberColumn("Conversions", format="%.0f"),
                "cost_per_conv": st.column_config.NumberColumn("Coût / conv.", format="$ %.2f"),
            },
        )
