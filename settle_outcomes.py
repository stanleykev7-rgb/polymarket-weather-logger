import csv
from datetime import datetime
import json
import os
import re
import time
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from data_utils import load_combined_log
from official_settlement_sources import fetch_official_actual_max_c

INPUT_CSV = "polymarket_weather_live_log.csv"
EVALUATED_CSV = "polymarket_weather_evaluated.csv"

# --- SETTLEMENT SOURCE NOTE (2026-08 audit, updated) --------------------
# Confirmed by reading live Polymarket market rules pages: the official
# resolution source per city is a named station feed -- Wunderground
# (specific airport station per city) for most cities, and the Hong Kong
# Observatory's own daily extract for Hong Kong specifically.
#
# As of this update, REAL official sources are wired in for 4 of 14
# cities (see official_settlement_sources.py):
#   - New York (KLGA), Chicago (KORD), Miami (KMIA) via NOAA/NWS
#     api.weather.gov -- the same underlying ASOS/METAR feed Wunderground
#     displays for these airport stations.
#   - Hong Kong via HKO's own public open data API.
#
# The remaining 10 cities have no verified non-Wunderground public
# equivalent yet, and continue to use the Open-Meteo Archive reanalysis
# proxy. Every row's `settlement_source` column says exactly which path
# was used, and `actual_max_c_openmeteo_proxy` is ALWAYS computed
# regardless (even for the 4 official cities) so you can cross-check the
# proxy's accuracy against real data as it accumulates.
CITIES = {
    # Coordinates updated 2026-08 to match Polymarket's exact named
    # settlement station (not city-center) -- see the matching CITIES
    # dict in weather_collector.py for the full rationale and per-city
    # source notes. Kept in sync with that dict intentionally.
    "Hong Kong": {"lat": 22.3020, "lon": 114.1740, "unit": "C", "tz": "Asia/Hong_Kong"},
    "Tokyo": {"lat": 35.5494, "lon": 139.7798, "unit": "C", "tz": "Asia/Tokyo"},
    "Shanghai": {"lat": 31.1443, "lon": 121.8083, "unit": "C", "tz": "Asia/Shanghai"},
    "Qingdao": {"lat": 36.3620, "lon": 120.0882, "unit": "C", "tz": "Asia/Shanghai"},
    "Seoul": {"lat": 37.4602, "lon": 126.4407, "unit": "C", "tz": "Asia/Seoul"},
    "Guangzhou": {"lat": 23.3924, "lon": 113.2988, "unit": "C", "tz": "Asia/Shanghai"},
    "Shenzhen": {"lat": 22.6393, "lon": 113.8107, "unit": "C", "tz": "Asia/Shanghai"},
    "New York": {"lat": 40.7769, "lon": -73.8740, "unit": "F", "tz": "America/New_York"},
    "Chicago": {"lat": 41.9742, "lon": -87.9073, "unit": "F", "tz": "America/Chicago"},
    "Miami": {"lat": 25.7959, "lon": -80.2870, "unit": "F", "tz": "America/New_York"},
    "London": {"lat": 51.5053, "lon": 0.0553, "unit": "C", "tz": "Europe/London"},
    "Paris": {"lat": 48.9694, "lon": 2.4414, "unit": "C", "tz": "Europe/Paris"},
    "Ankara": {"lat": 40.1281, "lon": 32.9951, "unit": "C", "tz": "Europe/Istanbul"},
    "Buenos Aires": {"lat": -34.8222, "lon": -58.5358, "unit": "C", "tz": "America/Argentina/Buenos_Aires"},
}


def c_to_f(c_temp):
  return (
      (c_temp * 9 / 5) + 32
      if c_temp is not None and pd.notnull(c_temp)
      else None
  )


def _create_resilient_session(retries=3, backoff_factor=1.5):
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


# Reused across all Open-Meteo Archive calls in a run rather than
# creating a fresh connection pool per city -- also gets us automatic
# retry/backoff on read timeouts and 429/5xx responses, which the raw
# `requests.get(..., timeout=15)` previously had none of. This mirrors
# the pattern already used in weather_collector.py.
_ARCHIVE_SESSION = _create_resilient_session()


def get_actual_max_temp(lat, lon, target_date_str, iana_tz, max_retries=3):
  # FIX (audit CRITICAL-3): aggregate the daily max over the city's
  # LOCAL calendar day, not the UTC calendar day. target_date_str is a
  # local date (matching the Polymarket market's named date); passing
  # timezone=UTC here previously shifted the day boundary by the city's
  # UTC offset (e.g. 9h for Tokyo), pulling in the wrong slice of
  # observations for non-UTC cities.
  url = (
      f"https://archive-api.open-meteo.com/v1/archive?"
      f"latitude={lat}&longitude={lon}&start_date={target_date_str}&end_date={target_date_str}"
      f"&daily=temperature_2m_max&timezone={iana_tz}"
  )
  for attempt in range(1, max_retries + 1):
    try:
      res = _ARCHIVE_SESSION.get(url, timeout=20)
      if res.status_code == 200:
        data = res.json()
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
          return temps[0]
        return None  # Valid response, genuinely no data for this date yet.
      # Non-200 that Retry() didn't already handle -- fall through to backoff.
    except requests.exceptions.RequestException as e:
      if attempt == max_retries:
        print(f"[Error] Failed to fetch temp for ({lat}, {lon}) after {max_retries} attempts: {e}")
        return None
    time.sleep(attempt * 2)
  return None


def match_observed_to_bucket(temp_native, poly_prices):
  """Matches observed temperature to actual market range buckets dynamically."""
  # Safely exit if temp is None or NaN
  if temp_native is None or pd.isna(temp_native) or not poly_prices:
    return None

  rounded_val = int(round(temp_native))

  for bucket_label in poly_prices.keys():
    lbl = str(bucket_label).strip()
    lbl_lower = lbl.lower()

    # Range match (e.g., "84-85°F")
    range_match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)", lbl
    )
    if range_match:
      low = float(range_match.group(1))
      high = float(range_match.group(2))
      if (
          (low - 0.5) <= temp_native <= (high + 0.5)
      ) or low <= rounded_val <= high:
        return bucket_label
      continue

    # Single degree match (e.g., "30°C")
    if re.search(r"\b" + str(rounded_val) + r"\s*°", lbl):
      return bucket_label

    # Bounded matches (e.g., "94°F or higher", "75°F or below")
    nums = re.findall(r"-?\d+(?:\.\d+)?", lbl)
    if nums:
      bound_val = float(nums[0])
      if (
          "higher" in lbl_lower or "above" in lbl_lower or "over" in lbl_lower
      ) and temp_native >= (bound_val - 0.5):
        return bucket_label
      if (
          "lower" in lbl_lower or "below" in lbl_lower or "under" in lbl_lower
      ) and temp_native <= (bound_val + 0.5):
        return bucket_label

  # FIX (audit MEDIUM): previously fabricated a pseudo-label like "27°"
  # here even when no real market bucket matched, which could get
  # silently compared against predicted_bucket as if it were a genuine
  # miss. Return None instead so the caller can flag this as an
  # unresolved mapping rather than a false "wrong prediction".
  return None


def verify_and_settle():
  if not os.path.exists(INPUT_CSV):
    print(f"File '{INPUT_CSV}' not found.")
    return

  try:
    # Reads ALL schema versions of the raw log (v1 + any _v2, _v3, ...)
    # and unions them, rather than only the original un-suffixed file.
    df = load_combined_log(INPUT_CSV)
  except Exception as e:
    print(f"Critical error loading CSV: {e}")
    return

  if df.empty:
    print("CSV is empty.")
    return

  unique_targets = df[["city", "target_date"]].drop_duplicates()
  proxy_results = {}
  official_results = {}

  print(
      "Fetching actual outcomes: official source where available (NOAA/HKO),"
      " Open-Meteo Archive proxy for every city as a cross-check/fallback..."
  )
  for _, row in unique_targets.iterrows():
    city = row["city"]
    target_date = str(row["target_date"])

    if city not in CITIES:
      continue

    unit = CITIES[city]["unit"]
    iana_tz = CITIES[city]["tz"]

    # 1. Try the real official source first (only implemented for a
    #    subset of cities -- see official_settlement_sources.py).
    official_c, official_source = fetch_official_actual_max_c(city, target_date, iana_tz)
    if official_c is not None:
      official_results[(city, target_date)] = (official_c, official_source)

    # 2. ALWAYS also compute the Open-Meteo proxy, even for cities with
    #    an official source -- this lets you empirically check, once
    #    enough data accumulates, how closely the proxy tracks the real
    #    value for the cities where we can compare them directly.
    proxy_c = get_actual_max_temp(
        CITIES[city]["lat"], CITIES[city]["lon"], target_date, iana_tz
    )
    if proxy_c is not None:
      proxy_results[(city, target_date)] = proxy_c

    if official_c is not None:
      native = c_to_f(official_c) if unit == "F" else official_c
      print(f"  ✓ {city} on {target_date}: OFFICIAL {official_c}°C ({native:.1f}°{unit}) via {official_source}")
    elif proxy_c is not None:
      native = c_to_f(proxy_c) if unit == "F" else proxy_c
      print(f"  ~ {city} on {target_date}: proxy only {proxy_c}°C ({native:.1f}°{unit})")
    else:
      print(f"  ✗ {city} on {target_date}: Data not available yet.")

  if not official_results and not proxy_results:
    print(
        "No actual temperature data found yet (target dates likely"
        " haven't finished yet). Still writing the evaluated CSV so the"
        " dashboard reflects the latest raw log data -- settlement"
        " columns will simply be empty for unresolved rows."
    )

  def resolve_actual(r):
    key = (r["city"], str(r["target_date"]))
    if key in official_results:
      val, source = official_results[key]
      return pd.Series([val, val, source])
    if key in proxy_results:
      return pd.Series([None, proxy_results[key], "openmeteo_archive_proxy_fallback"])
    return pd.Series([None, None, None])

  resolved = df.apply(resolve_actual, axis=1)
  resolved.columns = ["actual_max_c_official", "actual_max_c_used", "settlement_source"]
  df["actual_max_c_official"] = resolved["actual_max_c_official"]
  # NOTE: kept under its established name for continuity with earlier
  # rows -- ALWAYS the proxy value when available, independent of
  # whether an official value was also found (see note above).
  df["actual_max_c_openmeteo_proxy"] = df.apply(
      lambda r: proxy_results.get((r["city"], str(r["target_date"]))), axis=1
  )
  # The value actually used for bucket evaluation below: official when
  # we have it, proxy as fallback otherwise.
  df["actual_max_c_used"] = resolved["actual_max_c_used"]
  df["settlement_source"] = resolved["settlement_source"]

  # Dynamic Bucket Evaluation
  def evaluate_row(row):
    city = row["city"]
    actual_c = row["actual_max_c_used"]
    if actual_c is None or pd.isna(actual_c) or city not in CITIES:
      return None, None, None, None, None, None

    unit = CITIES[city]["unit"]
    native_temp = c_to_f(actual_c) if unit == "F" else actual_c

    poly_prices = {}
    if pd.notnull(row.get("all_bucket_prices")):
      try:
        poly_prices = json.loads(row["all_bucket_prices"])
      except Exception:
        pass

    winning_bucket = match_observed_to_bucket(native_temp, poly_prices)
    if winning_bucket is None:
      # FIX (audit MEDIUM): an unresolved bucket mapping is UNKNOWN, not
      # a miss. Previously this fell through to hit=False, which is
      # indistinguishable from "ECMWF genuinely predicted the wrong
      # bucket" in later analysis.
      return None, None, None, None, None, None

    def _bucket_hit(bucket_val):
      if bucket_val is None:
        return None
      if isinstance(bucket_val, float) and pd.isna(bucket_val):
        return None
      return str(bucket_val).strip() == str(winning_bucket).strip()

    # ecmwf_hit kept exactly as before (against legacy predicted_bucket,
    # which == the ECMWF-matched bucket -- see SCHEMA.md) for continuity
    # with every prior row's ecmwf_hit value.
    ecmwf_hit = _bucket_hit(row.get("predicted_bucket"))

    # Equivalent comparison for GFS, ICON, the city-matched national
    # model (schema v4 -- None for the 11 cities without one, same as
    # the field itself being absent), and the market's own favorite
    # (modal) bucket. Only populated for rows that have the relevant
    # schema-version fields -- None for older rows.
    gfs_hit = _bucket_hit(row.get("gfs_bucket"))
    icon_hit = _bucket_hit(row.get("icon_bucket"))
    national_hit = _bucket_hit(row.get("national_model_bucket"))
    market_favorite_hit = _bucket_hit(row.get("market_modal_bucket"))

    return winning_bucket, ecmwf_hit, gfs_hit, icon_hit, national_hit, market_favorite_hit

  eval_res = df.apply(evaluate_row, axis=1)
  df["actual_bucket"] = [res[0] for res in eval_res]
  df["ecmwf_hit"] = [res[1] for res in eval_res]
  df["gfs_hit"] = [res[2] for res in eval_res]
  df["icon_hit"] = [res[3] for res in eval_res]
  df["national_model_hit"] = [res[4] for res in eval_res]
  df["market_favorite_hit"] = [res[5] for res in eval_res]

  df.to_csv(
      EVALUATED_CSV,
      index=False,
      encoding="utf-8-sig",
      quoting=csv.QUOTE_MINIMAL,
      escapechar="\\",
  )
  print(f"\nSaved evaluated dataset to '{EVALUATED_CSV}'!")


if __name__ == "__main__":
  verify_and_settle()
