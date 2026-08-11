"""Runtime configuration and the definition of each backfill pass.

Configuration is read from the environment (``.env`` is loaded automatically).
Backfill-specific variables are preferred so that dropping this project next to
an existing script does not force you to repoint that script's credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load this project's .env first, then fall back to one in the parent folder.
# `override=False` means the local file always wins, so a shared token can live
# in a single place upstream while this project overrides only the account it
# targets — no copying secrets between files.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _candidate in (_PROJECT_ROOT / ".env", _PROJECT_ROOT.parent / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate, override=False)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _first_env(*names: str, default: str = "") -> str:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _normalise_account(value: str) -> str:
    value = value.strip()
    return value if value.startswith("act_") else f"act_{value}"


@dataclass(frozen=True)
class Settings:
    """Everything the extractor needs to talk to Meta and write results."""

    access_token: str
    account_id: str
    api_version: str
    data_dir: Path

    # Networking / pacing. The defaults are deliberately conservative: a
    # backfill competes for the same rate-limit budget as the production
    # automations running against this ad account.
    request_timeout: int = 120
    max_retries: int = 6
    poll_interval: int = 10
    poll_timeout: int = 3600
    page_size: int = 500

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.json"

    @property
    def warehouse_path(self) -> Path:
        return self.data_dir / "meta_ads.duckdb"


def load_settings() -> Settings:
    token = _first_env("META_BACKFILL_TOKEN", "META_ACCESS_TOKEN")
    if not token:
        raise ConfigError(
            "No access token found. Set META_BACKFILL_TOKEN in .env "
            "(copy .env.example to get started)."
        )

    account = _first_env("META_BACKFILL_ACCOUNT_ID", "META_AD_ACCOUNT_ID")
    if not account:
        raise ConfigError(
            "No ad account found. Set META_BACKFILL_ACCOUNT_ID in .env."
        )

    return Settings(
        access_token=token,
        account_id=_normalise_account(account),
        api_version=_first_env("META_API_VERSION", default="v19.0"),
        data_dir=Path(_first_env("BACKFILL_DATA_DIR", default="data")),
    )


# --------------------------------------------------------------------------
# Field sets
# --------------------------------------------------------------------------

ENTITY_FIELDS: tuple[str, ...] = (
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
)

# Full metric set — only valid on the unsegmented pass. Meta rejects reach and
# frequency for several breakdown combinations, and cost_per_action_type
# roughly doubles the payload for something we can recompute in SQL.
METRICS_FULL: tuple[str, ...] = (
    "impressions",
    "clicks",
    "spend",
    "reach",
    "frequency",
    "cpm",
    "cpc",
    "ctr",
    "actions",
    "action_values",
    "cost_per_action_type",
)

METRICS_LIGHT: tuple[str, ...] = (
    "impressions",
    "clicks",
    "spend",
    "cpm",
    "cpc",
    "ctr",
    "actions",
    "action_values",
)


@dataclass(frozen=True)
class BackfillPass:
    """One extraction shape: an entity level plus an optional segmentation."""

    name: str
    level: str
    fields: tuple[str, ...]
    breakdowns: tuple[str, ...] = ()
    description: str = ""

    @property
    def entity_fields(self) -> tuple[str, ...]:
        """Entity columns present in this pass, given its level."""
        if self.level == "campaign":
            return ("campaign_id", "campaign_name")
        if self.level == "adset":
            return ENTITY_FIELDS[:4]
        return ENTITY_FIELDS


PASSES: dict[str, BackfillPass] = {
    "base": BackfillPass(
        name="base",
        level="ad",
        fields=ENTITY_FIELDS + METRICS_FULL,
        description="One row per ad per day. The core dataset — run this first.",
    ),
    "age_gender": BackfillPass(
        name="age_gender",
        level="ad",
        fields=ENTITY_FIELDS + METRICS_LIGHT,
        breakdowns=("age", "gender"),
        description="Ad x day x age bucket x gender.",
    ),
    "placement": BackfillPass(
        name="placement",
        level="ad",
        fields=ENTITY_FIELDS + METRICS_LIGHT,
        breakdowns=("publisher_platform", "platform_position", "impression_device"),
        description="Ad x day x platform x position x device.",
    ),
    "region": BackfillPass(
        name="region",
        level="ad",
        fields=ENTITY_FIELDS + METRICS_LIGHT,
        breakdowns=("region",),
        description="Ad x day x region.",
    ),
    # Hourly is kept at campaign level on purpose: ad x day x hour over three
    # years would be tens of millions of rows for very little extra signal.
    "hourly": BackfillPass(
        name="hourly",
        level="campaign",
        fields=("campaign_id", "campaign_name") + METRICS_LIGHT,
        breakdowns=("hourly_stats_aggregated_by_advertiser_time_zone",),
        description="Campaign x day x hour of day.",
    ),
}

DEFAULT_PASS_ORDER: tuple[str, ...] = (
    "base",
    "age_gender",
    "placement",
    "region",
    "hourly",
)
