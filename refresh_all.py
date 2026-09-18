#!/usr/bin/env python3
"""Weekly refresh: re-extract recent data, rebuild the warehouse, republish.

    python refresh_all.py
    python refresh_all.py --months-back 2 --skip-images

Designed to be driven by a scheduler (Windows Task Scheduler, cron). Each step
runs as a subprocess so one failure is reported without taking the rest down,
and the exit code reflects whether anything failed — a scheduler can then
surface the problem instead of failing silently.

Only the last months are re-extracted, not the whole history: everything older
is immutable and already on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Shared with app/lib/pipeline_state.py by *file path convention only* — this
# script must keep working standalone (Task Scheduler, cron) whether or not
# the app/ directory even exists, so it reads and writes the lock file
# directly rather than importing the app's module. Without this, a scheduled
# run and a click on the app's "Lancer" button have no way to see each other
# and can end up writing to the same warehouse file at the same time.
LOCK_PATH = ROOT / "app_state" / "run.lock"

# The sub-scripts load .env through meta_backfill.config, but this orchestrator
# reads PORTFOLIO_SHEET_ID itself and must load it too — otherwise the publish
# step is silently skipped.
try:
    from dotenv import load_dotenv

    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
except ImportError:
    pass


def run_step(
    label: str,
    command: list[str],
    *,
    env: dict | None = None,
    log_file=None,
) -> tuple[bool, str]:
    """Run one CLI step as a subprocess.

    ``env`` overrides the subprocess environment (e.g. pointing
    ``BACKFILL_DATA_DIR`` at a different data directory) without touching this
    process's own environment; ``None`` inherits it unchanged, the existing
    behaviour. ``log_file``, if given, also receives the full banner and
    output — not just the truncated tail printed to the console — so a caller
    building a persistent log (the app's Pipeline page) gets the complete
    record. Both are optional and keyword-only so every existing call site
    keeps working unmodified.
    """
    started = time.monotonic()
    banner = f"\n{'─' * 62}\n▶ {label}\n{'─' * 62}"
    print(banner, flush=True)
    if log_file:
        print(banner, file=log_file, flush=True)

    try:
        proc = subprocess.run(
            [sys.executable, *command], cwd=ROOT, text=True,
            capture_output=True, encoding="utf-8", errors="replace",
            env=env,
        )
    except OSError as exc:
        message = f"lancement impossible : {exc}"
        if log_file:
            print(message, file=log_file, flush=True)
        return False, message

    tail = "\n".join((proc.stdout or "").strip().splitlines()[-12:])
    if tail:
        print(tail, flush=True)
    if log_file and proc.stdout and proc.stdout.strip():
        print(proc.stdout.strip(), file=log_file, flush=True)
    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        err = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        print(f"  ÉCHEC ({elapsed:.0f}s)\n{err}", file=sys.stderr, flush=True)
        if log_file:
            print(f"  ÉCHEC ({elapsed:.0f}s)\n{(proc.stderr or '').strip()}",
                  file=log_file, flush=True)
        return False, err[:300] or f"code {proc.returncode}"

    print(f"  OK ({elapsed:.0f}s)", flush=True)
    if log_file:
        print(f"  OK ({elapsed:.0f}s)", file=log_file, flush=True)
    return True, ""


def build_steps(
    months_back: int, sheet_id: str, credentials: str, skip_images: bool
) -> list[tuple[str, list[str]]]:
    """The ordered (label, command) pairs ``main()`` runs.

    Pulled out on its own so the app's Pipeline page can call this exact
    function instead of maintaining its own copy of the step list — the two
    can never silently diverge.
    """
    steps: list[tuple[str, list[str]]] = [
        ("Extraction des données récentes",
         ["run_backfill.py", "run", "--all", "--refresh", str(months_back),
          "--months", str(max(months_back, 2))]),
        ("Créatives",
         ["run_backfill.py", "creatives"] + (["--no-images"] if skip_images else [])),
        ("Reconstruction de l'entrepôt", ["build_warehouse.py"]),
    ]
    if sheet_id:
        steps.append(("Publication dans le classeur",
                      ["publish_to_sheets.py", "--sheet-id", sheet_id,
                       "--credentials", credentials]))
    return steps


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--months-back", type=int, default=1,
                   help="nombre de mois récents à réextraire (défaut %(default)s)")
    p.add_argument("--sheet-id", default=os.getenv("PORTFOLIO_SHEET_ID", ""),
                   help="classeur à mettre à jour ; sinon PORTFOLIO_SHEET_ID du .env")
    p.add_argument("--credentials", default="../google_service_account.json")
    p.add_argument("--skip-images", action="store_true",
                   help="ne pas retélécharger les vignettes (gain de ~2 min)")
    args = p.parse_args()

    # Refuse to start if the app (app/pages/1_Pipeline.py) already holds the
    # lock, and take it ourselves for the duration of this run so the app's
    # "Lancer"/"Régénérer" buttons see a scheduled run as in-progress too —
    # without this, a Task Scheduler run and a click in the app could both
    # end up writing to data/ at the same time.
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOCK_PATH, "x", encoding="utf-8") as fh:
            json.dump(
                {"action": "Rafraîchissement planifié (refresh.bat)",
                 "params": {"months_back": args.months_back, "skip_images": args.skip_images},
                 "started_at": datetime.now(timezone.utc).isoformat()},
                fh, indent=2, ensure_ascii=False,
            )
    except FileExistsError:
        print("Un rafraîchissement est déjà en cours (app locale ou autre exécution planifiée) "
              "— annulation.", file=sys.stderr)
        return 1

    print(f"Rafraîchissement — {datetime.now():%Y-%m-%d %H:%M}")

    try:
        steps = build_steps(args.months_back, args.sheet_id, args.credentials, args.skip_images)

        failures: list[str] = []
        for label, command in steps:
            ok, message = run_step(label, command)
            if not ok:
                failures.append(f"{label} : {message}")

        print(f"\n{'═' * 62}")
        if failures:
            print(f"TERMINÉ AVEC {len(failures)} ÉCHEC(S)")
            for f in failures:
                print(f"  · {f}")
            return 1

        print("TERMINÉ — toutes les étapes ont réussi")
        if not args.sheet_id:
            print("  (publication ignorée : aucun --sheet-id ni PORTFOLIO_SHEET_ID)")
        return 0
    finally:
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
