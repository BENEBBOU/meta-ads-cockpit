#!/usr/bin/env python3
"""Generate a synthetic dataset so the project runs without Meta credentials.

The real extraction needs a token, an ad account and several hours. This
produces a fabricated dataset with the same schema and roughly the same shape,
so anyone can clone the repository and exercise the full chain:

    python make_sample_data.py
    python build_warehouse.py
    python run_segment_analysis.py
    python run_early_warning.py
    python run_creative_analysis.py

The numbers are invented. Mild structure is deliberately embedded — some
placements convert better, video earns clicks but converts worse — so the
analyses have something to find and their output is worth looking at. Any
"finding" produced from this data describes the generator, not advertising.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw

from meta_backfill.transform import ACTION_METRICS, to_dataframe

SEED = 7
CITIES = ["Casablanca", "Rabat", "Marrakech", "Tanger", "Agadir", "Tetouan", "Saidia"]
ARTISTS = [
    "Nour Bennani", "Yassine Cherkaoui", "Salma Idrissi", "Karim Ouazzani",
    "Hind Alaoui", "Mehdi Tazi", "Lina Berrada", "Omar Fassi",
]
FORMATS = ["IG Reel", "IG Post", "FB Post"]

PLACEMENTS = [
    # (platform, position, relative conversion efficiency)
    ("instagram", "feed", 1.35),
    ("facebook", "feed", 1.25),
    ("instagram", "instagram_stories", 1.20),
    ("instagram", "instagram_reels", 0.98),
    ("facebook", "facebook_reels", 0.65),
    ("audience_network", "an_classic", 0.10),
]
AGES = ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
GENDERS = ["female", "male"]
REGIONS = ["Grand Casablanca", "Tangier-Tetouan", "Souss-Massa-Draa",
           "Rabat-Sale-Zemmour-Zaer", "Marrakesh-Tensift-El Haouz", "Oriental"]

COPY_TEMPLATES = [
    "{emoji} {artist} en concert a {city} ! {emoji}\n\nRendez-vous le {day} {month} "
    "a {city}. Billets a partir de {price} DH.\n\nReservez vite, places limitees.",
    "{artist} — {city}\n\nUne soiree exceptionnelle le {day} {month}. "
    "Places disponibles des maintenant.",
    "{emoji} DERNIERES PLACES {emoji}\n\n{artist} a {city}, le {day} {month}. "
    "Ne ratez pas ca !\n\nA partir de {price} DH.",
    "Envie de sortir a {city} ?\n\n{artist} vous attend le {day} {month}. "
    "Billetterie ouverte.",
]
MONTHS = ["janvier", "fevrier", "mars", "avril", "mai", "juin",
          "juillet", "aout", "septembre", "octobre", "novembre", "decembre"]
EMOJIS = ["\U0001F525", "\U0001F3A4", "✨", "\U0001F3B6", "\U0001F389"]


def build_ads(rng: np.random.Generator, n_campaigns: int) -> pd.DataFrame:
    """Campaign / ad set / ad hierarchy with per-ad latent quality."""
    rows = []
    for c in range(n_campaigns):
        city = CITIES[rng.integers(len(CITIES))]
        artist = ARTISTS[rng.integers(len(ARTISTS))]
        event_id = 6000 + int(rng.integers(400))
        campaign_id = f"c{100000 + c}"
        campaign_name = f"{c + 1} - #{event_id} - {artist} - {city} - ABO"
        # Campaign-level quality: some events simply sell better. This is the
        # confound the within-campaign controls in the analyses must remove.
        campaign_quality = float(rng.lognormal(0.0, 0.45))

        for a in range(int(rng.integers(2, 6))):
            adset_id = f"s{200000 + c * 10 + a}"
            fmt = FORMATS[rng.integers(len(FORMATS))]
            is_video = fmt.endswith("Reel")
            rows.append({
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "adset_id": adset_id,
                "adset_name": f"{c + 1}.{a + 1} - #{event_id} - {artist} - {city}",
                "ad_id": f"a{300000 + c * 10 + a}",
                "ad_name": f"{c + 1}.{a + 1}.1 - {fmt}",
                "city": city,
                "artist": artist,
                "is_video": is_video,
                "campaign_quality": campaign_quality,
                "ad_quality": float(rng.lognormal(0.0, 0.30)),
                "daily_budget": float(rng.choice([15, 20, 25, 30, 50])),
                "lifetime": int(rng.integers(1, 35)),
            })
    return pd.DataFrame(rows)


def daily_rows(ads: pd.DataFrame, rng: np.random.Generator, start: dt.date) -> list[dict]:
    rows = []
    for _, ad in ads.iterrows():
        offset = int(rng.integers(0, 40))
        for day in range(ad.lifetime):
            date = start + dt.timedelta(days=offset + day)
            spend = max(1.0, float(rng.normal(ad.daily_budget, ad.daily_budget * 0.25)))
            cpm = float(rng.uniform(0.4, 0.9))
            impressions = int(spend / cpm * 1000)

            # Video earns attention, converts worse — the inversion the
            # creative module is meant to surface.
            ctr = float(rng.normal(2.4 if ad.is_video else 1.7, 0.35))
            ctr = max(0.2, ctr)
            clicks = int(impressions * ctr / 100)

            efficiency = ad.campaign_quality * ad.ad_quality * (0.75 if ad.is_video else 1.15)
            checkouts = int(rng.poisson(max(0.0, spend * 0.20 * efficiency)))
            carts = checkouts * int(rng.integers(3, 7))

            rows.append({
                "campaign_id": ad.campaign_id, "campaign_name": ad.campaign_name,
                "adset_id": ad.adset_id, "adset_name": ad.adset_name,
                "ad_id": ad.ad_id, "ad_name": ad.ad_name,
                "date_start": date.isoformat(),
                "spend": round(spend, 2), "impressions": impressions, "clicks": clicks,
                "reach": int(impressions / 1.25), "frequency": round(float(rng.uniform(1.1, 1.4)), 3),
                "cpm": round(cpm * 1000 / 1000, 4), "cpc": round(spend / max(clicks, 1), 4),
                "ctr": round(ctr, 4),
                "actions": [
                    {"action_type": "link_click", "value": str(clicks)},
                    {"action_type": "landing_page_view", "value": str(int(clicks * 0.72))},
                    {"action_type": "add_to_cart", "value": str(carts)},
                    {"action_type": "initiate_checkout", "value": str(checkouts)},
                    {"action_type": "purchase", "value": "0"},
                ],
                "action_values": [
                    {"action_type": "initiate_checkout", "value": str(round(checkouts * 82.0, 2))},
                ],
            })
    return rows


def segment_rows(base: list[dict], rng: np.random.Generator, kind: str) -> list[dict]:
    """Split each daily row across the levels of one breakdown."""
    out = []
    for row in base:
        if kind == "placement":
            levels = [(p, {"publisher_platform": p[0], "platform_position": p[1],
                           "impression_device": "iphone" if rng.random() < 0.5 else "android_smartphone"})
                      for p in PLACEMENTS]
            weights = np.array([0.30, 0.22, 0.18, 0.16, 0.10, 0.04])
            efficiencies = np.array([p[2] for p in PLACEMENTS])
        elif kind == "age_gender":
            levels = [((a, g), {"age": a, "gender": g}) for a in AGES for g in GENDERS]
            weights = np.array([0.14, 0.10, 0.16, 0.13, 0.12, 0.10,
                                0.07, 0.06, 0.05, 0.04, 0.02, 0.01])
            efficiencies = np.array([1.0, 0.75, 1.40, 0.95, 1.35, 0.95,
                                     1.05, 0.80, 0.70, 0.60, 0.55, 0.50])
        else:  # region
            levels = [(r, {"region": r}) for r in REGIONS]
            weights = np.array([0.31, 0.25, 0.14, 0.12, 0.11, 0.07])
            efficiencies = np.ones(len(REGIONS))

        weights = weights / weights.sum()
        checkouts_total = next(
            (int(a["value"]) for a in row["actions"] if a["action_type"] == "initiate_checkout"), 0
        )
        share = weights * efficiencies
        share = share / share.sum()

        for i, (_, cols) in enumerate(levels):
            spend = row["spend"] * weights[i]
            if spend < 0.01:
                continue
            impressions = int(row["impressions"] * weights[i])
            clicks = int(row["clicks"] * weights[i])
            checkouts = int(round(checkouts_total * share[i]))
            piece = {
                "campaign_id": row["campaign_id"], "campaign_name": row["campaign_name"],
                "adset_id": row["adset_id"], "adset_name": row["adset_name"],
                "ad_id": row["ad_id"], "ad_name": row["ad_name"],
                "date_start": row["date_start"],
                "spend": round(spend, 4), "impressions": impressions, "clicks": clicks,
                "cpm": row["cpm"], "cpc": row["cpc"], "ctr": row["ctr"],
                "actions": [
                    {"action_type": "link_click", "value": str(clicks)},
                    {"action_type": "add_to_cart", "value": str(checkouts * 4)},
                    {"action_type": "initiate_checkout", "value": str(checkouts)},
                ],
                "action_values": [],
                **cols,
            }
            # Region breakdowns come back from Meta without pixel conversions;
            # the sample data reproduces that so the guard rail is exercised.
            if kind == "region":
                piece["actions"] = [{"action_type": "link_click", "value": str(clicks)}]
            out.append(piece)
    return out


def write_pass(rows: list[dict], name: str, breakdowns: tuple[str, ...], raw_dir: Path) -> int:
    frame = to_dataframe(rows, pass_name=name, breakdowns=breakdowns)
    if frame.empty:
        return 0
    frame["month_label"] = frame["date"].dt.strftime("%Y-%m")
    total = 0
    for label, chunk in frame.groupby("month_label"):
        out = raw_dir / f"pass={name}" / f"month={label}"
        out.mkdir(parents=True, exist_ok=True)
        chunk.drop(columns=["month_label"]).to_parquet(out / "part.parquet", index=False)
        total += len(chunk)
    return total


def write_creatives(ads: pd.DataFrame, rng: np.random.Generator, data_dir: Path) -> None:
    rows, thumbs = [], data_dir / "thumbnails"
    thumbs.mkdir(parents=True, exist_ok=True)
    for _, ad in ads.iterrows():
        template = COPY_TEMPLATES[rng.integers(len(COPY_TEMPLATES))]
        body = template.format(
            artist=ad.artist, city=ad.city, emoji=EMOJIS[rng.integers(len(EMOJIS))],
            day=int(rng.integers(1, 29)), month=MONTHS[rng.integers(len(MONTHS))],
            price=int(rng.choice([150, 200, 250, 300, 400])),
        )
        rows.append({
            "ad_id": ad.ad_id, "ad_name": ad.ad_name, "ad_status": "ACTIVE",
            "created_time": None, "creative_id": f"cr{ad.ad_id}", "creative_name": ad.ad_name,
            "title": None, "body": body,
            "object_type": "VIDEO" if ad.is_video else "SHARE",
            "cta": "BUY_TICKETS", "video_id": None, "image_url": None,
            "thumbnail_url": None, "story_id": None,
        })

        # Vertical for video, square otherwise — matching the aspect-ratio
        # signal the creative analysis picks up on real data.
        size = (360, 640) if ad.is_video else (480, 480)
        base = tuple(int(v) for v in rng.integers(30, 220, size=3))
        image = Image.new("RGB", size, base)
        draw = ImageDraw.Draw(image)
        for _ in range(int(rng.integers(3, 12))):
            x0, y0 = rng.integers(0, size[0]), rng.integers(0, size[1])
            draw.rectangle(
                [x0, y0, x0 + int(rng.integers(20, 160)), y0 + int(rng.integers(20, 160))],
                fill=tuple(int(v) for v in rng.integers(0, 255, size=3)),
            )
        image.save(thumbs / f"{ad.ad_id}.jpg", quality=85)

    pd.DataFrame(rows).to_parquet(data_dir / "creatives.parquet", index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaigns", type=int, default=70)
    parser.add_argument("--out", default="data",
                        help="output directory (default: %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing data directory")
    args = parser.parse_args()

    data_dir = Path(args.out)

    if data_dir.exists() and any(data_dir.iterdir()) and not args.force:
        print(f"{data_dir}/ already contains data. Use --force to replace it.", file=sys.stderr)
        return 1
    if data_dir.exists() and args.force:
        shutil.rmtree(data_dir)

    rng = np.random.default_rng(SEED)
    raw_dir = data_dir / "raw"
    start = dt.date.today().replace(day=1) - dt.timedelta(days=60)

    ads = build_ads(rng, args.campaigns)
    base = daily_rows(ads, rng, start)

    counts = {"base": write_pass(base, "base", (), raw_dir)}
    for kind, name, cols in [
        ("age_gender", "age_gender", ("age", "gender")),
        ("placement", "placement", ("publisher_platform", "platform_position", "impression_device")),
        ("region", "region", ("region",)),
    ]:
        counts[name] = write_pass(segment_rows(base, rng, kind), name, cols, raw_dir)

    write_creatives(ads, rng, data_dir)

    # A checkpoint file keeps `run_backfill.py status` meaningful on sample data.
    (data_dir / "checkpoints.json").write_text(
        json.dumps({f"{k}:sample": {"rows": v} for k, v in counts.items()}, indent=2),
        encoding="utf-8",
    )

    print("Synthetic dataset written to data/\n")
    for name, n in counts.items():
        print(f"  {name:<12} {n:>8,} rows")
    print(f"  {'creatives':<12} {len(ads):>8,} ads + thumbnails")
    print("\nNext: python build_warehouse.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
