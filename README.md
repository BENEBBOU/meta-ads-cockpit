# Meta Ads — extraction, warehouse and statistical diagnosis

An end-to-end analytics platform for a Meta (Facebook/Instagram) advertising
account: a rate-limit-resistant historical extractor, a local analytical
warehouse of ~167k rows, and three analysis modules that answer where the
advertising budget is misspent, and by how much.

Built during an internship for an events-ticketing advertiser. All figures
below come from the real account; the company is not named and no identifier
is committed.

---

## What it found

**A broken conversion pixel.** The funnel behaves normally down to checkout and
then collapses: 9,004 checkouts produced 26 recorded purchases, a 0.29% pass
rate where e-commerce norms sit between 40 and 70%. Meta's optimiser cannot
learn to find buyers from 26 examples, so every campaign on the account is
being optimised on a broken signal. Cheapest, highest-impact fix available.

**An placement burning money.** Audience Network cost \$58.62 per checkout
against \$3.55 on the Instagram feed — a factor of 16, on 7% of the budget.
Turning it off is a checkbox.

**Attention and conversion pull in opposite directions.** Creative traits that
win clicks lose conversions, and the effect survives a within-campaign control:

| Trait | CTR | Checkouts/\$ |
|---|---|---|
| Video | **+0.42** | **−0.20** |
| Vertical format | **+0.41** | **−0.20** |
| High contrast | −0.25 | **+0.21** |

Selecting creatives on click-through rate — the metric Meta's interface
foregrounds — therefore selects *against* conversion.

**Two hypotheses tested and rejected.** Creative fatigue does not occur on this
account (per-ad CTR slope: t = −0.23, p = 0.82; daily frequency *falls* from
1.29 to 1.19 over an ad's life, so no cumulative exposure builds up). And a
day-2 early-warning model, while showing real signal in cross-validation
(AUC 0.73), is beaten by simply ranking on early checkouts-per-dollar
(AUC 0.78) — so the deliverable is a rule, not a model.

Full write-up: [`rapport/rapport.pdf`](rapport/rapport.pdf) (16 pages, French).

---

## Quickstart

No Meta credentials needed — the repository ships a synthetic data generator
with the same schema:

```bash
pip install -r requirements.txt

python make_sample_data.py          # fabricate ~4.5k rows + creatives
python build_warehouse.py           # register DuckDB views
python run_segment_analysis.py      # rank segments, size reallocation
python run_early_warning.py         # day-2 triage, evaluated two ways
python run_creative_analysis.py     # what the creative explains
python explore.py views             # browse the data
```

The synthetic numbers are invented, with mild structure embedded so the tools
have something to find. Any "finding" from that data describes the generator.

### Against a real account

```bash
cp .env.example .env                # fill in META_BACKFILL_TOKEN
python run_backfill.py check        # verify token can read the account
python run_backfill.py run --all    # extract up to 37 months
python run_backfill.py creatives    # ad copy, formats, thumbnails
python build_warehouse.py
```

Use a **System User token** from Business Manager: it does not expire, which
matters for an extraction measured in hours. A Graph API Explorer token lasts
about an hour and will die mid-run.

---

## Architecture

```
Extraction  →  Meta Marketing API          →  Parquet, partitioned by month
Warehouse   →  DuckDB views over Parquet   →  SQL, no server
Analysis    →  segments · triage · creative →  ranked findings + reports
```

The split is deliberate: extraction is quota-bound and runs once; analysis
re-runs freely on what was extracted, without touching the API.

### Extraction

The obvious implementation — loop over days calling `GET /insights` — collapses
under throttling. This uses Meta's **asynchronous insights jobs**, the
documented path for bulk reads:

```
POST /act_<id>/insights          ->  report_run_id
GET  /<report_run_id>            ->  poll until "Job Completed"
GET  /<report_run_id>/insights   ->  paginated rows
```

Three properties make a multi-hour run survivable:

| Concern | Approach |
|---|---|
| Rate limits | Usage headers are parsed on every response; the client sleeps *before* being throttled, not after a 429 |
| Interruption | Every `(pass, month)` unit is checkpointed on completion; rerunning resumes exactly where it stopped |
| Shared quota | The same account serves live production jobs, so pacing is conservative by default |

Five passes trade breadth for depth — the dataset's richness comes from
segmentation, not from history:

| Pass | Segmentation | Rows |
|---|---|---|
| `base` | none | 3,643 |
| `age_gender` | age × gender | 43,422 |
| `placement` | platform × position × device | 88,497 |
| `region` | region | 6,719 |
| `hourly` | hour of day | 24,863 |

### Analysis

Three methodological choices do most of the work of keeping results honest.

**Cluster bootstrap over ads, not rows.** Rows are (ad, day) observations, and
the days of one ad share a creative, an audience and a bid. Resampling rows
treats those repeats as independent evidence and yields intervals far too
narrow; resampling whole ads respects the dependence.

**Within-campaign controls.** Ads inside a campaign share an event and an
audience, so a raw correlation can say "this event sold well" rather than "this
creative works". Centring both sides within campaign isolates the effect that
matters. This changed conclusions three times — most sharply for text
capitalisation, the single strongest raw correlation in the creative analysis
(ρ = −0.346, q = 2·10⁻⁷), which collapsed to −0.129 and lost significance once
the campaign effect was removed.

**False discovery rate control.** Scanning 150 segments at α = 0.05 manufactures
false positives by construction; p-values are Benjamini-Hochberg adjusted.

A quasi-Poisson dispersion is computed and printed, but deliberately does not
drive significance: estimated from between-segment deviations, it absorbs the
very effect under test. An earlier version that used it reported nothing as
significant while the confidence intervals visibly failed to overlap — the
contradiction that motivated the switch to bootstrapping.

### Guard rails

- Segments below a minimum expected count **or** a minimum share of spend are
  excluded and counted, never silently dropped.
- Breakdowns exposing no pixel conversion (`region`) fall back to link clicks
  and are marked ranking-only: a proxy outcome can order segments but cannot
  price a dollar moved between them.
- Reallocation is reported as a range — a linear ceiling and a saturated
  estimate (`conversions ~ spend^0.8`) — because the honest answer is an
  interval, and only an experiment settles it.
- Non-linear models are fitted alongside linear ones as a check. Gradient
  boosting failed to beat logistic regression in all three modules, which says
  the ceiling is the data rather than the model class.

---

## Output

```
data/
├── raw/pass=<name>/month=<YYYY-MM>/part.parquet
├── creatives.parquet
├── thumbnails/<ad_id>.jpg
├── checkpoints.json
└── meta_ads.duckdb
```

Hive-style partitioning makes `pass` and `month` readable as columns by DuckDB,
pandas, polars and Spark alike. Conversion counts use nullable integers so
"Meta reported nothing" stays distinct from "Meta reported zero" — a
distinction that matters when modelling a funnel. The untouched API payload is
kept in `actions_raw`, so an action type not anticipated here is recoverable.

`python explore.py export` writes every view to CSV for spreadsheet use.

---

## Limits

Everything here is **observational**. The allocations analysed were chosen by
Meta's delivery algorithm using signals the dataset does not contain, so a
segment converting better does not mean moving budget to it will reproduce that
rate. The simulations size the opportunity and justify running an experiment;
they do not replace one.

The account was near-dormant before mid-2026: 95% of the data covers two months
of a single season. That ruled out marketing-mix modelling, long-horizon
forecasting and any year-over-year comparison — an assessment made before the
analysis was scoped rather than after it failed.

The conversion metric is `initiate_checkout`, not `purchase`, because purchase
tracking is broken. If checkout-to-purchase rates differ across segments, the
rankings are biased.

---

## Layout

```
meta_backfill/
├── config.py           settings + pass definitions
├── api.py              async-job client, rate limiting, retries
├── creatives.py        ad copy, formats, thumbnail download
├── transform.py        nested actions -> tidy columns
├── checkpoint.py       crash-safe resume
└── runner.py           month iteration and persistence
analysis/
├── stats.py            dispersion, cluster bootstrap, FDR
├── segments.py         segment scoring
├── reallocation.py     budget simulation
├── early_warning.py    day-2 triage
└── creative.py         copy and image features
run_backfill.py         extraction CLI
build_warehouse.py      DuckDB views
run_*_analysis.py       analysis CLIs
make_sample_data.py     synthetic dataset
make_figures.py         report figures, generated from the warehouse
explore.py              browse / export
rapport/                LaTeX report
```

Every figure in the report is produced by `make_figures.py` straight from the
warehouse. No number in it is typed by hand.
