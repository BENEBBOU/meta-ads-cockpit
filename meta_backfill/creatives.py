"""Fetch the creative attached to every ad: copy, format, call to action, thumbnail.

Insights tell you how an ad performed. They say nothing about what the ad
*was*. This module retrieves the other half — the text a viewer read and the
image they saw — so that performance can be regressed on the creative itself.

Unlike insights, the ads edge is a plain paginated GET, so no asynchronous job
is involved. Thumbnails are downloaded from the CDN, which does not consume the
Graph API quota, but they are fetched politely all the same.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from .api import InsightsClient
from .config import Settings

log = logging.getLogger(__name__)

CREATIVE_FIELDS = (
    "id,name,status,created_time,"
    "creative{id,name,title,body,image_url,thumbnail_url,object_type,"
    "call_to_action_type,video_id,effective_object_story_id}"
)

THUMBNAIL_TIMEOUT = 30
THUMBNAIL_PAUSE = 0.15


def _first_url(*values: object) -> str | None:
    """First value that is a genuine non-empty URL string.

    Deliberately not ``a or b``: a missing pandas cell is NaN, which is truthy.
    """
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def fetch_creatives(client: InsightsClient, settings: Settings, *, page_size: int = 100) -> pd.DataFrame:
    """One row per ad, with its creative flattened into columns."""
    url = f"{settings.base_url}/{settings.account_id}/ads"
    params: dict = {"fields": CREATIVE_FIELDS, "limit": page_size}
    rows: list[dict] = []

    while True:
        payload = client.get(url, params)
        for ad in payload.get("data") or []:
            creative = ad.get("creative") or {}
            rows.append({
                "ad_id": ad.get("id"),
                "ad_name": ad.get("name"),
                "ad_status": ad.get("status"),
                "created_time": ad.get("created_time"),
                "creative_id": creative.get("id"),
                "creative_name": creative.get("name"),
                "title": creative.get("title"),
                "body": creative.get("body"),
                "object_type": creative.get("object_type"),
                "cta": creative.get("call_to_action_type"),
                "video_id": creative.get("video_id"),
                "image_url": creative.get("image_url"),
                "thumbnail_url": creative.get("thumbnail_url"),
                "story_id": creative.get("effective_object_story_id"),
            })

        paging = payload.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after:
            break
        params = {"fields": CREATIVE_FIELDS, "limit": page_size, "after": after}
        log.info("fetched %s creatives so far", len(rows))

    frame = pd.DataFrame(rows)
    log.info("fetched %s creatives", len(frame))
    return frame


def download_thumbnails(
    frame: pd.DataFrame,
    out_dir: Path,
    *,
    only_ads: set[str] | None = None,
) -> dict[str, Path]:
    """Download each ad's thumbnail. Returns ad_id -> local path.

    Ads outside ``only_ads`` are skipped: there is no point pulling images for
    ads that never delivered and therefore carry no performance signal.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}

    for _, row in frame.iterrows():
        ad_id = row.get("ad_id")
        if only_ads is not None and ad_id not in only_ads:
            continue
        # `a or b` is wrong here: pandas represents a missing cell as NaN, and
        # NaN is truthy, so the fallback would never fire and every video ad
        # (which has no image_url, only a thumbnail) would be skipped.
        url = _first_url(row.get("image_url"), row.get("thumbnail_url"))
        if url is None:
            continue

        target = out_dir / f"{ad_id}.jpg"
        if target.exists():
            saved[ad_id] = target
            continue

        try:
            request = urllib.request.Request(url, headers={"User-Agent": "meta-ads-backfill/0.1"})
            with urllib.request.urlopen(request, timeout=THUMBNAIL_TIMEOUT) as response:
                target.write_bytes(response.read())
            saved[ad_id] = target
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("thumbnail failed for %s: %s", ad_id, exc)
        time.sleep(THUMBNAIL_PAUSE)

    log.info("downloaded %s thumbnail(s) to %s", len(saved), out_dir)
    return saved
