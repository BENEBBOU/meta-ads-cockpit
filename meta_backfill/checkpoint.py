"""Crash-safe progress tracking so an interrupted backfill can resume.

A three-year extraction takes hours and *will* be interrupted — a rate limit,
an expired token, a closed laptop. Every completed (pass, month) unit is
recorded immediately, so restarting picks up exactly where it stopped instead
of re-downloading everything.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Checkpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read checkpoint file (%s) — starting fresh", exc)
            return
        if isinstance(payload, dict):
            self._entries = {k: v for k, v in payload.items() if isinstance(v, dict)}
        log.info("checkpoint loaded: %s unit(s) already complete", len(self._entries))

    def _save(self) -> None:
        """Write atomically so an interrupted save cannot corrupt the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(self._entries, fh, indent=2, sort_keys=True)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def key(pass_name: str, month: str) -> str:
        return f"{pass_name}:{month}"

    def done(self, pass_name: str, month: str) -> bool:
        return self.key(pass_name, month) in self._entries

    def mark(self, pass_name: str, month: str, **meta: Any) -> None:
        self._entries[self.key(pass_name, month)] = {
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **meta,
        }
        self._save()

    def forget(self, pass_name: str, month: str) -> None:
        if self._entries.pop(self.key(pass_name, month), None) is not None:
            self._save()

    def entries(self) -> dict[str, dict]:
        """A copy of the raw ``"pass:month" -> metadata`` records.

        ``summary()`` collapses this to a per-pass row total; callers that
        need per-month detail (a completion matrix, say) use this instead of
        reaching into ``_entries`` directly.
        """
        return dict(self._entries)

    def summary(self) -> dict[str, int]:
        """Rows extracted per pass, according to the recorded units."""
        totals: dict[str, int] = {}
        for key, meta in self._entries.items():
            pass_name = key.split(":", 1)[0]
            totals[pass_name] = totals.get(pass_name, 0) + int(meta.get("rows", 0))
        return totals
