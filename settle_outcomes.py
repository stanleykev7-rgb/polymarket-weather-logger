from datetime import datetime
import json
import os
import pandas as pd
import requests

INPUT_CSV = "polymarket_weather_live_log.csv"
EVALUATED_CSV = "polymarket_weather_evaluated.csv"

# Asian Coastal Cities (matching coordinates from collector)
CITIES = {
    "Hong Kong": {"lat": 22.3193, "lon": 114.1694},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737},
    "Qingdao": {"lat": 36.0671, "lon": 120.3826},
    "Seoul": {"lat": 37.5665, "lon": 126.9780},
    "Guangzhou": {"lat": 23.1291, "lon": 113.2644},
    "Shenzhen": {"lat": 22.5431, "lon": 114.0579},
}


def get_actual_max_temp(lat, lon, target_date_str):
  """Fetch actual historical observed max temperature from Open-Meteo Archive API."""
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

  df = pd.read_csv(INPUT_CSV)
  if df.empty:
    print("CSV is empty.")
    return

  # Extract unique (city, target_date) pairs
  unique_targets = df[["city", "target_date"]].drop_duplicates()
  actual_results = {}

  print("Fetching actual historical temperatures from Open-Meteo Archive...")
  for _, row in unique_targets.iterrows():
    city = row["city"]
    target_date = str(row["target_date"])

    if city in CITIES:
      actual_temp = get_actual_max_temp(
          CITIES[city]["lat"], CITIES[city]["lon"], target_date
      )
      if actual_temp is not None:
        actual_bucket = f"{int(round(actual_temp))}°C"
        actual_results[(city, target_date)] = (actual_temp, actual_bucket)
        print(f"  ✓ {city} on {target_date}: {actual_temp}°C -> {actual_bucket}")
      else:
        print(f"  ✗ {city} on {target_date}: Data not available yet.")

  if not actual_results:
    print("No actual temperature data found yet. Try again tomorrow.")
    return

  # Map actual results back to each row
  df["actual_max_c"] = df.apply(
      lambda r: actual_results.get((r["city"], str(r["target_date"])), (None, None))[0],
      axis=1,
  )
  df["actual_bucket"] = df.apply(
      lambda r: actual_results.get((r["city"], str(r["target_date"])), (None, None))[1],
      axis=1,
  )

  # Check if model predictions hit the exact winning bucket
  df["ecmwf_hit"] = df["predicted_bucket"] == df["actual_bucket"]

  # Save evaluated dataset
  df.to_csv(EVALUATED_CSV, index=False, encoding="utf-8-sig")
  print(f"\nSaved evaluated dataset to '{EVALUATED_CSV}'!")


if __name__ == "__main__":
  verify_and_settle()
