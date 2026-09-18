"""Warehouse connection and data-source selection, shared by every page.

Two concerns live here on purpose, and nowhere else: which DuckDB file is
"active" (real vs. synthetic), and how a connection to it is opened and
cached. Every page goes through :func:`get_connection` — never
``duckdb.connect`` directly — so the source selector always renders and stays
consistent across the whole app.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import streamlit as st

from meta_backfill.config import Settings, load_settings

_LABELS = {"real": "Réelle", "sample": "Synthétique"}


def data_dirs(settings: Settings | None = None) -> dict[str, Path]:
    """The real and synthetic data *directories* — siblings, never nested.

    ``make_sample_data.py`` writes an entire directory (``raw/``,
    ``creatives.parquet``, ``thumbnails/``, ``checkpoints.json``, and later
    ``meta_ads.duckdb`` once built) using the same fixed filenames every time,
    the same ones the real pipeline uses. A single file suffixed
    ``.sample.duckdb`` cannot represent that — ``build_warehouse.py`` always
    writes exactly ``meta_ads.duckdb``, nothing else — so two separate
    directories are the only correct split. This also means creatives and
    thumbnails follow the source selector along with the warehouse, rather
    than living in one fixed place regardless of which source is active.
    """
    settings = settings or load_settings()
    real = settings.data_dir
    sample = real.parent / f"{real.name}_sample"
    return {"real": real, "sample": sample}


def warehouse_paths(settings: Settings | None = None) -> dict[str, Path]:
    """The real and synthetic warehouse *file* paths, one per data directory."""
    return {key: directory / "meta_ads.duckdb" for key, directory in data_dirs(settings).items()}


def active_data_dir() -> Path:
    """The data directory (checkpoints, creatives, thumbnails) for the current source.

    Reads ``st.session_state`` without rendering anything — call this only
    after :func:`get_connection` (or on a page that manages its own source
    display, like Pipeline) has already established which source is active.
    """
    choice = st.session_state.get("db_choice", "real")
    return data_dirs()[choice]


def pick_active_source() -> str:
    """Render the sidebar source selector; return the chosen key.

    Persisted in ``st.session_state`` so the choice survives navigation
    between pages. If a source that exists disappears (e.g. a warehouse file
    is deleted between reruns), the selection falls back silently rather than
    crashing the page.
    """
    paths = warehouse_paths()
    available = [k for k in ("real", "sample") if paths[k].exists()]

    if not available:
        st.sidebar.error(
            "Aucun entrepôt trouvé.\n\n"
            "Lancez `python run_backfill.py run --all` puis "
            "`python build_warehouse.py`, ou `python make_sample_data.py` "
            "pour un jeu de données synthétique."
        )
        st.stop()

    current = st.session_state.get("db_choice")
    if current not in available:
        current = "real" if "real" in available else available[0]

    choice = st.sidebar.radio(
        "Source de données",
        options=available,
        format_func=lambda k: _LABELS[k],
        index=available.index(current),
        key="db_choice_radio",
    )
    st.session_state["db_choice"] = choice

    missing = [k for k in ("real", "sample") if k not in available]
    if missing:
        st.sidebar.caption(
            "Indisponible : " + ", ".join(_LABELS[k] for k in missing)
            + " — voir la page Pipeline."
        )

    return choice


# Every connection this process has opened, so close_all_connections() can
# actually close them — st.cache_resource.clear() only drops Streamlit's
# reference to a cached object, it never calls .close() on it, so a lingering
# open file handle can outlive the cache entry and still hold a read lock.
_OPEN_CONNECTIONS: list[duckdb.DuckDBPyConnection] = []


@st.cache_resource(show_spinner=False)
def open_connection(db_path: str, mtime: float) -> duckdb.DuckDBPyConnection:
    """Open a read-only connection for a *known* path, cached by (path, mtime).

    ``mtime`` is never read in the body — it exists purely as a cache-busting
    key. When ``build_warehouse.py`` rewrites the file, the mtime changes, the
    cache key no longer matches, and Streamlit opens a fresh connection with
    no manual ``.clear()`` needed for the ordinary "I refreshed, show me the
    new numbers" case. The app never opens the file for writing: only
    ``build_warehouse.py``, run as a subprocess, does that.

    Cached page computations call this directly with an explicit
    ``(db_path, mtime)`` pair — never :func:`get_connection`, which also
    renders the sidebar selector and has no business running inside a
    ``@st.cache_data`` function.
    """
    con = duckdb.connect(db_path, read_only=True)
    _OPEN_CONNECTIONS.append(con)
    return con


def close_all_connections() -> None:
    """Close every connection this process has opened, then clear the cache.

    Call this — never bare ``open_connection.clear()`` — before a subprocess
    (``build_warehouse.py``) opens the same warehouse file for writing.
    Clearing the cache alone drops Streamlit's reference but leaves the
    DuckDB file handle open; on Windows that lingering read handle can make
    the writer's open() fail even though nothing in the app appears to be
    using the file anymore.
    """
    while _OPEN_CONNECTIONS:
        con = _OPEN_CONNECTIONS.pop()
        try:
            con.close()
        except Exception:
            pass
    open_connection.clear()


def get_connection() -> duckdb.DuckDBPyConnection:
    """The connection for whichever source is currently selected.

    Also renders the sidebar selector as a side effect, so calling this once
    per page is enough to keep the selector present everywhere. Use this at
    the top of a page's main body; use :func:`open_connection` with an
    explicit path/mtime inside cached computation functions instead.
    """
    choice = pick_active_source()
    path = warehouse_paths()[choice]
    return open_connection(str(path), path.stat().st_mtime)


def require_views(con: duckdb.DuckDBPyConnection, *names: str) -> bool:
    """True if every named view exists; otherwise shows a friendly notice.

    Views can legitimately be missing — a fresh warehouse might only have the
    ``base`` pass extracted yet. Pages call this once, right after
    ``get_connection()``, and stop rendering rather than crash on a missing
    table.
    """
    from analysis.segments import available_views

    existing = available_views(con)
    missing = [n for n in names if n not in existing]
    if missing:
        st.info(
            "Cette page a besoin de : " + ", ".join(f"`{n}`" for n in missing)
            + " — pas encore construite(s) dans cette source. "
              "Lancez l'extraction correspondante puis `python build_warehouse.py`."
        )
        return False
    return True


def source_info() -> dict:
    """Everything the "which data am I looking at" banner needs to show."""
    choice = st.session_state.get("db_choice", "real")
    path = warehouse_paths()[choice]
    exists = path.exists()
    return {
        "kind": choice,
        "label": _LABELS[choice],
        "path": path,
        "exists": exists,
        "size_mb": (path.stat().st_size / 1e6) if exists else None,
        "modified": dt.datetime.fromtimestamp(path.stat().st_mtime) if exists else None,
    }
