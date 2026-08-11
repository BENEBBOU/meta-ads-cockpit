"""A small Meta Marketing API client built around *asynchronous* insight jobs.

Bulk historical extraction must not be done with synchronous ``GET /insights``
calls: Meta throttles them hard and a multi-year pull dies within minutes. The
documented approach for bulk reads, and the one implemented here, is:

    1. ``POST /act_<id>/insights``        -> ``report_run_id``
    2. ``GET  /<report_run_id>``          -> poll until ``Job Completed``
    3. ``GET  /<report_run_id>/insights`` -> paginated result rows

On top of that the client is defensive about rate limits. Meta reports the
remaining budget on every response through usage headers; we read them and
back off *before* being throttled rather than reacting to a 429 after the fact.
This matters because the same ad account also serves live production jobs.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import requests

from .config import Settings

log = logging.getLogger(__name__)

# Error codes Meta uses for throttling of one flavour or another.
RATE_LIMIT_CODES = {
    4,  # Application request limit reached
    17,  # User request limit reached
    32,  # Page-level throttling
    613,  # Calls to this API have exceeded the rate limit
    80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014,
}
TRANSIENT_CODES = {1, 2}  # Unknown / temporary platform errors

USAGE_HEADERS = (
    "x-business-use-case-usage",
    "x-ad-account-usage",
    "x-app-usage",
)


class MetaApiError(RuntimeError):
    """A non-retryable error returned by the Graph API."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class ReportFailed(MetaApiError):
    """An async insights job finished in a non-successful state."""


def _iter_usage_buckets(payload: Any) -> Iterator[dict]:
    """Yield the usage dictionaries hidden in Meta's various header shapes."""
    if not isinstance(payload, dict):
        return
    nested = False
    for value in payload.values():
        if isinstance(value, list):
            nested = True
            for item in value:
                if isinstance(item, dict):
                    yield item
        elif isinstance(value, dict):
            nested = True
            yield value
    if not nested:
        # x-ad-account-usage is a flat {"acc_id_util_pct": 12.3} object.
        yield payload


def usage_pct(headers: Any) -> float:
    """Worst-case percentage of the rate-limit budget currently consumed."""
    worst = 0.0
    for header in USAGE_HEADERS:
        raw = headers.get(header)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for bucket in _iter_usage_buckets(payload):
            for key in ("call_count", "total_cputime", "total_time", "acc_id_util_pct"):
                value = bucket.get(key)
                if isinstance(value, (int, float)):
                    worst = max(worst, float(value))
    return worst


def _parse_error(response: requests.Response) -> tuple[int | None, int | None, str]:
    try:
        error = (response.json() or {}).get("error", {})
    except ValueError:
        return None, None, f"HTTP {response.status_code}: {response.text[:200]}"
    return (
        error.get("code"),
        error.get("error_subcode"),
        error.get("message") or f"HTTP {response.status_code}",
    )


class InsightsClient:
    """Async-job based reader for the ad-account insights edge."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
    ) -> dict:
        params = dict(params or {})
        data = dict(data or {})
        # The token travels in the body for POST so it stays out of the URL.
        (data if method == "POST" else params)["access_token"] = self.settings.access_token

        delay = 5.0
        last_message = "no attempt made"

        for attempt in range(1, self.settings.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params or None,
                    data=data or None,
                    timeout=self.settings.request_timeout,
                )
            except requests.RequestException as exc:
                last_message = f"network error: {exc}"
                if attempt == self.settings.max_retries:
                    break
                log.warning("%s — retrying in %.0fs (%s/%s)",
                            last_message, delay, attempt, self.settings.max_retries)
                time.sleep(delay)
                delay = min(delay * 2, 900)
                continue

            if response.ok:
                self._respect_usage(response.headers)
                try:
                    return response.json()
                except ValueError as exc:
                    raise MetaApiError(f"malformed JSON response: {exc}") from exc

            code, subcode, message = _parse_error(response)
            retryable = (
                response.status_code == 429
                or code in RATE_LIMIT_CODES
                or code in TRANSIENT_CODES
                or 500 <= response.status_code < 600
            )
            if not retryable:
                raise MetaApiError(message, code=code, subcode=subcode)

            last_message = f"{message} (code {code})"
            if attempt == self.settings.max_retries:
                break
            wait = min(delay * 4, 900) if code in RATE_LIMIT_CODES else delay
            log.warning("%s — backing off %.0fs (%s/%s)",
                        last_message, wait, attempt, self.settings.max_retries)
            time.sleep(wait)
            delay = min(delay * 2, 900)

        raise MetaApiError(f"giving up after {self.settings.max_retries} attempts: {last_message}")

    def _respect_usage(self, headers: Any) -> None:
        """Pause proactively as the rate-limit budget fills up."""
        pct = usage_pct(headers)
        if pct >= 95:
            nap = 600
        elif pct >= 85:
            nap = 180
        elif pct >= 70:
            nap = 45
        elif pct >= 50:
            nap = 8
        else:
            return
        log.info("rate-limit budget at %.0f%% — cooling down for %ss", pct, nap)
        time.sleep(nap)

    # -- public API --------------------------------------------------------

    def get(self, url: str, params: dict | None = None) -> dict:
        """Plain GET against any Graph endpoint.

        Shares the retry, back-off and rate-limit accounting of the insights
        calls, so non-insights reads cannot quietly blow the same quota.
        """
        return self._request("GET", url, params=params)

    def verify_access(self) -> dict:
        """Fetch basic account metadata; raises if the token cannot read it."""
        url = f"{self.settings.base_url}/{self.settings.account_id}"
        return self._request(
            "GET",
            url,
            params={"fields": "name,currency,timezone_name,account_status"},
        )

    def create_report(self, params: dict) -> str:
        url = f"{self.settings.base_url}/{self.settings.account_id}/insights"
        payload = self._request("POST", url, data=params)
        run_id = payload.get("report_run_id")
        if not run_id:
            raise MetaApiError(f"no report_run_id returned: {payload}")
        return str(run_id)

    def wait_for_report(self, run_id: str) -> None:
        url = f"{self.settings.base_url}/{run_id}"
        deadline = time.monotonic() + self.settings.poll_timeout
        while True:
            status = self._request(
                "GET", url,
                params={"fields": "async_status,async_percent_completion"},
            )
            state = status.get("async_status", "")
            pct = status.get("async_percent_completion", 0)

            if state == "Job Completed":
                return
            if state in {"Job Failed", "Job Skipped"}:
                raise ReportFailed(f"report {run_id} finished as {state!r}")
            if time.monotonic() > deadline:
                raise ReportFailed(
                    f"report {run_id} timed out after {self.settings.poll_timeout}s "
                    f"(last status {state!r} at {pct}%)"
                )
            log.debug("report %s: %s (%s%%)", run_id, state, pct)
            time.sleep(self.settings.poll_interval)

    def fetch_rows(self, run_id: str) -> Iterator[dict]:
        url = f"{self.settings.base_url}/{run_id}/insights"
        params: dict = {"limit": self.settings.page_size}
        while True:
            payload = self._request("GET", url, params=params)
            rows = payload.get("data") or []
            yield from rows

            paging = payload.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                return
            params = {"limit": self.settings.page_size, "after": after}

    def run_insights(self, params: dict) -> list[dict]:
        """Create a report, wait for it, and return every row it produced."""
        run_id = self.create_report(params)
        log.debug("report %s created", run_id)
        self.wait_for_report(run_id)
        return list(self.fetch_rows(run_id))
