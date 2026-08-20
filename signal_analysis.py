"""
Signal analysis: for each (city, model, time-to-resolution window),
compares the OBSERVED hit rate against what the market itself priced
that model's matched bucket at, on average, across the same
observations.

This is deliberately NOT a buy/sell/edge-score/recommendation engine --
see the project's own founding principle (kept from the original audit
brief): trading logic stays out of this layer. What this module
produces is a descriptive statistical comparison: "here's what the data
shows, with real confidence intervals and honest sample-size caveats."
Reading it and deciding what to do with it is left entirely to the
person using the dashboard (or another analyst/agent consuming the
export).

Methodology notes (also included in the JSON export's own
"methodology" field, so a reader doesn't have to trust this docstring
was actually followed):

- "Market price" for a signal cell is the MEAN of the actual priced
  probability the market assigned to that model's matched bucket, at
  the moment of each contributing observation (ecmwf_bucket_probability
  / gfs_bucket_probability / icon_bucket_probability /
  national_model_bucket_probability / market_modal_bucket_price) --
  not a synthetic or generic estimate.
- "Edge" = observed hit rate minus that mean market price, in
  percentage points. Positive means the model won more often than the
  market's own pricing implied it should; negative means less often.
- Hit rate and its 95% confidence interval (Wilson score interval) are
  computed from ROW-level observations -- i.e. treating each hourly
  snapshot as one Bernoulli trial. This is intentionally consistent
  internally (the rate and its interval come from the same n), but
  overstates independence: many rows can be repeated hourly
  observations of the SAME still-unresolved market, not independent
  market outcomes.
- Because of that, "n_distinct_markets" (count of distinct (city,
  target_date) pairs contributing to the cell) is reported SEPARATELY
  and is what the sample-size filter in the dashboard actually filters
  on -- it is the more conservative, meaningful measure of how much
  real information backs a given cell. n_rows is shown alongside it,
  never hidden.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from report_builder import MODEL_HIT_COLS, RESOLUTION_TIME_BINS, RESOLUTION_TIME_LABELS, _settled_subset

# Column holding the market's actual priced probability for each
# model's matched bucket, per observation -- see module docstring.
MODEL_PRICE_COLS = {
    "ECMWF": "ecmwf_bucket_probability",
    "GFS": "gfs_bucket_probability",
    "ICON": "icon_bucket_probability",
    "National Model": "national_model_bucket_probability",
    "Market Favorite": "market_modal_bucket_price",
}


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score confidence interval for a binomial proportion.
    Preferred over the naive normal-approximation interval because it
    stays within [0, 1] and behaves sensibly for small n or rates near
    0/1 -- both of which are common here given how thin some cells are.
    """
    if n == 0:
        return None, None
    phat = hits / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    lower = max(0.0, (center - margin) / denom)
    upper = min(1.0, (center + margin) / denom)
    return lower, upper


def compute_signal_candidates(df: pd.DataFrame, city: str | None = None) -> pd.DataFrame:
    """One row per (city, model, resolution-time-window) cell with
    enough structure to have a hit rate at all. See module docstring
    for exactly what each column means.
    """
    settled = _settled_subset(df, city)
    if settled.empty or "hours_to_resolution" not in settled.columns:
        return pd.DataFrame(columns=[
            "city", "model", "window", "hit_rate", "ci_low", "ci_high",
            "market_price_mean", "edge", "n_rows", "n_distinct_markets",
        ])

    settled = settled.copy()
    settled["resolution_bucket"] = pd.cut(
        settled["hours_to_resolution"], bins=RESOLUTION_TIME_BINS, labels=RESOLUTION_TIME_LABELS
    )

    rows = []
    cities_to_scan = [city] if city else sorted(settled["city"].dropna().unique())
    for c in cities_to_scan:
        city_df = settled[settled["city"] == c]
        for model, hit_col in MODEL_HIT_COLS.items():
            price_col = MODEL_PRICE_COLS.get(model)
            if hit_col not in city_df.columns:
                continue
            for window in RESOLUTION_TIME_LABELS:
                group = city_df[city_df["resolution_bucket"] == window]
                hit_vals = group[hit_col].dropna()
                if hit_vals.empty:
                    continue
                n_rows = len(hit_vals)
                hits = int(hit_vals.sum())
                hit_rate = hits / n_rows
                ci_low, ci_high = wilson_ci(hits, n_rows)

                price_mean = None
                if price_col and price_col in group.columns:
                    # Only average price over the SAME rows that have a
                    # known hit/miss outcome, so rate and baseline are
                    # comparing the same underlying observations.
                    price_vals = group.loc[hit_vals.index, price_col].dropna()
                    if len(price_vals):
                        price_mean = float(price_vals.mean())

                n_distinct = group.loc[hit_vals.index, "target_date"].nunique() if "target_date" in group.columns else n_rows

                edge = (hit_rate - price_mean) if price_mean is not None else None

                rows.append({
                    "city": c,
                    "model": model,
                    "window": window,
                    "hit_rate": round(hit_rate, 4),
                    "ci_low": round(ci_low, 4) if ci_low is not None else None,
                    "ci_high": round(ci_high, 4) if ci_high is not None else None,
                    "market_price_mean": round(price_mean, 4) if price_mean is not None else None,
                    "edge": round(edge, 4) if edge is not None else None,
                    "n_rows": n_rows,
                    "n_distinct_markets": int(n_distinct),
                })

    return pd.DataFrame(rows)


def build_signal_export(candidates: pd.DataFrame, min_n: int, city_scope: str) -> dict:
    """JSON-serializable export payload. Includes a `methodology` block
    so a human or another AI agent reading this later doesn't have to
    trust undocumented assumptions about what n/edge/hit_rate mean.
    """
    filtered = candidates[candidates["n_distinct_markets"] >= min_n].copy()
    filtered = filtered.sort_values("edge", key=lambda s: s.abs(), ascending=False)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": city_scope,
        "min_sample_size_distinct_markets": min_n,
        "methodology": {
            "hit_rate": "Fraction of row-level observations where this model's matched bucket equaled the actual settled bucket.",
            "confidence_interval": "95% Wilson score interval, computed from n_rows (row-level observations treated as trials).",
            "market_price_mean": "Mean of the market's actual priced probability for this model's matched bucket, averaged across the same observations used for hit_rate.",
            "edge": "hit_rate minus market_price_mean, in probability units (multiply by 100 for percentage points). Positive means this model beat the market's own pricing in this sample.",
            "n_rows": "Row-level observation count (may include multiple hourly observations of the same still-resolving market -- not independent samples).",
            "n_distinct_markets": "Count of distinct (city, target_date) markets contributing -- the more conservative sample-size measure; this is what min_sample_size filters on.",
            "caveat": "Descriptive statistics only. Not a trading recommendation. Settlement uses an Open-Meteo proxy for most cities -- see SCHEMA.md.",
        },
        "signals": filtered.to_dict(orient="records"),
    }
