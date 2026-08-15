import csv
from datetime import datetime
import json
import os
import re
import pandas as pd
import requests

INPUT_CSV = "polymarket_weather_live_log.csv"
EVALUATED_CSV = "polymarket_weather_evaluated.csv"

CITIES = {
    "Hong Kong": {"lat": 22.3193, "lon": 114.1694, "unit": "C"},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503, "unit": "C"},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737, "unit": "C"},
    "Qingdao": {"lat": 36.0671, "lon": 120.3826, "unit": "C"},
    "Seoul": {"lat": 37.5665, "lon": 126.9780, "unit": "C"},
    "Guangzhou": {"lat": 23.1291, "lon": 113.2644, "unit": "C"},
    "Shenzhen": {"lat": 22.5431, "lon": 114.0579, "unit": "C"},
    "New York": {"lat": 40.7128, "lon": -74.0060, "unit": "F"},
    "Chicago": {"lat": 41.8781, "lon": -87.6298, "unit": "F"},
    "Miami": {"lat": 25.7617, "lon": -80.1918, "unit": "F"},
    "London": {"lat": 51.5074, "lon": -0.1278, "unit": "C"},
    "Paris": {"lat": 48.8566, "lon": 2.3522, "unit": "C"},
    "Ankara": {"lat": 39.9334, "lon": 32.8597, "unit": "C"},
    "Buenos Aires": {"lat": -34.6037, "lon": -58.3816, "unit": "C"},
}


def c_to_f(c_temp):
  return (c_temp * 9 / 5) + 32 if c_temp is not None else None


def get_actual_max_temp(lat, lon, target_date_str):
  url = (
      f"https://archive-api.open-meteo.com/v1/archive?"
      f"latitude={lat}&longitude={lon}&start_date={target_date_str}&end_date={target_date_str}"
      f"&daily=temperature_2m_max&timezone=UTC"
  )
  try:
    res = requests.get(url, timeout=10)
    if res.status_code == 200:
      data = res.json()
      temps = data.get("daily", {}).get("temperature_2m_max", [])
      if temps and temps[0] is not None:
        return temps[0]
  except Exception as e:
    print(f"[Error] Failed to fetch temp for ({lat}, {lon}): {e}")
  return None


def match_observed_to_bucket(temp_native, poly_prices):
  """Matches observed temperature to actual market range buckets dynamically."""
  if temp_native is None or not poly_prices:
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

  return f"{rounded_val}°"


def verify_and_settle():
  if not os.path.exists(INPUT_CSV):
    print(f"File '{INPUT_CSV}' not found.")
    return

  try:
    df = pd.read_csv(
        INPUT_CSV, engine="python", on_bad_lines="skip", quoting=csv.QUOTE_MINIMAL
    )
  except Exception as e:
    print(f"Critical error loading CSV: {e}")
    return

  if df.empty:
    print("CSV is empty.")
    return

  unique_targets = df[["city", "target_date"]].drop_duplicates()
  actual_results = {}

  print("Fetching actual historical temperatures from Open-Meteo Archive...")
  for _, row in unique_targets.iterrows():
    city = row["city"]
    target_date = str(row["target_date"])

    if city in CITIES:
      unit = CITIES[city]["unit"]
      actual_temp_c = get_actual_max_temp(
          CITIES[city]["lat"], CITIES[city]["lon"], target_date
      )

      if actual_temp_c is not None:
        native_temp = (
            c_to_f(actual_temp_c) if unit == "F" else actual_temp_c
        )
        actual_results[(city, target_date)] = (actual_temp_c, native_temp)
        print(
            f"  ✓ {city} on {target_date}: {actual_temp_c}°C"
            f" ({native_temp:.1f}°{unit})"
        )
      else:
        print(f"  ✗ {city} on {target_date}: Data not available yet.")

  if not actual_results:
    print("No actual temperature data found yet. Try again tomorrow.")
    return

  # Map numerical actual temperatures
  df["actual_max_c"] = df.apply(
      lambda r: actual_results.get((r["city"], str(r["target_date"])), (None, None))[
          0
      ],
      axis=1,
  )

  # Dynamic Bucket Evaluation
  def evaluate_row(row):
    city = row["city"]
    actual_c = row["actual_max_c"]
    if actual_c is None or city not in CITIES:
      return None, False

    unit = CITIES[city]["unit"]
    native_temp = c_to_f(actual_c) if unit == "F" else actual_c

    # Safely load json prices to match winning bucket label
    poly_prices = {}
    if pd.notnull(row["all_bucket_prices"]):
      try:
        poly_prices = json.loads(row["all_bucket_prices"])
      except Exception:
        pass

    winning_bucket = match_observed_to_bucket(native_temp, poly_prices)
    hit = str(row["predicted_bucket"]).strip() == str(winning_bucket).strip()
    return winning_bucket, hit

  eval_res = df.apply(evaluate_row, axis=1)
  df["actual_bucket"] = [res[0] for res in eval_res]
  df["ecmwf_hit"] = [res[1] for res in eval_res]

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
