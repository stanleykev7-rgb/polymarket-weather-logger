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

~~Settlement still uses the Open-Meteo Archive proxy, not the official
per-city Wunderground/HKO station data.~~ **Partially resolved (2026-08
update):** real official sources are now wired in for 4 of 14 cities:

| City | Official source | Implementation |
|---|---|---|
| New York | NOAA/NWS, LaGuardia (KLGA) | `official_settlement_sources.py` |
| Chicago | NOAA/NWS, O'Hare (KORD) | `official_settlement_sources.py` |
| Miami | NOAA/NWS, Miami Intl (KMIA) | `official_settlement_sources.py` |
| Hong Kong | Hong Kong Observatory open data | `official_settlement_sources.py` |

These 3 NOAA stations are confirmed by name in Polymarket's own live
market rules pages, and NOAA's `api.weather.gov` reports the same
underlying ASOS/METAR feed Wunderground displays for these airport
stations — no API key needed. HKO's field-name parsing in
`fetch_hko_daily_max_c` was written from documented API structure but
**not verified against a live response** (no network access in the
environment it was built in) — check the console output the first time
it runs for a "response shape unrecognized" warning, and adjust field
names in that function if needed.

The remaining 10 cities (Tokyo, Shanghai, Qingdao, Seoul, Guangzhou,
Shenzhen, London, Paris, Ankara, Buenos Aires) still use the Open-Meteo
proxy — Wunderground has no public API for non-US stations, and
scraping their site directly was deliberately not attempted (fragile,
JS-rendered, ToS considerations). Building that out is the natural next
step for full coverage.

New columns from this change:
- `actual_max_c_official` — real value, only populated for the 4 cities
  above, `NaN` otherwise.
- `actual_max_c_openmeteo_proxy` — **always** computed for every city,
  even the 4 with an official source, so you can empirically compare
  proxy-vs-official accuracy once enough data accumulates.
- `actual_max_c_used` — official value when available, proxy otherwise;
  this is what `actual_bucket`/`ecmwf_hit` are actually calculated from.
- `settlement_source` — `noaa_nws`, `hko_opendata`, or
  `openmeteo_archive_proxy_fallback`, telling you exactly which path
  produced `actual_max_c_used` for that row.

## Dashboard: "Exclude pre-fix rows" toggle

`app.py` has a sidebar checkbox that filters out rows tagged with a
legacy `_schema_version` (`gen_a`/`gen_b`/`gen_c`/`gen_d`, or plain `1`),
i.e. everything collected before the 2026-08 audit fixes. This does
**not** delete or modify any file — it only changes what the current
dashboard session displays. Turn it on for a "clean slate" view; leave
it off to see the full historical dataset.

## Schema v3: ICON added as a third forecast model (2026-08, later same day)

DWD's ICON model (`dwd_icon_seamless` in Open-Meteo's API — global
~11km, blended with higher-resolution ICON EU/D2 near Europe; sometimes
called "ICON13" from its older 13km global resolution) was added
alongside ECMWF and GFS. It's requested in the same Open-Meteo API call
as ECMWF/GFS, so this adds no extra network round-trip per polling
cycle.

New columns: `icon_max_c`, `icon_bucket`, `icon_bucket_probability`,
`icon_change_c` (collector); `icon_hit` (settlement). `ICON` was also
added to the report's model comparison set (`report_builder.py`) and
the Live Snapshot table (`app.py`).

**Deliberately NOT changed:** `model_spread_c`/`abs_model_spread_c`
stay strictly ECMWF-vs-GFS, for continuity with every existing row and
because that's what "model disagreement" has meant throughout this
project so far. ICON's accuracy is evaluated independently via its own
bucket/hit-rate/MAE metrics rather than folded into that spread
calculation. A three-way spread field can be added later if useful.

Since this changes the collector's column count again, new rows will
route to `polymarket_weather_live_log_v3.csv` (or higher) the same way
the v2 rows did — see the schema-versioning mechanism described above.

Adding a 4th, 5th, etc. model later (Open-Meteo supports UKMO, GEM,
JMA, KMA, Météo-France and others in the same API call) follows the
same pattern: add it to the `models=` request, parse its
`temperature_2m_max_{model_id}` field, add the 4 corresponding
columns, and it'll show up automatically in the report and dashboard
since those already iterate generically over whatever models are
present.

## Schema v4: national models + hours_to_resolution (2026-08, later same day)

**City-matched national models added**: JMA (Tokyo), UKMO (London),
Météo-France (Paris) — confirmed live/working via Open-Meteo's own docs
pages. Requested in the same API call as ECMWF/GFS/ICON, so no extra
network round-trip.

**Deliberately NOT added**, despite being the "obvious" match:
- **KMA for Seoul**: Open-Meteo's docs state KMA discontinued their
  UM-based models and updates are "currently suspended" during a
  migration.
- **CMA for Shanghai/Qingdao/Guangzhou/Shenzhen**: Open-Meteo's docs
  state CMA's open-data service has been "heavily overloaded... making
  it nearly impossible to download forecasts reliably."

Both can be revisited once Open-Meteo's own status pages stop flagging
them as degraded — check `https://open-meteo.com/en/docs/kma-api` and
`https://open-meteo.com/en/docs/cma-api` before re-enabling.

New columns: `national_model_name`, `national_model_max_c`,
`national_model_bucket`, `national_model_bucket_probability`,
`national_model_change_c` (collector); `national_model_hit`
(settlement). These are `NaN`/`None` for the 11 cities without a
matched model — expected, not an error.

**Two distinct lead-time framings, both kept** (raised 2026-08):
- `lead_time_hours` (unchanged): hours to the START of the target
  local day. Standard NWP verification convention.
- `hours_to_resolution` (new): hours to the END of the target local
  day — i.e. how close to the moment the day's actual max temperature,
  and therefore the market's outcome, is fully decided. A forecast
  made 6h before the day starts is ~30h before the day ends; these
  answer genuinely different questions and shouldn't be confused. Both
  now have their own chart/table in the dashboard and downloadable
  report, clearly labeled as distinct.

**Dashboard addition**: a "🏆 Model Reliability by City" table now
lives directly in the Backtesting section (not just the downloadable
report), showing each model's hit rate per city and a "Best Model"
column — the direct answer to "does JMA actually track Tokyo better
than ECMWF." Requires 10+ settled observations for a city before
naming a best model; below that it shows "—" rather than guessing from
too little data.

## CRITICAL FIX (2026-08, same day): market discovery was silently missing markets

**Bug found**: `get_polymarket_prices_multi_date` returned on the
FIRST candidate date with an open market and stopped — it never
checked later dates in the same polling cycle. Combined with
`candidate_dates` being capped at the 3 nearest days, this meant the
collector could get stuck logging only the earliest currently-open
market for a city (e.g. always Aug 15) even while Polymarket had Aug
16/17/18 simultaneously open — those later markets were never even
checked, let alone logged.

**Fix**: `get_polymarket_prices_multi_date` now checks every candidate
date and returns ALL matches, not just the first. `candidate_dates` no
longer caps at 3 — it uses every date Open-Meteo's forecast response
actually returned. `log_snapshot()` now loops over every matched
(target_date, prices) pair and logs a separate row for each, so a
single polling cycle correctly captures every simultaneously-open
market for a city, not just one.

**Related correctness fix**: `load_previous_snapshot()` (used to
compute `ecmwf_change_c`/`gfs_change_c`/etc., the "change vs previous
observation" fields) was keyed by city alone. Once a city can have
multiple target dates in flight in the same cycle, that meant a
"change" value could silently compare two DIFFERENT target dates'
forecasts against each other rather than the same day an hour apart.
Now keyed by `(city, target_date)`, so these deltas mean what they're
supposed to mean.

Verified with a mocked scenario: Tokyo with 3 simultaneously-open
markets (Aug 16/17/18) now correctly produces 3 rows in one cycle
(previously would have produced 1), and a repeat cycle correctly
isolates each date's own change-vs-previous rather than blending them.

## CRITICAL FIX (2026-08, same day): forecast coordinates now match the exact settlement station

**Finding**: every city's forecast (ECMWF/GFS/ICON/national model) was being
pulled for a generic city-center lat/lon, not the specific station
Polymarket actually names as its resolution source. Confirmed by reading
live Polymarket rules pages for all 14 cities. Two are not even in the
named city:

| City | Actual settlement station | Notes |
|---|---|---|
| Hong Kong | HKO Observatory HQ (Tsim Sha Tsui) | non-airport; official HKO source, not Wunderground |
| Tokyo | Haneda Airport (RJTT) | |
| Shanghai | Pudong Intl Airport (ZSPD) | |
| Qingdao | Jiaodong Intl Airport (ZSQD) | opened 2021, ~39km from city center, replaced Liuting |
| **Seoul** | **Incheon Intl Airport (RKSI)** | **not in Seoul — a separate city ~50km away** |
| Guangzhou | Baiyun Intl Airport (ZGGG) | |
| Shenzhen | Bao'an Intl Airport (ZGSZ) | |
| New York | LaGuardia Airport (KLGA) | not JFK, not Manhattan |
| Chicago | O'Hare Intl Airport (KORD) | |
| Miami | Miami Intl Airport (KMIA) | |
| London | London City Airport (EGLC) | not Heathrow |
| Paris | Le Bourget Airport (LFPB) | not Charles de Gaulle |
| Ankara | Esenboğa Intl Airport (LTAC) | |
| **Buenos Aires** | **Ministro Pistarini/Ezeiza Airport (SAEZ)** | **~35km southwest of downtown** |

**Fix**: `CITIES` in both `weather_collector.py` and `settle_outcomes.py`
now use these exact station coordinates instead of city-center
coordinates. Verified both files' coordinates are byte-identical to each
other after the change.

**This is a silent-but-significant change** — no new CSV columns, so it
won't show up as a schema version bump, but every forecast logged from
this point forward represents a meaningfully different (more correct)
physical location for several cities, especially Seoul and Buenos Aires.
Historical rows before this fix used the old city-center coordinates —
this is intentional, not a bug, per the same "never rewrite history"
principle as everything else in this project. If you want to mark a
before/after cutover point in your own analysis, this commit is that
line.

**Not yet done**: NOAA/HKO settlement fetching in
`official_settlement_sources.py` was unaffected by this change (it already
queries by station ID directly, e.g. `KLGA`, not by lat/lon), so no update
was needed there.

## SECOND market-discovery fix (2026-08, later same day): candidate dates were still capped by the forecast API's response window

**Finding**: even after the first market-discovery fix (checking every
candidate date instead of stopping at the first match), `candidate_dates`
was still *generated from* `forecasts_by_date.keys()` — i.e. from
whatever dates Open-Meteo's forecast response happened to include.
Open-Meteo's `/v1/forecast` defaults to a 7-day window unless
`forecast_days` is explicitly set (confirmed via their own docs — up to
16 is supported). This meant a Polymarket market open further out than
that response window (or shortened by a transient API response) could
never even be *checked*, regardless of the earlier fix.

**Fix, two parts**:
1. `get_weather_forecast` now explicitly requests
   `forecast_days=CANDIDATE_DATE_WINDOW_DAYS + 2` (12 by default) instead
   of relying on Open-Meteo's default.
2. `candidate_dates` is now generated independently by plain date
   arithmetic in each city's **local** timezone (today through
   `CANDIDATE_DATE_WINDOW_DAYS` = 10 days ahead), not derived from the
   forecast response at all. Polymarket is checked across this full
   window regardless of what the forecast API returned. If a specific
   date's forecast happens to be missing from that response, the row is
   still logged (Polymarket price data intact) with forecast fields as
   `None` and a new `FORECAST_MISSING_FOR_TARGET_DATE` data-quality flag,
   instead of the date being silently skipped entirely.

**Verified** with a mocked scenario: Open-Meteo's response covering only
3 days, Polymarket having a market open on day+8 (day 9 total, well
outside that response) — confirmed the day+8 market is still found,
logged, and correctly flagged for its missing forecast values, rather
than silently dropped. Also verified normal full-coverage operation
produces clean rows with no spurious flags.
