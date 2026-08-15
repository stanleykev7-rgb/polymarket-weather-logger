# Data Schema Notes

This file documents the raw log schema, its history, and known
limitations, per the 2026-08 independent audit. Read this before writing
any analysis against `polymarket_weather_live_log*.csv` or
`polymarket_weather_evaluated.csv`.

## Files

- `polymarket_weather_live_log.csv` — the original raw log file. **Frozen
  going forward.** Contains a mix of four historical row layouts under
  one stale 8-column header (see "Legacy schema generations" below). Do
  not append to this file anymore; do not rewrite it.
- `polymarket_weather_live_log_v2.csv` (and any future `_v3`, `_v4`, ...)
  — new rows land here once `weather_collector.py`'s column set changes.
  Each version file has a single consistent header for all its rows.
- `recovered/live_log_gen_{a,b,c,d}.csv` — read-only output of
  `recover_legacy_log.py`, splitting the mixed rows in the original v1
  file into four correctly-labeled, individually-consistent CSVs. This
  is what `data_utils.load_combined_log()` actually reads for the "v1"
  portion of the data, since a plain `pd.read_csv` on the raw file was
  silently dropping 57% of its rows.
- `polymarket_weather_evaluated.csv` — fully regenerated on every run of
  `settle_outcomes.py` (never appended to), from the unioned combined
  log.

**Always read the log via `data_utils.load_combined_log("polymarket_weather_live_log.csv")`**
rather than `pd.read_csv` directly on any single file — that's the only
path that returns the complete, correctly-labeled dataset across all
schema generations/versions.

## Legacy schema generations (all inside the original v1 file)

| Gen | Fields | Rows | Notes |
|---|---|---|---|
| A | 8 | 127 | Matches the file's literal (only-ever-written) header. |
| B | 9 | 6 | Gen A + one extra human-readable pipe-joined probability string before `all_bucket_prices`. Redundant with the JSON; preserved as `bucket_summary_readable_deprecated`. |
| C | 10 | 7 | Early "enhanced" column order, before the layout settled: `lead_time_hours` appears right after `target_date`, `model_spread_c` appears before `predicted_bucket`. |
| D | 23 | 154 | The full pre-audit `CSV_COLUMNS` layout (what most of the existing data actually is). |

None of these rows were modified, reordered, or deleted — `recover_legacy_log.py`
only re-labels each row's existing fields with the correct column names for
its generation and writes them to separate files.

## Field semantics — read carefully

- **`predicted_bucket` / `polymarket_price`**: kept with their
  **original, as-implemented** meaning for continuity with every
  historical row — the Polymarket bucket that matches the **ECMWF**
  forecast, and that bucket's price. They are **not** the market's
  favorite (modal) bucket, despite what the names suggest.
- **`ecmwf_bucket` / `ecmwf_bucket_probability`**: honestly-named
  duplicates of the above, introduced in schema v2.
- **`gfs_bucket` / `gfs_bucket_probability`**: the bucket matching the
  **GFS** forecast, and its price.
- **`market_modal_bucket` / `market_modal_bucket_price`** (schema v2,
  new): the market's actual current favorite (highest-priced) bucket,
  independent of either model. This is what `predicted_bucket` was
  probably intended to mean originally — use these fields for that.
- **`market_implied_temp_c`**: probability-weighted mean across all
  priced buckets. Open-ended buckets ("35°C or higher") use a
  documented `value ± 0.5` approximation, not a value derived from the
  contract. `market_implied_temp_used_open_bucket_approx` (schema v2,
  new) flags rows where that approximation was actually used.
- **`model_spread_c`**: signed, `ecmwf_max_c − gfs_max_c`. Negative means
  ECMWF is colder than GFS.
- **`lead_time_hours`** (schema v2, fixed): hours from the observation
  timestamp to the start of the target's **city-local** calendar day,
  computed via each city's IANA timezone (correctly handles DST for
  New York/Chicago/London/Paris). Pre-v2 rows used UTC midnight instead
  and are off by the city's UTC offset — do not mix pre/post-fix
  `lead_time_hours` values in the same regression without accounting
  for this.
- **`actual_max_c_openmeteo_proxy`** (evaluated CSV; renamed from
  `actual_max_c`): Open-Meteo Archive reanalysis value, aggregated over
  the city's **local** calendar day (schema-v2 fix — was previously
  aggregated over the UTC day). This is a **proxy**, not the official
  settlement source. Polymarket's actual resolution source is a named
  Wunderground airport station per city (e.g. Tokyo Haneda/RJTT, NYC
  LaGuardia), except Hong Kong, which resolves from the Hong Kong
  Observatory's own daily extract directly. No station-scraper exists
  in this project yet — building one is the natural next step before
  treating settlement outcomes as authoritative.
- **`ecmwf_hit`**: `True`/`False`/`None`. `None` means the actual
  temperature couldn't be mapped to any real market bucket (previously
  this silently became `False`, indistinguishable from a genuine wrong
  prediction).
- **`ecmwf_run_utc` / `gfs_run_utc`**: always `None` — the Open-Meteo
  forecast endpoint used here doesn't expose true model
  initialization timestamps. Not fabricated.

## Known open item

Settlement still uses the Open-Meteo Archive proxy, not the official
per-city Wunderground/HKO station data. This is flagged, not silently
assumed correct — see the note at the top of `settle_outcomes.py`.
Building a real station scraper is the highest-value next step for
backtest fidelity.
