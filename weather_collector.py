import csv
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import time
import uuid
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from data_utils import compute_hours_to_resolution, compute_lead_time_hours, load_combined_log, resolve_write_target

# How many days ahead (from each city's local "today") to check
# Polymarket for an open market, independent of whatever the weather
# forecast API happens to return for that same window (see log_snapshot
# / get_weather_forecast FIX notes, 2026-08). 10 gives real headroom
# beyond typical Polymarket listing patterns without over-querying.
CANDIDATE_DATE_WINDOW_DAYS = 10

# --- NATIONAL MODELS (schema v4, 2026-08) --------------------------------
# City-specific national weather service models, matched to the city each
# was actually built to forecast -- the argument being that a country's
# own model may out-perform a "generalist" global model on its own turf.
# Confirmed live/working identifiers via Open-Meteo's docs pages (checked
# 2026-08): JMA (jma_seamless), UKMO (ukmo_seamless), Météo-France
# (meteofrance_seamless), all requested in the SAME forecast API call as
# ECMWF/GFS/ICON, so no extra network round-trip.
#
# Deliberately NOT included, despite being the "obvious" match:
#   - KMA for Seoul: Open-Meteo's own docs state KMA discontinued their
#     UM-based models in March 2026 and "KMA data updates are currently
#     suspended" while they migrate to a new source. Wiring this up now
#     would silently return stale/no data.
#   - CMA for Shanghai/Qingdao/Guangzhou/Shenzhen: Open-Meteo's docs
#     state CMA's open-data service has been "heavily overloaded...
#     making it nearly impossible to download forecasts reliably."
# Both can be added later once Open-Meteo's own pages stop flagging them
# as degraded -- check https://open-meteo.com/en/docs/kma-api and
# https://open-meteo.com/en/docs/cma-api before re-enabling.
NATIONAL_MODELS = {
    "Tokyo": ("jma_seamless", "JMA"),
    "London": ("ukmo_seamless", "UKMO"),
    "Paris": ("meteofrance_seamless", "MeteoFrance"),
}

# 1. CITIES CONFIGURATION
CITIES = {
    # --- SETTLEMENT-STATION-PRECISE COORDINATES (2026-08) -------------------
    # Every lat/lon below was updated from generic city-center coordinates
    # to the EXACT station Polymarket names as its resolution source,
    # confirmed by reading live Polymarket market rules pages for all 14
    # cities (2026-08). This matters: forecasts for a city-center point can
    # meaningfully diverge from the specific airport/station microclimate
    # that actually settles the market -- coastal vs. urban heat island,
    # elevation, distance from the coast, etc. Two are NOT even in the
    # named city: Seoul settles via Incheon Airport (~50km away) and Buenos
    # Aires via Ezeiza Airport (~35km away). See SCHEMA.md for the full
    # per-city source list and links to the rules pages that confirmed each.
    "Hong Kong": {
        "lat": 22.3020,  # HK Observatory HQ, Tsim Sha Tsui (non-airport; official source is HKO directly, not Wunderground)
        "lon": 114.1740,
        "tz": "Asia/Hong_Kong",
        "slug_tag": "hong-kong",
        "search_term": "Highest temperature in Hong Kong",
        "unit": "C",
    },
    "Tokyo": {
        "lat": 35.5494,  # Haneda Airport (RJTT)
        "lon": 139.7798,
        "tz": "Asia/Tokyo",
        "slug_tag": "tokyo",
        "search_term": "Highest temperature in Tokyo",
        "unit": "C",
    },
    "Shanghai": {
        "lat": 31.1443,  # Pudong Intl Airport (ZSPD)
        "lon": 121.8083,
        "tz": "Asia/Shanghai",
        "slug_tag": "shanghai",
        "search_term": "Highest temperature in Shanghai",
        "unit": "C",
    },
    "Qingdao": {
        "lat": 36.3620,  # Jiaodong Intl Airport (ZSQD) -- opened 2021, ~39km from city center; replaced the older Liuting airport
        "lon": 120.0882,
        "tz": "Asia/Shanghai",
        "slug_tag": "qingdao",
        "search_term": "Highest temperature in Qingdao",
        "unit": "C",
    },
    "Seoul": {
        "lat": 37.4602,  # Incheon Intl Airport (RKSI) -- NOT in Seoul; a separate city ~50km away. This is Polymarket's actual named source.
        "lon": 126.4407,
        "tz": "Asia/Seoul",
        "slug_tag": "seoul",
        "search_term": "Highest temperature in Seoul",
        "unit": "C",
    },
    "Guangzhou": {
        "lat": 23.3924,  # Baiyun Intl Airport (ZGGG)
        "lon": 113.2988,
        "tz": "Asia/Shanghai",
        "slug_tag": "guangzhou",
        "search_term": "Highest temperature in Guangzhou",
        "unit": "C",
    },
    "Shenzhen": {
        "lat": 22.6393,  # Bao'an Intl Airport (ZGSZ)
        "lon": 113.8107,
        "tz": "Asia/Shanghai",
        "slug_tag": "shenzhen",
        "search_term": "Highest temperature in Shenzhen",
        "unit": "C",
    },
    "New York": {
        "lat": 40.7769,  # LaGuardia Airport (KLGA) -- not JFK, not Manhattan
        "lon": -73.8740,
        "tz": "America/New_York",
        "slug_tag": "nyc",
        "search_term": "Highest temperature in NYC",
        "unit": "F",
    },
    "Chicago": {
        "lat": 41.9742,  # O'Hare Intl Airport (KORD)
        "lon": -87.9073,
        "tz": "America/Chicago",
        "slug_tag": "chicago",
        "search_term": "Highest temperature in Chicago",
        "unit": "F",
    },
    "Miami": {
        "lat": 25.7959,  # Miami Intl Airport (KMIA)
        "lon": -80.2870,
        "tz": "America/New_York",
        "slug_tag": "miami",
        "search_term": "Highest temperature in Miami",
        "unit": "F",
    },
    "London": {
        "lat": 51.5053,  # London City Airport (EGLC) -- not Heathrow
        "lon": 0.0553,
        "tz": "Europe/London",
        "slug_tag": "london",
        "search_term": "Highest temperature in London",
        "unit": "C",
    },
    "Paris": {
        "lat": 48.9694,  # Le Bourget Airport (LFPB) -- not Charles de Gaulle
        "lon": 2.4414,
        "tz": "Europe/Paris",
        "slug_tag": "paris",
        "search_term": "Highest temperature in Paris",
        "unit": "C",
    },
    "Ankara": {
        "lat": 40.1281,  # Esenboğa Intl Airport (LTAC)
        "lon": 32.9951,
        "tz": "Europe/Istanbul",
        "slug_tag": "ankara",
        "search_term": "Highest temperature in Ankara",
        "unit": "C",
    },
    "Buenos Aires": {
        "lat": -34.8222,  # Ministro Pistarini/Ezeiza Intl Airport (SAEZ) -- NOT in Buenos Aires proper; ~35km southwest
        "lon": -58.5358,
        "tz": "America/Argentina/Buenos_Aires",
        "slug_tag": "buenos-aires",
        "search_term": "Highest temperature in Buenos Aires",
        "unit": "C",
    },
}

CSV_FILE = "polymarket_weather_live_log.csv"

# --- SCHEMA NOTE (2026-08 audit) ---------------------------------------
# `predicted_bucket` / `polymarket_price` are PRESERVED with their
# original (as-implemented) historical meaning for continuity with every
# already-logged row: they are the Polymarket bucket that matches the
# ECMWF forecast, and that bucket's market price. They are NOT the
# market's modal (highest-probability) bucket, despite the name.
# Renaming or repurposing them here would silently break every existing
# row's interpretation with no way to tell old vs. new meaning apart.
#
# Instead, this schema version ADDS clearly-named columns that were
# missing before:
#   - ecmwf_bucket / ecmwf_bucket_probability : explicit, unambiguous
#     duplicate of predicted_bucket / polymarket_price under an honest
#     name. (ecmwf_bucket_probability already existed; ecmwf_bucket did
#     not and is new.)
#   - gfs_bucket / gfs_bucket_probability : the GFS-forecast-matched
#     bucket and its price. gfs_bucket is new; gfs_bucket_probability
#     already existed but previously had no accompanying label.
#   - market_modal_bucket / market_modal_bucket_price : the market's
#     actual current favorite (highest-priced) bucket -- this is what
#     "predicted_bucket" was originally *intended* to mean, now
#     implemented correctly under its own name.
#
# Because this column set differs from the original file's header,
# these rows are written to a NEW physical file
# (polymarket_weather_live_log_v2.csv) rather than appended under the
# old header. See data_utils.resolve_write_target /
# data_utils.load_combined_log.
#
# --- SCHEMA v3 (2026-08, later same day) --------------------------------
# Added ICON (DWD) as a third forecast model alongside ECMWF/GFS:
# icon_max_c, icon_bucket, icon_bucket_probability, icon_change_c.
# Same column-count-changed-so-route-to-a-new-file mechanism applies --
# this will land in polymarket_weather_live_log_v3.csv once the first
# row with this column set is written.
# --- SCHEMA v4 (2026-08, later same day) --------------------------------
# Added city-matched national model support (JMA/Tokyo, UKMO/London,
# Météo-France/Paris -- see NATIONAL_MODELS) plus hours_to_resolution,
# a second lead-time framing measured to the END of the target local day
# (when the outcome is actually decided) rather than its start. See
# data_utils.compute_hours_to_resolution for why this is a distinct,
# deliberately separate field from lead_time_hours.
CSV_COLUMNS = [
    "timestamp_utc",
    "city",
    "target_date",
    "ecmwf_max_c",
    "gfs_max_c",
    "icon_max_c",
    "national_model_name",
    "national_model_max_c",
    "predicted_bucket",  # legacy meaning preserved: == ecmwf_bucket
    "polymarket_price",  # legacy meaning preserved: == ecmwf_bucket_probability
    "all_bucket_prices",
    "snapshot_id",
    "lead_time_hours",
    "hours_to_resolution",
    "model_spread_c",
    "abs_model_spread_c",
    "ecmwf_run_utc",
    "gfs_run_utc",
    "market_implied_temp_c",
    "market_implied_temp_used_open_bucket_approx",
    "market_vs_ecmwf_c",
    "market_vs_gfs_c",
    "ecmwf_bucket",
    "ecmwf_bucket_probability",
    "gfs_bucket",
    "gfs_bucket_probability",
    "icon_bucket",
    "icon_bucket_probability",
    "national_model_bucket",
    "national_model_bucket_probability",
    "market_modal_bucket",
    "market_modal_bucket_price",
    "ecmwf_change_c",
    "gfs_change_c",
    "icon_change_c",
    "national_model_change_c",
    "market_implied_change_c",
    "data_quality",
]


def c_to_f(c_temp):
  """Converts Celsius to Fahrenheit."""
  return (c_temp * 9 / 5) + 32 if c_temp is not None else None


def f_to_c(f_temp):
  """Converts Fahrenheit to Celsius."""
  return (f_temp - 32) * 5 / 9 if f_temp is not None else None


def create_resilient_session(retries=3, backoff_factor=1):
  session = requests.Session()
  retry_strategy = Retry(
      total=retries,
      backoff_factor=backoff_factor,
      status_forcelist=[429, 500, 502, 503, 504],
      raise_on_status=False,
  )
  adapter = HTTPAdapter(max_retries=retry_strategy)
  session.mount("https://", adapter)
  session.mount("http://", adapter)
  return session


def get_weather_forecast(lat, lon, tz, max_retries=3, national_model_id=None, forecast_days=10):
  # Third model (ICON) added 2026-08: DWD ICON ("dwd_icon_seamless" --
  # DWD's global ICON model, ~11km, blended with higher-resolution ICON
  # EU/D2 near Europe where available; sometimes referred to as
  # "ICON13" from its older 13km global resolution).
  #
  # Optional 4th model (national_model_id, schema v4): a city-matched
  # national weather service model (JMA/UKMO/Météo-France -- see
  # NATIONAL_MODELS). Passed as an extra comma-joined model id so it's
  # requested in this SAME API call, no extra network round-trip.
  #
  # FIX (2026-08): forecast_days defaults to Open-Meteo's own default of
  # 7 if not specified, which was silently capping how many future dates
  # we could even consider checking Polymarket for. Now explicitly
  # requests 10 (Open-Meteo supports up to 16) so a Polymarket market
  # opened further out than a week is still checkable. This is now a
  # secondary safety margin, not the primary mechanism -- see
  # log_snapshot(): candidate_dates is generated independently by date
  # arithmetic, not derived from whatever this response happens to
  # include, so a gap here degrades gracefully (missing forecast values,
  # flagged in data_quality) instead of silently skipping the date
  # entirely.
  models = "ecmwf_ifs025,gfs_seamless,dwd_icon_seamless"
  if national_model_id:
    models += f",{national_model_id}"

  url = (
      f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
      f"&daily=temperature_2m_max&models={models}&timezone={tz}&forecast_days={forecast_days}"
  )
  session = create_resilient_session(retries=max_retries)

  for attempt in range(1, max_retries + 1):
    try:
      res = session.get(url, timeout=10)
      if res.status_code == 200:
        data = res.json()
        if "daily" in data and "time" in data["daily"]:
          forecasts = {}
          daily = data["daily"]
          n = len(daily["time"])
          national_key = f"temperature_2m_max_{national_model_id}" if national_model_id else None
          for idx, date_str in enumerate(daily["time"]):
            forecasts[date_str] = {
                "ecmwf": daily["temperature_2m_max_ecmwf_ifs025"][idx],
                "gfs": daily["temperature_2m_max_gfs_seamless"][idx],
                # .get(...) with a fallback list, not a plain index,
                # so a temporary gap in a model's response (an issue
                # this API has occasionally had for some
                # locations/models) degrades to a missing value
                # instead of crashing the whole snapshot.
                "icon": daily.get("temperature_2m_max_dwd_icon_seamless", [None] * n)[idx],
                "national": (
                    daily.get(national_key, [None] * n)[idx] if national_key else None
                ),
            }
          return forecasts

      time.sleep(attempt * 1.5)
    except (requests.RequestException, KeyError, IndexError) as e:
      if attempt == max_retries:
        raise e
      time.sleep(attempt * 1.5)

  raise RuntimeError(
      f"Failed to fetch weather data for ({lat}, {lon}) after {max_retries}"
      " attempts."
  )


def parse_event_markets(event_data):
  bucket_prices = {}
  if not event_data or "markets" not in event_data:
    return bucket_prices

  for market in event_data.get("markets", []):
    bucket = market.get("groupItemTitle") or market.get("question")
    raw_prices = market.get("outcomePrices")

    if bucket and raw_prices:
      prices = (
          json.loads(raw_prices)
          if isinstance(raw_prices, str)
          else raw_prices
      )
      if prices:
        try:
          bucket_prices[bucket] = float(prices[0])
        except (ValueError, TypeError):
          continue
  return bucket_prices


def get_polymarket_prices_multi_date(city_name, city_info, forecast_dates, verbose=True):
  """Returns a list of (target_date_str, prices_dict) for candidate
  dates that have an active, priced market -- not just the first one
  found.

  FIX (2026-08, first pass): previously this returned on the first
  match and stopped, so a single polling cycle only ever logged the
  single soonest open market for a city and silently never even
  checked later dates.

  FIX (2026-08, third pass): widening the candidate-date window to 10
  days (see CANDIDATE_DATE_WINDOW_DAYS) meant checking every one of
  those dates against Polymarket unconditionally -- up to ~30 requests
  per city per hour (2 slug patterns + a search fallback, times 10
  dates), a ~10x jump from before. That's a real, plausible cause of
  silent rate-limiting on the LATER dates checked within a city's loop
  (earlier/nearer dates would tend to succeed, later/farther ones
  quietly fail) -- which would produce exactly the reported symptom of
  near-term dates showing up but a genuinely-open further-out market
  not appearing.

  Mitigation: stop checking further dates once we've found at least one
  match AND then hit 2 consecutive dates with no market -- Polymarket
  opens a contiguous rolling window of dates, not scattered gaps, so
  this reflects reality (we've run past the open window) rather than
  silently giving up. If we haven't found any match yet, we keep
  checking the full window (there could be a temporary gap before the
  window opens for that city). Also adds a small delay between
  per-date checks to reduce request bursts.

  Set verbose=True (default) to print a per-date diagnostic line to
  stdout -- this shows up in the GitHub Actions log and is the fastest
  way to tell "fix not deployed" apart from "rate limited" apart from
  "Polymarket genuinely doesn't have this market open yet" the next
  time this is reported.
  """
  session = create_resilient_session()
  slug_tag = city_info["slug_tag"]
  found = []
  consecutive_misses_after_hit = 0

  for target_date_str in forecast_dates:
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    day_num = dt.day

    patterns = [
        f"highest-temperature-in-{slug_tag}-on-{month_name}-{day_num}",
        f"highest-temperature-in-{slug_tag}-on-{month_name}-{day_num}-{dt.year}",
    ]

    matched_prices = None
    last_status = None
    for event_slug in patterns:
      url_slug = f"https://gamma-api.polymarket.com/events/slug/{event_slug}"
      try:
        res = session.get(url_slug, timeout=10)
        last_status = res.status_code
        if res.status_code == 200:
          event = res.json()
          if event and not event.get("closed", False):
            prices = parse_event_markets(event)
            if prices:
              matched_prices = prices
              break
      except Exception as e:
        last_status = f"error: {e}"

    if matched_prices is None:
      search_query = city_info.get(
          "search_term", f"Highest temperature in {city_name}"
      )
      url_search = "https://gamma-api.polymarket.com/events"
      try:
        res = session.get(
            url_search,
            params={"active": "true", "closed": "false", "q": search_query},
            timeout=10,
        )
        last_status = res.status_code
        if res.status_code == 200:
          events = res.json()
          if isinstance(events, list):
            for event in events:
              title = event.get("title", "").lower()
              if (
                  "temperature" in title
                  and month_name in title
                  and str(day_num) in title
              ):
                prices = parse_event_markets(event)
                if prices:
                  matched_prices = prices
                  break
      except Exception as e:
        last_status = f"error: {e}"

    if matched_prices:
      found.append((target_date_str, matched_prices))
      consecutive_misses_after_hit = 0
      if verbose:
        print(f"    [{city_name}] {target_date_str}: MATCH ({len(matched_prices)} buckets)")
    else:
      if verbose:
        print(f"    [{city_name}] {target_date_str}: no market (last HTTP status: {last_status})")
      if found:
        consecutive_misses_after_hit += 1
        if consecutive_misses_after_hit >= 2:
          if verbose:
            print(f"    [{city_name}] stopping early after 2 consecutive misses past the open window")
          break

    time.sleep(0.15)  # small gap between per-date checks to reduce burst load

  return found


def parse_bucket_midpoint(bucket_str):
  """Parses range buckets into numerical midpoints.

  Returns (midpoint, is_open_ended_approx). Open-ended buckets ("35°C or
  higher", "25°C or lower") have no true midpoint -- the +/-0.5 used
  here is a documented APPROXIMATION, not a value derived from the
  actual contract, and callers should propagate the flag rather than
  silently trusting it as exact.
  """
  if not bucket_str:
    return None, False

  s = str(bucket_str).strip()

  range_match = re.search(
      r"(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)", s
  )
  if range_match:
    low = float(range_match.group(1))
    high = float(range_match.group(2))
    return (low + high) / 2.0, False

  nums = re.findall(r"-?\d+(?:\.\d+)?", s)
  if not nums:
    return None, False

  val = float(nums[0])
  s_lower = s.lower()
  if "lower" in s_lower or "below" in s_lower or "under" in s_lower:
    return val - 0.5, True
  if "higher" in s_lower or "above" in s_lower or "over" in s_lower:
    return val + 0.5, True

  return val, False


def match_temp_to_bucket(temp_native, poly_prices):
  """Matches native unit temperature to market buckets with exact bounds."""
  if temp_native is None or not poly_prices:
    return None, None, False

  rounded_val = int(round(temp_native))

  for bucket_label, prob in poly_prices.items():
    lbl = str(bucket_label).strip()
    lbl_lower = lbl.lower()

    # 1. Exact integer degree match (e.g., "30°C", "84°F") using regex bounds
    if re.search(r"\b" + str(rounded_val) + r"\s*°", lbl):
      return bucket_label, prob, True

    # 2. Range match (e.g., "82-83°F", "84-85°F")
    range_match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)", lbl
    )
    if range_match:
      low = float(range_match.group(1))
      high = float(range_match.group(2))

      if (
          (low - 0.5) <= temp_native <= (high + 0.5)
      ) or low <= rounded_val <= high:
        return bucket_label, prob, True
      continue

    # 3. Single bounded matches (e.g., "84°F or higher", "75°F or below")
    nums = re.findall(r"-?\d+(?:\.\d+)?", lbl)
    if nums:
      bound_val = float(nums[0])
      if (
          "higher" in lbl_lower or "above" in lbl_lower or "over" in lbl_lower
      ) and temp_native >= (bound_val - 0.5):
        return bucket_label, prob, True
      if (
          "lower" in lbl_lower or "below" in lbl_lower or "under" in lbl_lower
      ) and temp_native <= (bound_val + 0.5):
        return bucket_label, prob, True

  return None, None, False


def compute_market_implied_temp(prices_dict):
  """Calculates probability-weighted average temperature.

  Returns (implied_temp, total_prob, used_open_bucket_approx). The third
  value is True if any bucket that contributed non-trivial weight was an
  open-ended bucket ("X or higher/lower"), whose midpoint is a
  documented approximation rather than a value defined by the contract.
  """
  if not prices_dict:
    return None, 0.0, False

  total_weighted = 0.0
  total_prob = 0.0
  used_approx = False

  for bucket_label, prob in prices_dict.items():
    if prob is not None and prob > 0:
      midpoint, is_approx = parse_bucket_midpoint(bucket_label)
      if midpoint is not None:
        total_weighted += midpoint * prob
        total_prob += prob
        if is_approx:
          used_approx = True

  if total_prob > 0:
    return round(total_weighted / total_prob, 2), round(total_prob, 4), used_approx
  return None, 0.0, False


def load_previous_snapshot():
  """Loads the most recent row per (city, target_date) pair across ALL
  schema versions of the log (see data_utils.load_combined_log), so
  'previous observation' comparisons (ecmwf_change_c etc.) aren't blind
  to rows written under an older or newer schema file.

  FIX (2026-08): previously keyed by city ALONE. Now that a single
  polling cycle can log multiple simultaneously-open target dates for
  the same city (see get_polymarket_prices_multi_date fix), keying by
  city alone meant "change vs previous" could silently compare two
  DIFFERENT target dates' forecasts against each other (e.g. this
  cycle's Aug 18 forecast minus last cycle's Aug 16 forecast) rather
  than the same day's forecast an hour apart. Keying by (city,
  target_date) makes this comparison mean what it's supposed to mean.
  """
  try:
    df = load_combined_log(CSV_FILE)
    if df.empty or "city" not in df.columns or "target_date" not in df.columns:
      return {}
    if "timestamp_utc" in df.columns:
      df = df.sort_values("timestamp_utc")
    last_records = {}
    for (city, target_date), group in df.groupby(["city", "target_date"]):
      last_row = group.iloc[-1]
      last_records[(city, target_date)] = {
          "ecmwf_max_c": last_row.get("ecmwf_max_c"),
          "gfs_max_c": last_row.get("gfs_max_c"),
          "icon_max_c": last_row.get("icon_max_c"),
          "national_model_max_c": last_row.get("national_model_max_c"),
          "market_implied_temp_c": last_row.get("market_implied_temp_c"),
      }
    return last_records
  except Exception:
    return {}


def log_snapshot():
  now_dt = datetime.now(timezone.utc)
  now_utc_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
  snapshot_id = str(uuid.uuid4())[:8]

  prev_data = load_previous_snapshot()
  records = []

  for city_name, info in CITIES.items():
    time.sleep(0.3)
    base_quality_issues = []
    unit = info.get("unit", "C")

    # 1. Generate candidate target dates INDEPENDENTLY of the forecast
    # response (FIX 2026-08, second pass): previously candidate_dates
    # was derived from forecasts_by_date.keys(), which meant our
    # Polymarket search window was silently capped by whatever
    # Open-Meteo's response happened to include (its own default is 7
    # days if forecast_days isn't specified -- confirmed via Open-Meteo's
    # docs). If a market was open further out than that, or a transient
    # gap shortened the response, we'd never even attempt to check
    # Polymarket for it. Now it's plain date arithmetic in the city's
    # LOCAL timezone, so the Polymarket search window is never coupled
    # to forecast API response quirks.
    today_local = now_dt.astimezone(ZoneInfo(info["tz"])).date()
    candidate_dates = [
        (today_local + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(CANDIDATE_DATE_WINDOW_DAYS)
    ]
    print(f"[{city_name}] checking candidate dates {candidate_dates[0]} .. {candidate_dates[-1]} ({info['tz']})")

    # 2. Fetch Local Weather Forecasts (°C). forecast_days is requested
    # generously (see get_weather_forecast) so it should cover the full
    # candidate_dates window above -- but even if a specific date is
    # missing from this response, that date is still checked against
    # Polymarket below (just with forecast values coming back None,
    # flagged via *_FORECAST_MISSING in data_quality, rather than the
    # date being silently skipped).
    national_model_id, national_model_name = NATIONAL_MODELS.get(city_name, (None, None))
    try:
      forecasts_by_date = get_weather_forecast(
          info["lat"], info["lon"], info["tz"], national_model_id=national_model_id,
          forecast_days=CANDIDATE_DATE_WINDOW_DAYS + 2,
      )
    except Exception:
      forecasts_by_date = {}
      base_quality_issues.append("WEATHER_FETCH_FAILED")

    # 3. Fetch ALL Active Market Prices for every candidate date (FIX
    # 2026-08: previously stopped at the first match, so simultaneously
    # open markets for later dates were never logged at all -- see
    # get_polymarket_prices_multi_date docstring).
    matches = get_polymarket_prices_multi_date(city_name, info, candidate_dates)

    if not matches:
      # No active market found for ANY candidate date this cycle --
      # still log one row (with empty prices) so the absence is visible
      # in data_quality, instead of silently producing nothing for
      # this city.
      default_target = (
          candidate_dates[1] if len(candidate_dates) > 1 else candidate_dates[0]
      )
      matches = [(default_target, {})]

    for target_date, poly_prices in matches:
      quality_issues = list(base_quality_issues)
      if target_date not in forecasts_by_date:
        quality_issues.append("FORECAST_MISSING_FOR_TARGET_DATE")

      day_weather = forecasts_by_date.get(target_date, {})
      ecmwf_t_c = day_weather.get("ecmwf")
      gfs_t_c = day_weather.get("gfs")
      icon_t_c = day_weather.get("icon")
      national_t_c = day_weather.get("national")

      # Convert forecast to native unit (°F for US, °C for others)
      ecmwf_native = c_to_f(ecmwf_t_c) if unit == "F" else ecmwf_t_c
      gfs_native = c_to_f(gfs_t_c) if unit == "F" else gfs_t_c
      icon_native = c_to_f(icon_t_c) if unit == "F" else icon_t_c
      national_native = c_to_f(national_t_c) if unit == "F" else national_t_c

      if not poly_prices:
        quality_issues.append("MISSING_POLYMARKET_PRICES")

      # 3. Lead Time Calculation (city-LOCAL midnight, not UTC midnight --
      # see data_utils.compute_lead_time_hours / audit finding HIGH-1).
      # Two DISTINCT framings, deliberately both kept -- see the
      # docstrings on each in data_utils.py:
      #   lead_time_hours: hours to the START of the target local day
      #     (standard NWP verification convention).
      #   hours_to_resolution: hours to the END of the target local day
      #     (when the market's outcome is actually decided).
      lead_time_hours = compute_lead_time_hours(now_dt, target_date, info["tz"])
      hours_to_resolution = compute_hours_to_resolution(now_dt, target_date, info["tz"])

      # 4. Spreads & Bucket Matching
      # NOTE: model_spread_c / abs_model_spread_c stay strictly ECMWF vs
      # GFS (unchanged meaning, for continuity with every existing row).
      # ICON/national model don't get their own spread field yet -- their
      # bucket/hit-rate comparison below is enough to evaluate them
      # independently.
      model_spread_c = None
      abs_model_spread_c = None
      if ecmwf_t_c is not None and gfs_t_c is not None:
        model_spread_c = round(ecmwf_t_c - gfs_t_c, 2)
        abs_model_spread_c = round(abs(model_spread_c), 2)

      ecmwf_bucket, ecmwf_bucket_prob, ecmwf_valid = match_temp_to_bucket(
          ecmwf_native, poly_prices
      )
      if ecmwf_t_c is not None and poly_prices and not ecmwf_valid:
        quality_issues.append("ECMWF_BUCKET_MAPPING_AMBIGUOUS")

      gfs_bucket, gfs_bucket_prob, gfs_valid = match_temp_to_bucket(
          gfs_native, poly_prices
      )
      if gfs_t_c is not None and poly_prices and not gfs_valid:
        quality_issues.append("GFS_BUCKET_MAPPING_AMBIGUOUS")

      icon_bucket, icon_bucket_prob, icon_valid = match_temp_to_bucket(
          icon_native, poly_prices
      )
      if icon_t_c is not None and poly_prices and not icon_valid:
        quality_issues.append("ICON_BUCKET_MAPPING_AMBIGUOUS")
      if icon_t_c is None:
        quality_issues.append("ICON_FORECAST_MISSING")

      # National model bucket matching -- only meaningful for cities with
      # a matched model (see NATIONAL_MODELS). No "missing" quality flag
      # here for the other 11 cities, since not having one is expected,
      # not an error.
      national_bucket, national_bucket_prob, national_valid = match_temp_to_bucket(
          national_native, poly_prices
      )
      if national_model_id and national_t_c is not None and poly_prices and not national_valid:
        quality_issues.append("NATIONAL_MODEL_BUCKET_MAPPING_AMBIGUOUS")
      if national_model_id and national_t_c is None:
        quality_issues.append("NATIONAL_MODEL_FORECAST_MISSING")

      # NOTE (schema v2): predicted_bucket/polymarket_price keep their
      # ORIGINAL as-implemented meaning (ECMWF-matched bucket) for
      # continuity with every historical row. See CSV_COLUMNS comment.
      predicted_bucket = ecmwf_bucket
      polymarket_price = ecmwf_bucket_prob

      # market_modal_bucket: the market's ACTUAL current favorite bucket
      # (highest priced), independent of either model. This is what
      # "predicted_bucket" was originally intended to mean; it now has
      # its own honestly-named field instead of overloading an existing
      # column.
      market_modal_bucket = None
      market_modal_bucket_price = None
      if poly_prices:
        market_modal_bucket = max(poly_prices, key=poly_prices.get)
        market_modal_bucket_price = poly_prices[market_modal_bucket]

      # 5. Market Implied Temperature (Standardized to °C for CSV)
      mkt_implied_native, sum_prob, implied_used_approx = (
          compute_market_implied_temp(poly_prices)
      )
      mkt_implied_c = (
          f_to_c(mkt_implied_native)
          if (unit == "F" and mkt_implied_native is not None)
          else mkt_implied_native
      )
      if mkt_implied_c is not None:
        mkt_implied_c = round(mkt_implied_c, 2)

      if poly_prices and sum_prob < 0.50:
        quality_issues.append("LOW_TOTAL_MARKET_PROBABILITY")

      mkt_vs_ecmwf = (
          round(mkt_implied_c - ecmwf_t_c, 2)
          if (mkt_implied_c is not None and ecmwf_t_c is not None)
          else None
      )
      mkt_vs_gfs = (
          round(mkt_implied_c - gfs_t_c, 2)
          if (mkt_implied_c is not None and gfs_t_c is not None)
          else None
      )

      # 6. Inter-run Deltas
      # FIX (2026-08): keyed by (city, target_date), not city alone --
      # see load_previous_snapshot docstring for why. Without this, a
      # city with multiple simultaneously-open target dates could have
      # its "change vs previous" computed against a DIFFERENT date's
      # forecast.
      city_prev = prev_data.get((city_name, target_date), {})
      ecmwf_change = (
          round(ecmwf_t_c - city_prev["ecmwf_max_c"], 2)
          if (
              ecmwf_t_c is not None
              and pd.notnull(city_prev.get("ecmwf_max_c"))
          )
          else None
      )
      gfs_change = (
          round(gfs_t_c - city_prev["gfs_max_c"], 2)
          if (gfs_t_c is not None and pd.notnull(city_prev.get("gfs_max_c")))
          else None
      )
      icon_change = (
          round(icon_t_c - city_prev["icon_max_c"], 2)
          if (icon_t_c is not None and pd.notnull(city_prev.get("icon_max_c")))
          else None
      )
      national_change = (
          round(national_t_c - city_prev["national_model_max_c"], 2)
          if (national_t_c is not None and pd.notnull(city_prev.get("national_model_max_c")))
          else None
      )
      mkt_change = (
          round(mkt_implied_c - city_prev["market_implied_temp_c"], 2)
          if (
              mkt_implied_c is not None
              and pd.notnull(city_prev.get("market_implied_temp_c"))
          )
          else None
      )

      if implied_used_approx:
        quality_issues.append("IMPLIED_TEMP_USED_OPEN_BUCKET_APPROX")
      if poly_prices and market_modal_bucket is None:
        quality_issues.append("MODAL_BUCKET_UNRESOLVED")

      # 7. Construct Record
      all_bucket_json = json.dumps(poly_prices, ensure_ascii=False)
      data_quality = "OK" if not quality_issues else "|".join(quality_issues)

      records.append({
          "timestamp_utc": now_utc_str,
          "city": city_name,
          "target_date": target_date,
          "ecmwf_max_c": ecmwf_t_c,
          "gfs_max_c": gfs_t_c,
          "icon_max_c": icon_t_c,
          "national_model_name": national_model_name,
          "national_model_max_c": national_t_c,
          "predicted_bucket": predicted_bucket,  # legacy meaning: == ecmwf_bucket
          "polymarket_price": polymarket_price,  # legacy meaning: == ecmwf_bucket_probability
          "all_bucket_prices": all_bucket_json,
          "snapshot_id": snapshot_id,
          "lead_time_hours": lead_time_hours,
          "hours_to_resolution": hours_to_resolution,
          "model_spread_c": model_spread_c,
          "abs_model_spread_c": abs_model_spread_c,
          "ecmwf_run_utc": None,
          "gfs_run_utc": None,
          "market_implied_temp_c": mkt_implied_c,
          "market_implied_temp_used_open_bucket_approx": implied_used_approx,
          "market_vs_ecmwf_c": mkt_vs_ecmwf,
          "market_vs_gfs_c": mkt_vs_gfs,
          "ecmwf_bucket": ecmwf_bucket,
          "ecmwf_bucket_probability": ecmwf_bucket_prob,
          "gfs_bucket": gfs_bucket,
          "gfs_bucket_probability": gfs_bucket_prob,
          "icon_bucket": icon_bucket,
          "icon_bucket_probability": icon_bucket_prob,
          "national_model_bucket": national_bucket,
          "national_model_bucket_probability": national_bucket_prob,
          "market_modal_bucket": market_modal_bucket,
          "market_modal_bucket_price": market_modal_bucket_price,
          "ecmwf_change_c": ecmwf_change,
          "gfs_change_c": gfs_change,
          "icon_change_c": icon_change,
          "national_model_change_c": national_change,
          "market_implied_change_c": mkt_change,
          "data_quality": data_quality,
      })

  if records:
    df = pd.DataFrame(records, columns=CSV_COLUMNS)

    # Schema-safe write: if the column set differs from whatever file
    # currently holds the latest schema version, this routes to a NEW
    # file (e.g. polymarket_weather_live_log_v2.csv) instead of
    # appending mismatched columns under a stale header. Historical
    # files are never touched. See data_utils.resolve_write_target.
    write_path, need_header = resolve_write_target(CSV_FILE, CSV_COLUMNS)

    # STRICT CSV EXPORT FIX: Enforce quote minimalism & escape characters
    df.to_csv(
        write_path,
        mode="a",
        header=need_header,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_MINIMAL,
        escapechar="\\",
    )
    if write_path != CSV_FILE:
      print(
          f"[{now_utc_str}] Schema change detected -- new rows written to"
          f" '{write_path}' (old file '{CSV_FILE}' left untouched)."
      )
    print(
        f"[{now_utc_str}] Logged snapshot '{snapshot_id}' for {len(records)}"
        " cities."
    )


if __name__ == "__main__":
  log_snapshot()
