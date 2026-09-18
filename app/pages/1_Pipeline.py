"""Pipeline management: trigger a refresh, watch it live, browse the history.

The only page that touches subprocesses and background threads. Unlike every
other page it does NOT call get_connection() — that helper hard-stops the
whole app when neither warehouse exists yet, which would make this exact
page, the one meant to help bootstrap the very first dataset, unusable on a
fresh clone. It renders its own lightweight per-source status instead.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import refresh_all
from app.lib import pipeline_state as ps
from app.lib.data_access import close_all_connections, data_dirs, warehouse_paths
from meta_backfill.checkpoint import Checkpoint
from meta_backfill.config import DEFAULT_PASS_ORDER
from meta_backfill.runner import MAX_MONTHS_BACK, month_windows

INK = "#1f2933"
ACCENT = "#2f6f9f"
WARN = "#c0623d"
GOOD = "#3f7d5a"
GREY = "#9aa5b1"

st.set_page_config(page_title="Pipeline — Meta Ads Cockpit", page_icon="📊", layout="wide")
st.title("Pipeline")
st.caption(
    "Déclencher un rafraîchissement, suivre son avancement en direct, "
    "consulter l'historique des extractions."
)


# --------------------------------------------------------- live run banner ---
@st.fragment(run_every=2)
def _live_status() -> None:
    st.session_state.setdefault("_pipeline_was_locked", False)
    if ps.is_locked():
        st.session_state["_pipeline_was_locked"] = True
        info = ps.lock_info() or {}
        st.info(f"⏳ En cours : **{info.get('action', '?')}** — démarré à {info.get('started_at', '?')}")
        st.code(ps.read_log()[-4000:] or "(en attente de la première sortie…)", language=None)

        with st.expander("Le run semble bloqué (l'app a redémarré, le process a été tué…)"):
            st.caption(
                "N'utilise ceci que si tu es certain qu'aucun rafraîchissement ne "
                "tourne réellement — l'app ne peut pas le savoir à ta place. Un "
                "sous-processus déjà lancé n'est pas arrêté, seul le verrou l'est."
            )
            confirm = st.checkbox("Je confirme qu'aucun run n'est réellement en cours", key="_force_unlock_confirm")
            if st.button("Forcer le déverrouillage", disabled=not confirm, key="_force_unlock_btn"):
                ps.force_unlock()
                st.session_state["_pipeline_was_locked"] = False
                st.rerun()
    elif st.session_state["_pipeline_was_locked"]:
        # The run just finished between two polls of this fragment. One full
        # rerun refreshes everything that lives outside it — the "Lancer"
        # buttons' disabled state, the checkpoint matrices, the history table.
        st.session_state["_pipeline_was_locked"] = False
        st.rerun()


_live_status()
st.divider()


def _checkpoint_heatmap(data_dir: Path) -> None:
    checkpoint_path = data_dir / "checkpoints.json"
    if not checkpoint_path.exists():
        st.caption("Aucune extraction pour cette source.")
        return

    entries = Checkpoint(checkpoint_path).entries()
    if not entries:
        st.caption("Aucune extraction pour cette source.")
        return

    # make_sample_data.py writes one lump "pass:sample" entry per pass rather
    # than real per-month keys (it generates the whole synthetic history in
    # one shot). Keep those out of the calendar grid — mixing a literal
    # "sample" row into sorted YYYY-MM labels reads as a data error — and
    # surface them as a separate line instead of dropping them silently.
    month_re = re.compile(r"^\d{4}-\d{2}$")
    seen_keys = [key for key in entries if ":" in key]
    off_calendar = [key for key in seen_keys if not month_re.match(key.split(":", 1)[1])]
    if off_calendar:
        detail = ", ".join(
            f"{key.split(':', 1)[0]} ({int(entries[key].get('rows', 0)):,})".replace(",", " ")
            for key in sorted(off_calendar)
        )
        st.caption(f"Génération synthétique en un bloc (hors calendrier) : {detail}")

    expected = [w.label for w in month_windows(MAX_MONTHS_BACK)]
    seen = sorted({
        key.split(":", 1)[1] for key in seen_keys
        if month_re.match(key.split(":", 1)[1])
    })
    months = sorted(set(expected) | set(seen))
    passes = list(DEFAULT_PASS_ORDER)

    if not months:
        return

    z, text = [], []
    for month in months:
        row_z, row_text = [], []
        for pass_name in passes:
            meta = entries.get(f"{pass_name}:{month}")
            if meta is None:
                row_z.append(None)
                row_text.append("—")
            else:
                rows = int(meta.get("rows", 0))
                row_z.append(float(np.log1p(rows)))
                row_text.append(f"{rows:,}".replace(",", " "))
        z.append(row_z)
        text.append(row_text)

    fig = go.Figure(go.Heatmap(
        z=z, x=passes, y=months, text=text, texttemplate="%{text}",
        colorscale=[[0, "#eef1f4"], [1, ACCENT]],
        showscale=False, xgap=2, ygap=2,
        hovertemplate="%{y} · %{x} : %{text} lignes<extra></extra>",
    ))
    fig.update_layout(
        height=max(220, 24 * len(months) + 60),
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis={"autorange": "reversed"},
        font=dict(size=10),
    )
    st.plotly_chart(fig, width="stretch")


def _source_header(key: str, label: str) -> Path:
    st.subheader(label)
    dir_path = data_dirs()[key]
    wh_path = warehouse_paths()[key]
    if wh_path.exists():
        size_mb = wh_path.stat().st_size / 1e6
        modified = dt.datetime.fromtimestamp(wh_path.stat().st_mtime)
        st.caption(f"`{dir_path}` · entrepôt {size_mb:.1f} Mo · modifié le {modified:%Y-%m-%d %H:%M}")
    else:
        st.caption(f"`{dir_path}` · aucun entrepôt construit pour l'instant")
    _checkpoint_heatmap(dir_path)
    return dir_path


col_real, col_sample = st.columns(2)

with col_real:
    _source_header("real", "Données réelles")
    with st.expander("Lancer un rafraîchissement", expanded=not ps.is_locked()):
        months_back = st.number_input(
            "Mois récents à réextraire", value=1, min_value=1, max_value=12,
            step=1, key="real_months_back",
        )
        skip_images = st.checkbox(
            "Ne pas retélécharger les vignettes", value=False, key="real_skip_images",
        )
        sheet_id = os.getenv("PORTFOLIO_SHEET_ID", "")
        credentials = "../google_service_account.json"
        if sheet_id:
            st.caption(f"Publiera aussi dans le classeur (`{sheet_id[:14]}…`).")
        else:
            st.caption("Pas de `PORTFOLIO_SHEET_ID` dans `.env` — publication ignorée.")

        if st.button("Lancer", disabled=ps.is_locked(), key="real_launch"):
            # disabled= above only affects the *next* render of this button —
            # it does not stop this click's own script run, so a second tab
            # or a fast double-click can still reach this line while a run is
            # already active. Re-check right here, and let start_run()'s own
            # atomic acquire be the real guard, not just this check. Only
            # rerun on the success path: st.rerun() aborts the script
            # immediately, so a warning shown right before it would never
            # actually reach the browser.
            if ps.is_locked():
                st.warning("Un rafraîchissement est déjà en cours — celui-ci n'a pas démarré.")
            else:
                close_all_connections()
                steps = [
                    (label, cmd, None)
                    for label, cmd in refresh_all.build_steps(
                        int(months_back), sheet_id, credentials, skip_images,
                    )
                ]
                started = ps.start_run(
                    steps, action="Rafraîchissement (réel)",
                    params={"months_back": int(months_back), "skip_images": skip_images},
                )
                if started:
                    st.rerun()
                else:
                    st.warning("Un rafraîchissement vient de démarrer ailleurs — celui-ci n'a pas été lancé.")

with col_sample:
    _source_header("sample", "Données synthétiques")
    with st.expander("Régénérer le jeu synthétique", expanded=not ps.is_locked()):
        n_campaigns = st.number_input(
            "Campagnes fabriquées", value=70, min_value=10, max_value=300,
            step=10, key="sample_campaigns",
        )
        sample_dir = data_dirs()["sample"]
        st.caption(f"Écrira dans `{sample_dir}` — n'affecte jamais les données réelles.")

        if st.button("Régénérer", disabled=ps.is_locked(), key="sample_launch"):
            if ps.is_locked():
                st.warning("Une exécution est déjà en cours — celle-ci n'a pas démarré.")
            else:
                close_all_connections()
                sample_env = os.environ.copy()
                sample_env["BACKFILL_DATA_DIR"] = str(sample_dir)
                steps = [
                    ("Génération des données synthétiques",
                     ["make_sample_data.py", "--out", str(sample_dir), "--force",
                      "--campaigns", str(int(n_campaigns))], None),
                    # build_warehouse.py takes no --data-dir flag: BACKFILL_DATA_DIR
                    # is the only way to point it at the synthetic directory instead
                    # of the real one, without touching build_warehouse.py itself.
                    ("Reconstruction de l'entrepôt (synthétique)",
                     ["build_warehouse.py"], sample_env),
                ]
                started = ps.start_run(
                    steps, action="Génération synthétique",
                    params={"campaigns": int(n_campaigns)},
                )
                if started:
                    st.rerun()
                else:
                    st.warning("Une exécution vient de démarrer ailleurs — celle-ci n'a pas été lancée.")

st.divider()
st.subheader("Historique des exécutions")
runs = ps.read_runs(limit=20)
if not runs:
    st.caption("Aucune exécution déclenchée depuis cette page pour l'instant.")
else:
    table = pd.DataFrame([
        {
            "date": (r.get("started_at") or "")[:16].replace("T", " "),
            "action": r.get("action", ""),
            "durée (s)": r.get("duration_s", 0),
            "résultat": "OK" if r.get("ok") else "ÉCHEC",
            "détails": " · ".join(r.get("failures") or []),
        }
        for r in runs
    ])
    st.dataframe(
        table, hide_index=True, width="stretch",
        column_config={
            "durée (s)": st.column_config.NumberColumn(format="%.1f"),
        },
    )
