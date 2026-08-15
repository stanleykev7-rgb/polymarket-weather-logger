import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
import pandas as pd
import requests

# 1. CITIES LIST (Expand as needed for active Polymarket cities)
CITIES = {
    "Hong Kong": {"lat": 22.3193, "lon": 114.1694, "slug": "hong-kong"},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503, "slug": "tokyo"},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737, "slug": "shanghai"},
    "Qingdao": {"lat": 36.0671, "lon": 120.3826, "slug": "qingdao"},
    "Seoul": {"lat": 37.5665, "lon": 126.9780, "slug": "seoul"},
    "Guangzhou": {"lat": 23.1291, "lon": 113.2644, "slug": "guangzhou"},
    "Shenzhen": {"lat": 22.5431, "lon": 114.0579, "slug": "shenzhen"},
    "New York": {"lat": 40.7128, "lon": -74.0060, "slug": "new-york"},
    "Chicago": {"lat": 41.8781, "lon": -87.6298, "slug": "chicago"},
    "Miami": {"lat": 25.7617, "lon": -80.1918, "slug": "miami"},
    "London": {"lat": 51.5074, "lon": -0.1278, "slug": "london"},
    "Paris": {"lat": 48.8566, "lon": 2.3522, "slug": "paris"},
    "Ankara": {"lat": 39.9334, "lon": 32.8597, "slug": "ankara"},
    "Buenos Aires": {"lat": -34.6037, "lon": -58.3816, "slug": "buenos-aires"},
}

CSV_FILE = "polymarket_weather_live_log.csv"

# 2. FINAL STRICT SCHEMA ORDERING
CSV_COLUMNS = [
    "timestamp_utc",
    "city",
    "target_date",
    "ecmwf_max_c",
    "gfs_max_c",
    "predicted_bucket",
    "polymarket_price",
    "all_bucket_prices",
    "snapshot_id",
    "lead_time_hours",
    "model_spread_c",
    "abs_model_spread_c",
    "ecmwf_run_utc",
    "gfs_run_utc",
    "market_implied_temp_c",
    "market_vs_ecmwf_c",
    "market_vs_gfs_c",
    "ecmwf_bucket_probability",
    "gfs_bucket_probability",
    "ecmwf_change_c",
    "gfs_change_c",
    "market_implied_change_c",
    "data_quality",
]


def match_temp_to_bucket(temp_c, poly_prices):
  """Maps a decimal temperature to the explicit Polymarket bucket contract without blind assumptions.

  Returns (bucket_label, probability, is_unambiguous).
  """
  if temp_c is None or not poly_prices:
    return None, None, False

  # Method 1: Check for exact integer bucket matching (Standard Polymarket 1°C integer buckets)
  rounded_val = int(round(temp_c))
  exact_key = f"{rounded_val}°C"

  if exact_key in poly_prices:
    return exact_key, poly_prices[exact_key], True

  # Method 2: Evaluate boundary buckets (e.g. "32°C or higher", "25°C or lower")
  for bucket_label, prob in poly_prices.items():
    label_lower = bucket_label.lower()
    numbers = re.findall(r"-?\d+", bucket_label)

    if numbers:
      bound_val = float(numbers[0])
      if ("higher" in label_lower or "above" in label_lower) and temp_c >= bound_val:
        return bucket_label, prob, True
      if ("lower" in label_lower or "below" in label_lower) and temp_c <= bound_val:
        return bucket_label, prob, True

  # If no contract bucket strictly maps to the temperature value:
  return None, None, False


def parse_bucket_midpoint(bucket_str):
  """Extracts numerical midpoint temperature for statistical expected value calculation."""
  numbers = re.findall(r"-?\d+", str(bucket_str))
  if not numbers:
    return None
  val = float(numbers[0])
  if "lower" in bucket_str.lower() or "below" in bucket_str.lower():
    return val - 0.5
  if "higher" in bucket_str.lower() or "above" in bucket_str.lower():
    return val + 0.5
  return val


def compute_market_implied_temp(prices_dict):
  """Calculates probability-weighted mean: sum(Price * BucketTemp) / sum(Prices)."""
  if not prices_dict:
    return None, 0.0

  total_weighted = 0.0
  total_prob = 0.0

  for bucket_label, prob in prices_dict.items():
    if prob is not None and prob > 0:
      midpoint = parse_bucket_midpoint(bucket_label)
      if midpoint is not None:
        total_weighted += midpoint * prob
        total_prob += prob

  if total_prob > 0:
    return round(total_weighted / total_prob, 2), round(total_prob, 4)
  return None, 0.0


def get_weather_forecast(lat, lon):
  """Fetches ECMWF/GFS daily maximum predictions."""
  url = (
      f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
      f"&daily=temperature_2m_max&models=ecmwf_ifs025,gfs_seamless&timezone=UTC"
  )
  res = requests.get(url, timeout=10).json()

  target_date = res["daily"]["time"][1]
  ecmwf_max = res["daily"]["temperature_2m_max_ecmwf_ifs025"][1]
  gfs_max = res["daily"]["temperature_2m_max_gfs_seamless"][1]

  return target_date, ecmwf_max, gfs_max


def parse_event_markets(event_data):
  """Extracts raw outcome prices into a dict."""
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


def get_polymarket_prices(city_name, city_slug, target_date_str):
  """Fetches active Polymarket prices."""
  dt = datetime.strptime(target_date_str, "%Y-%m-%d")
  month_name = dt.strftime("%B").lower()

  event_slug = (
      f"highest-temperature-in-{city_slug}-on-{month_name}-{dt.day}-{dt.year}"
  )
  url_slug = f"https://gamma-api.polymarket.com/events/slug/{event_slug}"

  try:
    res = requests.get(url_slug, timeout=10)
    if res.status_code == 200:
      prices = parse_event_markets(res.json())
      if prices:
        return prices
  except Exception:
    pass

  # Search Fallback
  url_search = "https://gamma-api.polymarket.com/events"
  try:
    res = requests.get(
        url_search,
        params={"active": "true", "closed": "false", "q": city_name},
        timeout=10,
    )
    if res.status_code == 200:
      events = res.json()
      if isinstance(events, list):
        for event in events:
          title = event.get("title", "").lower()
          desc = event.get("description", "").lower()
          if "highest temperature" in title and (
              target_date_str in title or target_date_str in desc
          ):
            prices = parse_event_markets(event)
            if prices:
              return prices
  except Exception:
    pass

  return {}


def load_previous_snapshot():
  """Loads most recent logged snapshot per city for calculating inter-observation deltas."""
  if not os.path.exists(CSV_FILE):
    return {}
  try:
    df = pd.read_csv(CSV_FILE)
    if df.empty or "city" not in df.columns:
      return {}
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
    quality_issues = []

    # 1. Fetch Forecast
    try:
      target_date, ecmwf_t, gfs_t = get_weather_forecast(
          info["lat"], info["lon"]
      )
    except Exception:
      target_date, ecmwf_t, gfs_t = None, None, None
      quality_issues.append("WEATHER_FETCH_FAILED")

    # 2. Fetch Polymarket Prices
    poly_prices = (
        get_polymarket_prices(city_name, info["slug"], target_date)
        if target_date
        else {}
    )
    if not poly_prices:
      quality_issues.append("MISSING_POLYMARKET_PRICES")

    # 3. Lead Time Calculation
    lead_time_hours = None
    if target_date:
      target_midnight_utc = datetime.strptime(
          target_date, "%Y-%m-%d"
      ).replace(tzinfo=timezone.utc)
      lead_time_hours = round(
          (target_midnight_utc - now_dt).total_seconds() / 3600.0, 2
      )

    # 4. Spreads & Strict Contract Bucket Matching
    model_spread_c = None
    abs_model_spread_c = None

    if ecmwf_t is not None and gfs_t is not None:
      model_spread_c = round(ecmwf_t - gfs_t, 2)
      abs_model_spread_c = round(abs(model_spread_c), 2)

    # Match ECMWF
    ecmwf_bucket, ecmwf_bucket_prob, ecmwf_valid = match_temp_to_bucket(
        ecmwf_t, poly_prices
    )
    if ecmwf_t is not None and poly_prices and not ecmwf_valid:
      quality_issues.append("ECMWF_BUCKET_MAPPING_AMBIGUOUS")

    # Match GFS
    gfs_bucket, gfs_bucket_prob, gfs_valid = match_temp_to_bucket(
        gfs_t, poly_prices
    )
    if gfs_t is not None and poly_prices and not gfs_valid:
      quality_issues.append("GFS_BUCKET_MAPPING_AMBIGUOUS")

    predicted_bucket = ecmwf_bucket
    polymarket_price = ecmwf_bucket_prob

    # 5. Market Implied Temperature
    mkt_implied_t, sum_prob = compute_market_implied_temp(poly_prices)
    if poly_prices and sum_prob < 0.50:
      quality_issues.append("LOW_TOTAL_MARKET_PROBABILITY")

    mkt_vs_ecmwf = (
        round(mkt_implied_t - ecmwf_t, 2)
        if (mkt_implied_t is not None and ecmwf_t is not None)
        else None
    )
    mkt_vs_gfs = (
        round(mkt_implied_t - gfs_t, 2)
        if (mkt_implied_t is not None and gfs_t is not None)
        else None
    )

    # 6. Inter-run Deltas
    city_prev = prev_data.get(city_name, {})
    ecmwf_change = (
        round(ecmwf_t - city_prev["ecmwf_max_c"], 2)
        if (
            ecmwf_t is not None
            and pd.notnull(city_prev.get("ecmwf_max_c"))
        )
        else None
    )
    gfs_change = (
        round(gfs_t - city_prev["gfs_max_c"], 2)
        if (gfs_t is not None and pd.notnull(city_prev.get("gfs_max_c")))
        else None
    )
    mkt_change = (
        round(mkt_implied_t - city_prev["market_implied_temp_c"], 2)
        if (
            mkt_implied_t is not None
            and pd.notnull(city_prev.get("market_implied_temp_c"))
        )
        else None
    )

    # 7. Construct Row Record
    all_bucket_json = json.dumps(poly_prices, ensure_ascii=False)
    data_quality = "OK" if not quality_issues else "|".join(quality_issues)

    records.append({
        "timestamp_utc": now_utc_str,
        "city": city_name,
        "target_date": target_date,
        "ecmwf_max_c": ecmwf_t,
        "gfs_max_c": gfs_t,
        "predicted_bucket": predicted_bucket,
        "polymarket_price": polymarket_price,
        "all_bucket_prices": all_bucket_json,
        "snapshot_id": snapshot_id,
        "lead_time_hours": lead_time_hours,
        "model_spread_c": model_spread_c,
        "abs_model_spread_c": abs_model_spread_c,
        "ecmwf_run_utc": None,
        "gfs_run_utc": None,
        "market_implied_temp_c": mkt_implied_t,
        "market_vs_ecmwf_c": mkt_vs_ecmwf,
        "market_vs_gfs_c": mkt_vs_gfs,
        "ecmwf_bucket_probability": ecmwf_bucket_prob,
        "gfs_bucket_probability": gfs_bucket_prob,
        "ecmwf_change_c": ecmwf_change,
        "gfs_change_c": gfs_change,
        "market_implied_change_c": mkt_change,
        "data_quality": data_quality,
    })

  if records:
    df = pd.DataFrame(records, columns=CSV_COLUMNS)
    file_exists = os.path.exists(CSV_FILE)
    df.to_csv(
        CSV_FILE,
        mode="a",
        header=not file_exists,
        index=False,
        encoding="utf-8-sig",
    )
    print(
        f"[{now_utc_str}] Logged snapshot '{snapshot_id}' for {len(records)}"
        " cities."
    )


if __name__ == "__main__":
  log_snapshot()
