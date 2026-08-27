"""
PILOT backfill script -- Tokyo only, ~30 days -- to validate that
historical (forecast, market price, actual outcome) triples can
actually be reconstructed before committing to the full 14-city,
~5.5-month build.

This is a ONE-TIME, MANUALLY-RUN utility, not part of the hourly/daily
GitHub Actions automation. It only READS from external APIs and WRITES
to a new, clearly-separate output file -- it never touches
polymarket_weather_live_log*.csv or polymarket_weather_evaluated.csv.

--- WHAT THIS PULLS, AND FROM WHERE ------------------------------------
1. Market discovery + final settlement: Polymarket Gamma API
   (gamma-api.polymarket.com) -- same pattern already used in
   weather_collector.py, but with closed=true to find RESOLVED markets
   for past dates. The resolved bucket is read directly from the event
   data (no need to re-derive it from temperature -- Polymarket already
   tells us which outcome won).
2. Historical market price trajectory: Polymarket CLOB API
   (clob.polymarket.com/prices-history), keyed by the CLOB token ID
   found in the Gamma event's `clobTokenIds` field. Requested at
   fidelity=720 (12 hours) -- a GitHub issue against Polymarket's own
   client library (Polymarket/py-clob-client#216) reports that finer
   fidelity can silently return EMPTY data for already-resolved
   markets. 12h is chosen to be safely within what's been confirmed to
   work, not because it's the ideal granularity.
3. Historical forecasts at fixed lead times: Open-Meteo's Previous Runs
   API (previous-runs-api.open-meteo.com), requesting
   temperature_2m_max_previous_day1 through _day4 for ECMWF and GFS --
   the same two models weather_collector.py has always used, for
   direct comparability. This is ONE call for the whole date range, not
   one per day.

--- HONEST STATUS -------------------------------------------------------
Every individual piece above was verified against real, current
documentation/discussion (see chat history / SCHEMA.md for sources).
What has NOT been verified is actually calling these three APIs
together end-to-end with real network access, which this sandbox
doesn't have. Run this yourself and tell me what breaks -- treat it as
a pilot, not a finished pipeline. Likely rough edges: exact Gamma
closed-event query params, exact shape of `clobTokenIds`, and how
cleanly the CLOB price series aligns with the previous-run forecast
timestamps.

--- OUTPUT ---------------------------------------------------------------
Writes polymarket_weather_backfill_pilot.csv with a `_schema_version`
column set to "backfill_pilot_v1", clearly distinguishing these rows
from anything the live hourly collector produced. Union this in via
data_utils.load_combined_log the same way any other schema version is
handled, once you've reviewed the output makes sense.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

PILOT_CITY = "Tokyo"
PILOT_SLUG_TAG = "tokyo"
PILOT_ICAO = "RJTT"
PILOT_TZ = "Asia/Tokyo"
PILOT_UNIT = "C"

# Start conservative -- 14 days, not 30, for the very first run. Widen
# once this is confirmed to work at all.
PILOT_DAYS_BACK = 14

OUTPUT_CSV = "polymarket_weather_backfill_pilot.csv"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
PREVIOUS_RUNS_BASE = "https://previous-runs-api.open-meteo.com/v1/forecast"

LEAD_DAYS = [1, 2, 3, 4]  # matches Polymarket markets typically opening ~4 days ahead


def fetch_resolved_event(slug_tag: str, target_date: datetime) -> dict | None:
    """Look up a RESOLVED Polymarket event for this city/date via the
    same slug pattern the live collector uses, requesting closed events
    this time. Returns the raw event JSON, or None if not found.
    """
    month_name = target_date.strftime("%B").lower()
    day_num = target_date.day
    patterns = [
        f"highest-temperature-in-{slug_tag}-on-{month_name}-{day_num}",
        f"highest-temperature-in-{slug_tag}-on-{month_name}-{day_num}-{target_date.year}",
    ]
    for slug in patterns:
        try:
            res = requests.get(f"{GAMMA_BASE}/events/slug/{slug}", timeout=15)
            if res.status_code == 200:
                event = res.json()
                if event and event.get("closed", False):
                    return event
        except Exception as e:
            print(f"    [gamma] slug lookup failed for {slug}: {e}")
    return None


def extract_resolution_and_tokens(event: dict) -> tuple[str | None, dict[str, str]]:
    """From a resolved event, get (winning_bucket_label, {bucket_label:
    clob_token_id}) so we know both the outcome and which token to pull
    price history for.
    """
    winning_bucket = None
    tokens = {}
    for market in event.get("markets", []):
        label = market.get("groupItemTitle") or market.get("question")
        if not label:
            continue
        raw_tokens = market.get("clobTokenIds")
        if raw_tokens:
            try:
                token_list = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
                if token_list:
                    tokens[label] = token_list[0]  # first token = "Yes" outcome for this bucket
            except (json.JSONDecodeError, TypeError):
                pass
        raw_prices = market.get("outcomePrices")
        if raw_prices:
            try:
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                if prices and float(prices[0]) > 0.9:  # resolved "Yes" settles near 1.0
                    winning_bucket = label
            except (json.JSONDecodeError, TypeError, ValueError, IndexError):
                pass
    return winning_bucket, tokens


def fetch_price_history(token_id: str, start_ts: int, end_ts: int, fidelity_min: int = 720) -> list[dict]:
    """Historical price series for one CLOB token. fidelity=720 (12h)
    chosen defensively -- see module docstring re: GitHub issue #216
    reporting finer fidelity can return empty for resolved markets.
    """
    try:
        res = requests.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity_min},
            timeout=15,
        )
        if res.status_code == 200:
            return res.json().get("history", [])
    except Exception as e:
        print(f"    [clob] price history fetch failed for token {token_id}: {e}")
    return []


def fetch_previous_runs(lat: float, lon: float, tz: str, start_date: str, end_date: str) -> pd.DataFrame:
    """ONE call covering the whole date range.

    FIX (real error from a live run): originally requested variable
    names with the model suffix baked in
    (temperature_2m_max_ecmwf_ifs025_previous_day1), which Open-Meteo
    rejected outright (HTTP 400, "Cannot initialize ForecastVariableDaily
    from invalid String value"). Confirmed via Open-Meteo's own example
    that the correct pattern is the BASE variable name +
    _previous_dayN, with NO model name in the requested variable --
    exactly the same convention weather_collector.py already uses
    correctly for the regular forecast endpoint (request
    `temperature_2m_max`, models are a separate `models=` parameter,
    and the RESPONSE comes back with model-suffixed keys).

    The exact response column naming for the previous-runs endpoint
    specifically hasn't been confirmed against a live call yet, so the
    caller (run_pilot) takes whatever columns actually come back
    generically rather than assuming a specific name -- if this is
    still slightly off, the next run's printed column list will show
    exactly what to fix instead of failing blind a second time.
    """
    daily_vars = [f"temperature_2m_max_previous_day{d}" for d in LEAD_DAYS]

    try:
        res = requests.get(
            PREVIOUS_RUNS_BASE,
            params={
                "latitude": lat, "longitude": lon, "timezone": tz,
                "start_date": start_date, "end_date": end_date,
                "models": "ecmwf_ifs025,gfs_seamless",
                "daily": ",".join(daily_vars),
            },
            timeout=30,
        )
        if res.status_code != 200:
            print(f"    [open-meteo previous-runs] HTTP {res.status_code}: {res.text[:300]}")
            return pd.DataFrame()
        data = res.json()
        daily = data.get("daily", {})
        if not daily or "time" not in daily:
            print("    [open-meteo previous-runs] Unexpected response shape, no 'daily.time' found.")
            return pd.DataFrame()
        print(f"    [open-meteo previous-runs] response columns: {list(daily.keys())}")
        return pd.DataFrame(daily)
    except Exception as e:
        print(f"    [open-meteo previous-runs] fetch failed: {e}")
        return pd.DataFrame()


def run_pilot():
    print(f"=== Backfill pilot: {PILOT_CITY}, last {PILOT_DAYS_BACK} days ===\n")

    today = datetime.now(timezone.utc).date()
    dates = [today - timedelta(days=i) for i in range(3, PILOT_DAYS_BACK + 3)]  # skip last 2 days (likely unresolved)

    print("Step 1/2: fetching Open-Meteo previous-runs forecast series (one call)...")
    from weather_collector import CITIES  # reuse the already-corrected station coordinates
    city_info = CITIES[PILOT_CITY]
    prev_runs_df = fetch_previous_runs(
        city_info["lat"], city_info["lon"], PILOT_TZ,
        dates[-1].isoformat(), dates[0].isoformat(),
    )
    if prev_runs_df.empty:
        print("  FAILED -- no previous-runs data returned. Stopping pilot here; nothing written.")
        return
    print(f"  Got {len(prev_runs_df)} days of previous-runs forecast data.\n")

    print("Step 2/2: fetching resolved markets + price history, one date at a time...")
    rows = []
    for target_date in dates:
        date_str = target_date.isoformat()
        event = fetch_resolved_event(PILOT_SLUG_TAG, datetime.combine(target_date, datetime.min.time()))
        if event is None:
            print(f"  {date_str}: no resolved market found (may not exist yet or slug pattern mismatch)")
            time.sleep(0.3)
            continue

        winning_bucket, tokens = extract_resolution_and_tokens(event)
        if winning_bucket is None:
            print(f"  {date_str}: found event but couldn't identify winning bucket")
            time.sleep(0.3)
            continue

        print(f"  {date_str}: resolved to '{winning_bucket}', {len(tokens)} outcome tokens found")

        # Pull price history for the WINNING bucket's token specifically,
        # to see how its price evolved leading up to resolution.
        token_id = tokens.get(winning_bucket)
        price_points = []
        if token_id:
            start_ts = int((datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=7)).timestamp())
            end_ts = int((datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)).timestamp())
            price_points = fetch_price_history(token_id, start_ts, end_ts)
            print(f"    -> {len(price_points)} price points retrieved for winning bucket's token")

        day_row = prev_runs_df[prev_runs_df.get("time", pd.Series(dtype=str)) == date_str]
        forecast_vals = day_row.iloc[0].to_dict() if not day_row.empty else {}

        rows.append({
            "city": PILOT_CITY,
            "target_date": date_str,
            "winning_bucket": winning_bucket,
            "n_price_points": len(price_points),
            "price_points_json": json.dumps(price_points[:20]),  # cap for sanity in a pilot
            **{k: v for k, v in forecast_vals.items() if k != "time"},
            "_schema_version": "backfill_pilot_v1",
        })
        time.sleep(0.3)

    if not rows:
        print("\nNo rows produced. Nothing written -- check the per-date log above for why.")
        return

    df_out = pd.DataFrame(rows)
    df_out.to_csv(OUTPUT_CSV, index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"\nWrote {len(df_out)} rows to {OUTPUT_CSV}. Review this file before trusting it or scaling up.")


if __name__ == "__main__":
    run_pilot()
