"""Funnel, monthly trend, ad lifetime and the fatigue-absence evidence.

Interactive equivalents of make_figures.py::fig_funnel / fig_lifetime /
fig_fatigue — same SQL and the same computation, reused verbatim, only the
rendering changes from a static PNG to a hoverable Plotly chart.
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
from plotly.subplots import make_subplots

from app.lib.data_access import get_connection, open_connection, require_views, source_info

INK = "#1f2933"
ACCENT = "#2f6f9f"
WARN = "#c0623d"
GREY = "#9aa5b1"

st.set_page_config(page_title="Entonnoir — Meta Ads Cockpit", page_icon="📊", layout="wide")
st.title("Entonnoir & cycle de vie")

con = get_connection()
if not require_views(con, "funnel_by_month", "ad_lifecycle", "ads_daily"):
    st.stop()


@st.cache_data(show_spinner=False)
def _funnel_totals(db_path: str, mtime: float) -> dict:
    con = open_connection(db_path, mtime)
    row = con.sql("""
        SELECT SUM(impressions) AS imp, SUM(link_click) AS lc,
               SUM(landing_page_view) AS lpv, SUM(add_to_cart) AS atc,
               SUM(initiate_checkout) AS ic, SUM(purchase) AS p
        FROM ads_daily WHERE spend > 0
    """).df().iloc[0]
    return row.to_dict()


@st.cache_data(show_spinner=False)
def _funnel_monthly(db_path: str, mtime: float):
    con = open_connection(db_path, mtime)
    return con.sql("SELECT * FROM funnel_by_month ORDER BY month").df()


@st.cache_data(show_spinner=False)
def _lifetime_days(db_path: str, mtime: float) -> np.ndarray:
    con = open_connection(db_path, mtime)
    df = con.sql("SELECT active_days FROM ad_lifecycle ORDER BY 1").df()
    return df.active_days.to_numpy()


@st.cache_data(show_spinner="Calcul des pentes de CTR par annonce…")
def _fatigue_data(db_path: str, mtime: float):
    con = open_connection(db_path, mtime)
    df = con.sql("""
        SELECT ad_id, ctr, spend,
               ROW_NUMBER() OVER (PARTITION BY ad_id ORDER BY date) AS day_n
        FROM ads_daily WHERE spend > 0 AND impressions > 0
    """).df()
    life = df.groupby("ad_id").day_n.max()
    long_ads = life[life >= 12].index
    sub = df[df.ad_id.isin(long_ads)]

    profile = sub.groupby("day_n").agg(n=("ad_id", "nunique"), ctr=("ctr", "median")).reset_index()
    profile = profile[profile.n >= 10]

    slopes = []
    for _, g in sub.groupby("ad_id"):
        g = g.sort_values("day_n")
        if len(g) >= 8 and g.ctr.std() > 0:
            slopes.append(np.polyfit(g.day_n, g.ctr, 1)[0] / g.ctr.mean() * 100)
    return profile, np.array(slopes), int(len(long_ads))


_info = source_info()
_db_path, _mtime = str(_info["path"]), _info["path"].stat().st_mtime

# ---------------------------------------------------------------- funnel ---
st.subheader("Entonnoir")
totals = _funnel_totals(_db_path, _mtime)
labels = ["Impressions", "Clics lien", "Vues de page", "Panier", "Checkout", "Achat"]
values = [totals["imp"], totals["lc"], totals["lpv"], totals["atc"], totals["ic"], max(totals["p"], 1)]
colors = [ACCENT] * 5 + [WARN]

fig = go.Figure(go.Bar(
    x=values, y=labels, orientation="h",
    marker_color=colors,
    text=[f"{int(v):,}".replace(",", " ") for v in values],
    textposition="outside",
    hovertemplate="%{y} : %{x:,.0f}<extra></extra>",
))
fig.update_layout(
    xaxis_type="log", xaxis_title="Volume (échelle logarithmique)",
    yaxis={"autorange": "reversed"},
    height=340, margin=dict(l=10, r=10, t=10, b=10),
    plot_bgcolor="white",
)
st.plotly_chart(fig, width="stretch")

if totals["p"] < 100 and totals["ic"] > 0:
    pass_rate = totals["p"] / totals["ic"] * 100
    st.warning(
        f"Taux de passage checkout → achat : **{pass_rate:.2f} %** "
        f"(attendu 40-70 % en e-commerce). Signale un pixel de conversion "
        f"cassé plutôt qu'un problème de campagne."
    )

st.divider()

# ---------------------------------------------------- tendance mensuelle ---
st.subheader("Tendance mensuelle")
monthly = _funnel_monthly(_db_path, _mtime)
st.dataframe(
    monthly,
    hide_index=True,
    width="stretch",
    column_config={
        "month": st.column_config.DateColumn("Mois", format="YYYY-MM"),
        "spend": st.column_config.NumberColumn("Dépense", format="$ %.0f"),
        "impressions": st.column_config.NumberColumn("Impressions", format="%d"),
        "link_clicks": st.column_config.NumberColumn("Clics", format="%d"),
        "landing_page_views": st.column_config.NumberColumn("Vues de page", format="%d"),
        "add_to_cart": st.column_config.NumberColumn("Panier", format="%d"),
        "initiate_checkout": st.column_config.NumberColumn("Checkout", format="%d"),
        "purchase": st.column_config.NumberColumn("Achat", format="%d"),
        "lpv_to_cart_pct": st.column_config.NumberColumn("Vue → panier", format="%.2f %%"),
        "cart_to_checkout_pct": st.column_config.NumberColumn("Panier → checkout", format="%.2f %%"),
        "checkout_to_purchase_pct": st.column_config.NumberColumn("Checkout → achat", format="%.2f %%"),
        "cost_per_checkout": st.column_config.NumberColumn("Coût / checkout", format="$ %.2f"),
    },
)

st.divider()

# --------------------------------------------------- durée de vie / survie ---
st.subheader("Durée de vie des annonces")
days = _lifetime_days(_db_path, _mtime)

fig2 = make_subplots(rows=1, cols=2, subplot_titles=("Distribution", "Survie empirique"))
fig2.add_trace(
    go.Histogram(x=days, marker_color=ACCENT, xbins=dict(start=1, end=int(days.max()) + 1, size=1)),
    row=1, col=1,
)
grid = np.arange(1, int(days.max()) + 1)
surv = [(days >= d).mean() for d in grid]
fig2.add_trace(
    go.Scatter(x=grid, y=surv, mode="lines", line_shape="hv", line=dict(color=WARN, width=2.2)),
    row=1, col=2,
)
fig2.update_xaxes(title_text="Jours de diffusion", row=1, col=1)
fig2.update_yaxes(title_text="Nombre d'annonces", row=1, col=1)
fig2.update_xaxes(title_text="Jours de diffusion", row=1, col=2)
fig2.update_yaxes(title_text="Part encore active", range=[0, 1.02], row=1, col=2)
fig2.update_layout(height=320, showlegend=False, margin=dict(l=10, r=10, t=40, b=10))
st.plotly_chart(fig2, width="stretch")

st.divider()

# ------------------------------------------------------ absence de fatigue ---
st.subheader("Absence de fatigue créative")
st.caption(
    "Hypothèse testée et rejetée : si la fatigue créative existait, le CTR "
    "baisserait avec le temps de diffusion. Ce n'est pas le cas ici."
)
profile, slopes, n_long_ads = _fatigue_data(_db_path, _mtime)

if len(slopes) == 0:
    st.info("Pas assez d'annonces avec ≥ 12 jours de diffusion pour ce test.")
else:
    fig3 = make_subplots(rows=1, cols=2, subplot_titles=(
        f"CTR médian par jour ({n_long_ads} annonces ≥ 12 j)", "Pente du CTR par annonce",
    ))
    fig3.add_trace(
        go.Scatter(x=profile.day_n, y=profile.ctr, mode="lines+markers",
                    line=dict(color=ACCENT, width=1.8), marker=dict(size=5)),
        row=1, col=1,
    )
    fig3.add_trace(
        go.Histogram(x=slopes, marker_color=GREY, nbinsx=14),
        row=1, col=2,
    )
    fig3.add_vline(x=0, line_dash="dash", line_color=INK, row=1, col=2)
    fig3.add_vline(x=float(slopes.mean()), line_color=WARN, line_width=2, row=1, col=2)
    fig3.update_xaxes(title_text="Jour de diffusion", row=1, col=1)
    fig3.update_yaxes(title_text="CTR médian (%)", rangemode="tozero", row=1, col=1)
    fig3.update_xaxes(title_text="Pente (%/jour)", row=1, col=2)
    fig3.update_yaxes(title_text="Nombre d'annonces", row=1, col=2)
    fig3.update_layout(height=320, showlegend=False, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig3, width="stretch")

    st.caption(
        f"Pente moyenne : {slopes.mean():+.2f} %/jour sur {len(slopes)} annonces "
        "— proche de zéro, cohérent avec l'absence de fatigue documentée dans le rapport."
    )
