import json
import os
import pandas as pd
import requests
from datetime import datetime, timezone

# Target Cities with exact weather station lat/lon coordinates
CITIES = {
    "Hong Kong": {"lat": 22.3193, "lon": 114.1694, "slug": "hong-kong"},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503, "slug": "tokyo"},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737, "slug": "shanghai"},
    "Qingdao": {"lat": 36.0671, "lon": 120.3826, "slug": "qingdao"},
    "Seoul": {"lat": 37.5665, "lon": 126.9780, "slug": "seoul"},
    "Guangzhou": {"lat": 23.1291, "lon": 113.2644, "slug": "guangzhou"},
    "Shenzhen": {"lat": 22.5431, "lon": 114.0579, "slug": "shenzhen"},
}

CSV_FILE = "polymarket_weather_live_log.csv"

# Enhanced Schema incorporating ChatGPT's feedback
CSV_COLUMNS = [
    "timestamp_utc",
    "city",
    "target_date",
    "lead_time_hours",       # Hours remaining until target date midnight
    "ecmwf_max_c",
    "gfs_max_c",
    "model_spread_c",        # ECMWF vs GFS delta (ECMWF - GFS)
    "predicted_bucket",
    "polymarket_price",
    "all_bucket_prices"
]

def get_weather_forecast(lat, lon):
    """Fetch target forecast along with model run timestamps."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&models=ecmwf_ifs025,gfs_seamless&timezone=UTC"
    res = requests.get(url, timeout=10).json()
    
    target_date = res["daily"]["time"][1]  # Tomorrow's date string
    ecmwf_max = res["daily"]["temperature_2m_max_ecmwf_ifs025"][1]
    gfs_max = res["daily"]["temperature_2m_max_gfs_seamless"][1]
    
    return target_date, ecmwf_max, gfs_max

def parse_event_markets(event_data):
    """Extract YES outcome prices across all temperature buckets."""
    bucket_prices = {}
    if not event_data or "markets" not in event_data:
        return bucket_prices
    
    for market in event_data.get("markets", []):
        bucket = market.get("groupItemTitle") or market.get("question")
        raw_prices = market.get("outcomePrices")
        
        if bucket and raw_prices:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            if prices:
                bucket_prices[bucket] = float(prices[0])
    return bucket_prices

def get_polymarket_prices(city_name, city_slug, target_date_str):
    """Fetches Polymarket bucket prices using exact slug lookups with search fallback."""
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    
    event_slug = f"highest-temperature-in-{city_slug}-on-{month_name}-{dt.day}-{dt.year}"
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
        res = requests.get(url_search, params={"active": "true", "closed": "false", "q": city_name}, timeout=10)
        if res.status_code == 200:
            events = res.json()
            if isinstance(events, list):
                for event in events:
                    title = event.get("title", "").lower()
                    desc = event.get("description", "").lower()
                    if "highest temperature" in title and (target_date_str in title or target_date_str in desc):
                        prices = parse_event_markets(event)
                        if prices:
                            return prices
    except Exception:
        pass
        
    return {}

def log_snapshot():
    now_dt = datetime.now(timezone.utc)
    now_utc_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    records = []

    for city_name, info in CITIES.items():
        try:
            target_date, ecmwf_t, gfs_t = get_weather_forecast(info["lat"], info["lon"])
            poly_prices = get_polymarket_prices(city_name, info["slug"], target_date)
            
            # Calculate Lead Time (Hours until 00:00 UTC of target date)
            target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            lead_time_hours = round((target_dt - now_dt).total_seconds() / 3600.0, 2)
            
            # Model Disagreement Metric
            model_spread = round(ecmwf_t - gfs_t, 2)
            
            predicted_bucket = f"{int(round(ecmwf_t))}°C"
            matching_price = poly_prices.get(predicted_bucket, None)
            
            records.append({
                "timestamp_utc": now_utc_str,
                "city": city_name,
                "target_date": target_date,
                "lead_time_hours": lead_time_hours,
                "ecmwf_max_c": ecmwf_t,
                "gfs_max_c": gfs_t,
                "model_spread_c": model_spread,
                "predicted_bucket": predicted_bucket,
                "polymarket_price": matching_price,
                "all_bucket_prices": json.dumps(poly_prices, ensure_ascii=False)
            })

        except Exception as e:
            print(f"Error processing {city_name}: {e}")

    if records:
        df = pd.DataFrame(records, columns=CSV_COLUMNS)
        file_exists = os.path.exists(CSV_FILE)
        df.to_csv(CSV_FILE, mode="a", header=not file_exists, index=False, encoding="utf-8-sig")
        print(f"[{now_utc_str}] Logged {len(records)} city snapshots.")

if __name__ == "__main__":
    log_snapshot()
