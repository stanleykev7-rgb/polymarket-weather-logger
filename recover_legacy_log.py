"""
One-time, READ-ONLY recovery for polymarket_weather_live_log.csv.

FINDING (2026-08 audit, confirmed by direct inspection):
The single raw log file already contains FOUR different row layouts,
all under one 8-column header written the first time the file was
created. Every existing reader in this project (the dashboard, the
settlement script, and the collector's own load_previous_snapshot)
uses pandas with on_bad_lines='skip', which means every row that
doesn't have exactly 8 fields has been SILENTLY DROPPED on every read.

Measured on the current file: 294 total data rows, only 127 (43%) have
the original 8 fields. The remaining 167 rows (57%) have 9, 10, or 23
fields and have never been successfully read by any script in this
repo until now.

This script does NOT modify, delete, or reorder the original CSV. It
only reads it and writes out clean, correctly-labeled copies of the
rows it finds, split by the schema generation that was in effect when
each row was written. It is safe to re-run any number of times.

Schema generations identified by direct inspection of row content
(matching timestamps/values across generations to confirm alignment):

  Gen A (8 fields)  -- matches the file's original declared header:
      timestamp_utc, city, target_date, ecmwf_max_c, gfs_max_c,
      predicted_bucket, polymarket_price, all_bucket_prices

  Gen B (9 fields)  -- Gen A layout plus one extra human-readable
      pipe-joined probability summary string inserted before
      all_bucket_prices. Short-lived (6 rows observed). That extra
      field carried no information not already in all_bucket_prices
      (it's a rendering of the same JSON), so it's preserved under an
      honest name but not treated as a first-class metric.
      timestamp_utc, city, target_date, ecmwf_max_c, gfs_max_c,
      predicted_bucket, polymarket_price,
      bucket_summary_readable_deprecated, all_bucket_prices

  Gen C (10 fields) -- an early "enhanced" ordering, before columns
      were finalized to the Gen D order. Confirmed by cross-checking
      lead_time_hours values against adjacent Gen D rows for the same
      city minutes later (values decrease consistently with elapsed
      time, confirming the field identity):
      timestamp_utc, city, target_date, lead_time_hours, ecmwf_max_c,
      gfs_max_c, model_spread_c, predicted_bucket, polymarket_price,
      all_bucket_prices

  Gen D (23 fields) -- matches CSV_COLUMNS as implemented in
      weather_collector.py prior to this audit's schema-v2 changes:
      timestamp_utc, city, target_date, ecmwf_max_c, gfs_max_c,
      predicted_bucket, polymarket_price, all_bucket_prices,
      snapshot_id, lead_time_hours, model_spread_c, abs_model_spread_c,
      ecmwf_run_utc, gfs_run_utc, market_implied_temp_c,
      market_vs_ecmwf_c, market_vs_gfs_c, ecmwf_bucket_probability,
      gfs_bucket_probability, ecmwf_change_c, gfs_change_c,
      market_implied_change_c, data_quality

Any row whose field count doesn't match one of the four known
generations is written to `recovered/unrecognized_rows.csv` (raw,
untouched fields) with a note printed to stdout, rather than being
silently dropped -- so nothing simply disappears.
"""

from __future__ import annotations

import csv
import os

RAW_FILE = "polymarket_weather_live_log.csv"
OUT_DIR = "recovered"

GEN_A_COLS = [
    "timestamp_utc", "city", "target_date", "ecmwf_max_c", "gfs_max_c",
    "predicted_bucket", "polymarket_price", "all_bucket_prices",
]

GEN_B_COLS = [
    "timestamp_utc", "city", "target_date", "ecmwf_max_c", "gfs_max_c",
    "predicted_bucket", "polymarket_price",
    "bucket_summary_readable_deprecated", "all_bucket_prices",
]

GEN_C_COLS = [
    "timestamp_utc", "city", "target_date", "lead_time_hours",
    "ecmwf_max_c", "gfs_max_c", "model_spread_c", "predicted_bucket",
    "polymarket_price", "all_bucket_prices",
]

GEN_D_COLS = [
    "timestamp_utc", "city", "target_date", "ecmwf_max_c", "gfs_max_c",
    "predicted_bucket", "polymarket_price", "all_bucket_prices",
    "snapshot_id", "lead_time_hours", "model_spread_c",
    "abs_model_spread_c", "ecmwf_run_utc", "gfs_run_utc",
    "market_implied_temp_c", "market_vs_ecmwf_c", "market_vs_gfs_c",
    "ecmwf_bucket_probability", "gfs_bucket_probability",
    "ecmwf_change_c", "gfs_change_c", "market_implied_change_c",
    "data_quality",
]

GENERATIONS = {
    8: ("gen_a", GEN_A_COLS),
    9: ("gen_b", GEN_B_COLS),
    10: ("gen_c", GEN_C_COLS),
    23: ("gen_d", GEN_D_COLS),
}


def recover():
  if not os.path.exists(RAW_FILE):
    print(f"'{RAW_FILE}' not found.")
    return

  os.makedirs(OUT_DIR, exist_ok=True)

  buckets = {name: [] for name, _ in GENERATIONS.values()}
  unrecognized = []

  with open(RAW_FILE, "r", encoding="utf-8-sig", newline="") as f:
    reader = csv.reader(f)
    next(reader)  # skip the (only-ever-8-column) declared header
    for row in reader:
      n = len(row)
      if n in GENERATIONS:
        name, _ = GENERATIONS[n]
        buckets[name].append(row)
      else:
        unrecognized.append(row)

  total = 0
  for n, (name, cols) in GENERATIONS.items():
    rows = buckets[name]
    total += len(rows)
    if not rows:
      continue
    out_path = os.path.join(OUT_DIR, f"live_log_{name}.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
      writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
      writer.writerow(cols)
      writer.writerows(rows)
    print(f"  {name} ({n} fields): {len(rows)} rows -> {out_path}")

  if unrecognized:
    out_path = os.path.join(OUT_DIR, "unrecognized_rows.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
      writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
      writer.writerows(unrecognized)
    print(
        f"  UNRECOGNIZED: {len(unrecognized)} rows with an unexpected"
        f" field count -> {out_path} (inspect manually)"
    )

  print(
      f"\nRecovered {total} rows across {len([b for b in buckets.values() if b])}"
      f" known schema generations, plus {len(unrecognized)} unrecognized."
      f" Original '{RAW_FILE}' was not modified."
  )


if __name__ == "__main__":
  recover()
