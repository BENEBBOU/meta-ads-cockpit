"""Meta Ads Cockpit — landing page.

Run from meta-ads-backfill/, exactly like every run_*.py script (Settings and
make_figures.OUT are paths relative to the current working directory):

    streamlit run app/Home.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `analysis`, `meta_backfill`, `build_warehouse`, `app.lib` importable —
# every page file needs this same block, since Streamlit executes each page
# as its own top-level script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from app.lib.data_access import active_data_dir, get_connection, source_info
from analysis.segments import available_views
from build_warehouse import PASS_VIEWS
from meta_backfill.checkpoint import Checkpoint

INK = "#1f2933"
WARN = "#c0623d"
GREY = "#9aa5b1"

st.set_page_config(
    page_title="Meta Ads Cockpit",
    page_icon="📊",
    layout="wide",
)

st.title("Meta Ads Cockpit")
st.caption(
    "Extraction, entrepôt et diagnostic statistique d'un compte Meta Ads — "
    "les mêmes modules que les scripts en ligne de commande, navigables."
)

con = get_connection()
info = source_info()

badge_color = WARN if info["kind"] == "real" else GREY
badge_text = "DONNÉES RÉELLES" if info["kind"] == "real" else "DONNÉES SYNTHÉTIQUES"

st.markdown(
    f"""
<div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.4rem;">
  <span style="background:{badge_color};color:white;padding:0.15rem 0.6rem;
        border-radius:999px;font-size:0.75rem;font-weight:600;letter-spacing:0.03em;">
    {badge_text}
  </span>
  <span style="color:{INK};font-size:0.85rem;">
    {info['path']} · {info['size_mb']:.1f} Mo ·
    modifié le {info['modified']:%Y-%m-%d %H:%M}
  </span>
</div>
""",
    unsafe_allow_html=True,
)
st.divider()

views = available_views(con)

if "funnel_by_month" not in views:
    st.info(
        "La vue `funnel_by_month` n'existe pas encore dans cet entrepôt. "
        "Lancez `python build_warehouse.py` après une première extraction."
    )
else:
    funnel = con.sql(
        """
        SELECT
            SUM(spend)                                                    AS spend,
            SUM(initiate_checkout)                                        AS checkouts,
            SUM(purchase)                                                 AS purchases,
            SUM(spend) / NULLIF(SUM(initiate_checkout), 0)                AS cost_per_checkout,
            SUM(purchase) / NULLIF(SUM(initiate_checkout), 0) * 100       AS checkout_to_purchase_pct,
            MIN(month)                                                    AS first_month,
            MAX(month)                                                    AS last_month
        FROM funnel_by_month
        """
    ).df().iloc[0]

    period = ""
    if funnel["first_month"] is not None:
        period = f"{funnel['first_month']:%Y-%m} → {funnel['last_month']:%Y-%m}"

    cols = st.columns(4)
    cols[0].metric("Dépense totale", f"{funnel['spend']:,.0f} $".replace(",", " "))
    cols[1].metric("Checkouts initiés", f"{funnel['checkouts']:,.0f}".replace(",", " "))
    cols[2].metric("Coût par checkout", f"{funnel['cost_per_checkout']:.2f} $")
    cols[3].metric(
        "Checkout → achat",
        f"{funnel['checkout_to_purchase_pct']:.2f} %",
        help="Attendu 40-70 % en e-commerce — un écart ici signale un pixel de "
             "conversion cassé, pas un problème de campagne.",
    )
    if period:
        st.caption(f"Période couverte : {period}")

st.divider()
st.subheader("État de l'extraction")

checkpoint = Checkpoint(active_data_dir() / "checkpoints.json")
rows_per_pass = checkpoint.summary()

if not rows_per_pass:
    st.warning(
        "Aucun checkpoint trouvé — aucune extraction n'a encore été lancée "
        "pour cette source."
    )
else:
    import pandas as pd

    table = pd.DataFrame(
        [
            {
                "passe": pass_name,
                "lignes extraites": rows_per_pass.get(pass_name, 0),
                "vue construite": "oui" if view_name in views else "non",
            }
            for pass_name, view_name in PASS_VIEWS.items()
        ]
    )
    st.dataframe(table, hide_index=True, width="stretch")

st.caption(
    "Les pages Entonnoir, Segments, Alerte précoce, Créative et Pipeline "
    "arrivent aux étapes suivantes de la construction."
)
