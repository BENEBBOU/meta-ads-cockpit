"""Budget reallocation simulator.

Given segments that are statistically worse than the account average, ask what
happens if their budget moves to the ones that are statistically better.

The honest answer has bounds, not a point estimate:

* **Linear ceiling.** Moved budget converts at the recipient's current rate.
  This is an upper bound and it is certainly optimistic: no channel keeps its
  efficiency while its budget grows.
* **Saturated estimate.** Conversions grow as ``spend ** alpha`` with
  ``alpha < 1``. Performance marketing response curves typically sit around
  0.7-0.9; 0.8 is the default here.

Both are simulations built on observational data. Delivery already chose the
current allocation for reasons the data does not record, so the only way to
settle the question is a geo- or audience-split experiment. The numbers below
size the opportunity and justify running that test — they do not replace it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_SATURATION = 0.8


@dataclass
class ReallocationResult:
    donors: pd.DataFrame
    recipients: pd.DataFrame
    freed_spend: float
    lost_conversions: float
    baseline_conversions: float
    linear_conversions: float
    saturated_conversions: float
    saturation: float

    @property
    def linear_gain(self) -> float:
        return self.linear_conversions - self.baseline_conversions

    @property
    def saturated_gain(self) -> float:
        return self.saturated_conversions - self.baseline_conversions

    @property
    def linear_gain_pct(self) -> float:
        return _pct(self.linear_gain, self.baseline_conversions)

    @property
    def saturated_gain_pct(self) -> float:
        return _pct(self.saturated_gain, self.baseline_conversions)


def _pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole else 0.0


def simulate(
    frame: pd.DataFrame,
    *,
    saturation: float = DEFAULT_SATURATION,
    donor_index_max: float = 1.0,
    recipient_index_min: float = 1.0,
) -> ReallocationResult | None:
    """Move budget from significant under-performers to significant winners.

    ``frame`` is the output of :func:`analysis.segments.analyse`. Only segments
    flagged ``significant`` participate, so noise never drives a
    recommendation. Recipients receive the freed budget in proportion to their
    current spend — scaling what already works rather than betting everything
    on the single best segment, which would be the fastest way to saturate it.
    """
    if frame.empty or "significant" not in frame.columns:
        return None

    donors = frame[
        frame["significant"] & (frame["perf_index"] < donor_index_max)
    ].copy()
    recipients = frame[
        frame["significant"] & (frame["perf_index"] > recipient_index_min)
    ].copy()

    if donors.empty or recipients.empty:
        return None

    baseline = float(frame["conversions"].sum())
    freed = float(donors["spend"].sum())
    lost = float(donors["conversions"].sum())

    recipient_spend = float(recipients["spend"].sum())
    if recipient_spend <= 0:
        return None

    share = recipients["spend"] / recipient_spend
    extra = share * freed
    recipients["extra_spend"] = extra
    recipients["new_spend"] = recipients["spend"] + extra

    # Linear: the moved budget converts at the recipient's observed rate.
    recipients["linear_conversions"] = recipients["rate"] * recipients["new_spend"]

    # Saturated: conversions scale with the spend growth factor ** alpha.
    growth = recipients["new_spend"] / recipients["spend"]
    recipients["saturated_conversions"] = recipients["conversions"] * growth ** saturation

    untouched = baseline - lost - float(recipients["conversions"].sum())
    linear_total = untouched + float(recipients["linear_conversions"].sum())
    saturated_total = untouched + float(recipients["saturated_conversions"].sum())

    return ReallocationResult(
        donors=donors.sort_values("spend", ascending=False),
        recipients=recipients.sort_values("extra_spend", ascending=False),
        freed_spend=freed,
        lost_conversions=lost,
        baseline_conversions=baseline,
        linear_conversions=linear_total,
        saturated_conversions=saturated_total,
        saturation=saturation,
    )
