import csv
from datetime import datetime
import json
import os
import pandas as pd
import requests

INPUT_CSV = "polymarket_weather_live_log.csv"
EVALUATED_CSV = "polymarket_weather_evaluated.csv"

# Fully synchronized 14-city configuration (matching log_snapshot)
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
  """Converts Celsius to Fahrenheit for US markets."""
  return (c_temp * 9 / 5) + 32 if c_temp is not None else None


def get_actual_max_temp(lat, lon, target_date_str):
  """Fetch actual historical observed max temperature (°C) from Open-Meteo Archive API."""
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
    print(
        f"[Error] Failed to fetch actual temp for ({lat}, {lon}) on"
        f" {target_date_str}: {e}"
    )
  return None


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
        # Convert to native unit to construct the correct outcome bucket label
        native_temp = (
            c_to_f(actual_temp_c) if unit == "F" else actual_temp_c
        )
        actual_bucket = f"{int(round(native_temp))}°{unit}"

        actual_results[(city, target_date)] = (actual_temp_c, actual_bucket)
        print(
            f"  ✓ {city} on {target_date}: {actual_temp_c}°C"
            f" ({native_temp:.1f}°{unit}) -> {actual_bucket}"
        )
      else:
        print(f"  ✗ {city} on {target_date}: Data not available yet.")

  if not actual_results:
    print("No actual temperature data found yet. Try again tomorrow.")
    return

  df["actual_max_c"] = df.apply(
      lambda r: actual_results.get((r["city"], str(r["target_date"])), (None, None))[
          0
      ],
      axis=1,
  )
  df["actual_bucket"] = df.apply(
      lambda r: actual_results.get((r["city"], str(r["target_date"])), (None, None))[
          1
      ],
      axis=1,
  )

  df["ecmwf_hit"] = df["predicted_bucket"] == df["actual_bucket"]

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
