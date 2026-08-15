import csv
from datetime import datetime, timezone
import json
import math
import os
import re
import time
import uuid
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from data_utils import compute_lead_time_hours, load_combined_log, resolve_write_target

# 1. CITIES CONFIGURATION
CITIES = {
    "Hong Kong": {
        "lat": 22.3193,
        "lon": 114.1694,
        "tz": "Asia/Hong_Kong",
        "slug_tag": "hong-kong",
        "search_term": "Highest temperature in Hong Kong",
        "unit": "C",
    },
    "Tokyo": {
        "lat": 35.6762,
        "lon": 139.6503,
        "tz": "Asia/Tokyo",
        "slug_tag": "tokyo",
        "search_term": "Highest temperature in Tokyo",
        "unit": "C",
    },
    "Shanghai": {
        "lat": 31.2304,
        "lon": 121.4737,
        "tz": "Asia/Shanghai",
        "slug_tag": "shanghai",
        "search_term": "Highest temperature in Shanghai",
        "unit": "C",
    },
    "Qingdao": {
        "lat": 36.0671,
        "lon": 120.3826,
        "tz": "Asia/Shanghai",
        "slug_tag": "qingdao",
        "search_term": "Highest temperature in Qingdao",
        "unit": "C",
    },
    "Seoul": {
        "lat": 37.5665,
        "lon": 126.9780,
        "tz": "Asia/Seoul",
        "slug_tag": "seoul",
        "search_term": "Highest temperature in Seoul",
        "unit": "C",
    },
    "Guangzhou": {
        "lat": 23.1291,
        "lon": 113.2644,
        "tz": "Asia/Shanghai",
        "slug_tag": "guangzhou",
        "search_term": "Highest temperature in Guangzhou",
        "unit": "C",
    },
    "Shenzhen": {
        "lat": 22.5431,
        "lon": 114.0579,
        "tz": "Asia/Shanghai",
        "slug_tag": "shenzhen",
        "search_term": "Highest temperature in Shenzhen",
        "unit": "C",
    },
    "New York": {
        "lat": 40.7128,
        "lon": -74.0060,
        "tz": "America/New_York",
        "slug_tag": "nyc",
        "search_term": "Highest temperature in NYC",
        "unit": "F",
    },
    "Chicago": {
        "lat": 41.8781,
        "lon": -87.6298,
        "tz": "America/Chicago",
        "slug_tag": "chicago",
        "search_term": "Highest temperature in Chicago",
        "unit": "F",
    },
    "Miami": {
        "lat": 25.7617,
        "lon": -80.1918,
        "tz": "America/New_York",
        "slug_tag": "miami",
        "search_term": "Highest temperature in Miami",
        "unit": "F",
    },
    "London": {
        "lat": 51.5074,
        "lon": -0.1278,
        "tz": "Europe/London",
        "slug_tag": "london",
        "search_term": "Highest temperature in London",
        "unit": "C",
    },
    "Paris": {
        "lat": 48.8566,
        "lon": 2.3522,
        "tz": "Europe/Paris",
        "slug_tag": "paris",
        "search_term": "Highest temperature in Paris",
        "unit": "C",
    },
    "Ankara": {
        "lat": 39.9334,
        "lon": 32.8597,
        "tz": "Europe/Istanbul",
        "slug_tag": "ankara",
        "search_term": "Highest temperature in Ankara",
        "unit": "C",
    },
    "Buenos Aires": {
        "lat": -34.6037,
        "lon": -58.3816,
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
CSV_COLUMNS = [
    "timestamp_utc",
    "city",
    "target_date",
    "ecmwf_max_c",
    "gfs_max_c",
    "predicted_bucket",  # legacy meaning preserved: == ecmwf_bucket
    "polymarket_price",  # legacy meaning preserved: == ecmwf_bucket_probability
    "all_bucket_prices",
    "snapshot_id",
    "lead_time_hours",
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
    "market_modal_bucket",
    "market_modal_bucket_price",
    "ecmwf_change_c",
    "gfs_change_c",
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


def get_weather_forecast(lat, lon, tz, max_retries=3):
  url = (
      f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
      f"&daily=temperature_2m_max&models=ecmwf_ifs025,gfs_seamless&timezone={tz}"
  )
  session = create_resilient_session(retries=max_retries)

  for attempt in range(1, max_retries + 1):
    try:
      res = session.get(url, timeout=10)
      if res.status_code == 200:
        data = res.json()
        if "daily" in data and "time" in data["daily"]:
          forecasts = {}
          for idx, date_str in enumerate(data["daily"]["time"]):
            forecasts[date_str] = {
                "ecmwf": data["daily"]["temperature_2m_max_ecmwf_ifs025"][idx],
                "gfs": data["daily"]["temperature_2m_max_gfs_seamless"][idx],
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


def get_polymarket_prices_multi_date(city_name, city_info, forecast_dates):
  session = create_resilient_session()
  slug_tag = city_info["slug_tag"]

  for target_date_str in forecast_dates:
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    day_num = dt.day

    patterns = [
        f"highest-temperature-in-{slug_tag}-on-{month_name}-{day_num}",
        f"highest-temperature-in-{slug_tag}-on-{month_name}-{day_num}-{dt.year}",
    ]

    for event_slug in patterns:
      url_slug = f"https://gamma-api.polymarket.com/events/slug/{event_slug}"
      try:
        res = session.get(url_slug, timeout=10)
        if res.status_code == 200:
          event = res.json()
          if event and not event.get("closed", False):
            prices = parse_event_markets(event)
            if prices:
              return target_date_str, prices
      except Exception:
        pass

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
                return target_date_str, prices
    except Exception:
      pass

  default_target = (
      forecast_dates[1] if len(forecast_dates) > 1 else forecast_dates[0]
  )
  return default_target, {}


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
  """Loads the most recent row per city across ALL schema versions of
  the log (see data_utils.load_combined_log), so 'previous observation'
  comparisons (ecmwf_change_c etc.) aren't blind to rows written under
  an older or newer schema file.
  """
  try:
    df = load_combined_log(CSV_FILE)
    if df.empty or "city" not in df.columns:
      return {}
    # Sort by timestamp so "last" is chronologically last, not just
    # last-in-file (matters once multiple schema-version files exist).
    if "timestamp_utc" in df.columns:
      df = df.sort_values("timestamp_utc")
    last_records = {}
    for city, group in df.groupby("city"):
      last_row = group.iloc[-1]
      last_records[city] = {
          "ecmwf_max_c": last_row.get("ecmwf_max_c"),
          "gfs_max_c": last_row.get("gfs_max_c"),
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
    quality_issues = []
    unit = info.get("unit", "C")

    # 1. Fetch Local Weather Forecasts (°C)
    try:
      forecasts_by_date = get_weather_forecast(
          info["lat"], info["lon"], info["tz"]
      )
      candidate_dates = sorted(list(forecasts_by_date.keys()))[:3]
    except Exception:
      forecasts_by_date = {}
      candidate_dates = [now_dt.strftime("%Y-%m-%d")]
      quality_issues.append("WEATHER_FETCH_FAILED")

    # 2. Fetch Active Market Prices
    target_date, poly_prices = get_polymarket_prices_multi_date(
        city_name, info, candidate_dates
    )

    day_weather = forecasts_by_date.get(target_date, {})
    ecmwf_t_c = day_weather.get("ecmwf")
    gfs_t_c = day_weather.get("gfs")

    # Convert forecast to native unit (°F for US, °C for others)
    ecmwf_native = c_to_f(ecmwf_t_c) if unit == "F" else ecmwf_t_c
    gfs_native = c_to_f(gfs_t_c) if unit == "F" else gfs_t_c

    if not poly_prices:
      quality_issues.append("MISSING_POLYMARKET_PRICES")

    # 3. Lead Time Calculation (city-LOCAL midnight, not UTC midnight --
    # see data_utils.compute_lead_time_hours / audit finding HIGH-1)
    lead_time_hours = compute_lead_time_hours(now_dt, target_date, info["tz"])

    # 4. Spreads & Bucket Matching
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
    city_prev = prev_data.get(city_name, {})
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
        "predicted_bucket": predicted_bucket,  # legacy meaning: == ecmwf_bucket
        "polymarket_price": polymarket_price,  # legacy meaning: == ecmwf_bucket_probability
        "all_bucket_prices": all_bucket_json,
        "snapshot_id": snapshot_id,
        "lead_time_hours": lead_time_hours,
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
        "market_modal_bucket": market_modal_bucket,
        "market_modal_bucket_price": market_modal_bucket_price,
        "ecmwf_change_c": ecmwf_change,
        "gfs_change_c": gfs_change,
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
