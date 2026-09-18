"""Lock, run history and log file for the Pipeline page's background runs.

Kept on disk, not in ``st.session_state``: a real backfill can run for tens
of minutes, and a closed browser tab or an app restart must not lose track of
whether something is still running, or leave the "Lancer" button permanently
disabled with no way to tell why.

Nothing in this module calls any ``st.*`` function. It is written to be safe
to call from a background thread — the Pipeline page's script (the only place
allowed to touch Streamlit) only ever *reads* what this module writes.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = ROOT / "app_state"
LOCK_PATH = STATE_DIR / "run.lock"
RUNS_PATH = STATE_DIR / "runs.json"
LOG_PATH = STATE_DIR / "last_run.log"

_MAX_HISTORY = 100


def _atomic_write_json(path: Path, data) -> None:
    """Write-then-rename, so a reader never observes a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def is_locked() -> bool:
    return LOCK_PATH.exists()


def lock_info() -> dict | None:
    if not LOCK_PATH.exists():
        return None
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def try_acquire_lock(meta: dict) -> bool:
    """Atomically create the lock file; ``False`` if one already exists.

    Uses exclusive creation (``O_CREAT | O_EXCL`` under the hood, via the
    ``"x"`` file mode) rather than write-then-rename: two callers racing to
    acquire at the same instant cannot both succeed, unlike the previous
    unconditional overwrite, which let a second click silently start a
    second concurrent pipeline run on top of the first.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOCK_PATH, "x", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, ensure_ascii=False)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def force_unlock() -> None:
    """Manual escape hatch: clear a lock left behind by a killed process.

    Nothing here can tell a genuinely long-running extraction apart from an
    orphaned one — that judgement is left to the person clicking the button,
    with the lock's age and last log line shown so they can make it. Does
    NOT attempt to kill any subprocess the dead thread may have started;
    those either finish on their own or were already gone with the process
    that spawned them.
    """
    release_lock()


def read_runs(limit: int = 20) -> list[dict]:
    """Most recent runs first."""
    if not RUNS_PATH.exists():
        return []
    try:
        data = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return list(reversed(data[-limit:]))


def append_run_record(record: dict) -> None:
    runs = []
    if RUNS_PATH.exists():
        try:
            existing = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                runs = existing
        except (OSError, ValueError):
            pass
    runs.append(record)
    _atomic_write_json(RUNS_PATH, runs[-_MAX_HISTORY:])


def read_log(max_chars: int = 20000) -> str:
    if not LOG_PATH.exists():
        return ""
    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def start_run(
    steps: list[tuple[str, list[str], dict | None]], *, action: str, params: dict
) -> bool:
    """Try to acquire the lock, then hand the work to a background thread.

    Returns ``False`` without starting anything if the lock is already held
    — the caller (the page) is expected to show that as a warning rather
    than silently doing nothing, since it means a second click or a second
    browser tab raced an in-progress run.

    The lock is acquired *synchronously*, on the caller's thread, and
    atomically (:func:`try_acquire_lock`), before anything is started. A
    caller that merely checked ``is_locked()`` first and then wrote the lock
    separately would leave a window between the two where a second caller
    could pass the same check — this collapses that into one step.
    """
    started = datetime.now(timezone.utc)
    got_lock = try_acquire_lock(
        {"action": action, "params": params, "started_at": started.isoformat()}
    )
    if not got_lock:
        return False

    thread = threading.Thread(
        target=_run_locked, args=(steps,),
        kwargs={"action": action, "params": params, "started": started},
        daemon=True,
    )
    thread.start()
    return True


def _run_locked(
    steps: list[tuple[str, list[str], dict | None]], *, action: str, params: dict, started: datetime
) -> None:
    """The entire body of the background thread. Never call st.* from here.

    Each step is ``(label, command, env)`` — ``env`` overrides the subprocess
    environment (e.g. pointing ``BACKFILL_DATA_DIR`` at the synthetic data
    directory for a step that has no CLI flag of its own for it) and is
    ``None`` for steps that should inherit this process's environment as-is.

    ``refresh_all.run_step`` already does everything a step needs — capture
    the subprocess, print a console tail, return (ok, message). This only
    adds a full-output log file and a history record. The lock is already
    held by the time this runs (see :func:`start_run`); a ``try/finally``
    here guarantees it is still released even if a step raises something
    ``run_step`` itself does not catch — an unhandled exception must never
    leave the "Lancer" button stuck disabled forever.
    """
    import refresh_all

    ok_all = True
    failures: list[str] = []
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as logf:
            print(f"{action} — {started:%Y-%m-%d %H:%M} UTC", file=logf, flush=True)
            for label, command, env in steps:
                ok, message = refresh_all.run_step(label, command, env=env, log_file=logf)
                if not ok:
                    ok_all = False
                    failures.append(f"{label} : {message}")
    except BaseException as exc:  # noqa: BLE001 — the lock must not get stuck
        ok_all = False
        failures.append(f"erreur inattendue : {exc}")
    finally:
        # release_lock() must run even if writing the history record itself
        # fails (disk full, permissions...) — a raise from *inside* a
        # finally block aborts whatever follows it in that same block, so
        # the two are deliberately separated into their own try/except
        # rather than left as two bare statements one after the other.
        finished = datetime.now(timezone.utc)
        try:
            append_run_record({
                "action": action,
                "params": params,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "duration_s": round((finished - started).total_seconds(), 1),
                "ok": ok_all,
                "failures": failures,
            })
        except BaseException:  # noqa: BLE001 — see comment above
            pass
        release_lock()
