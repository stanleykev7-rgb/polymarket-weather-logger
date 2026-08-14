import json
import os
import requests
import pandas as pd
from datetime import datetime, timezone

# Target Asian Coastal Cities
CITIES = {
    "Hong Kong": {"lat": 22.3193, "lon": 114.1694, "slug": "hong-kong"},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503, "slug": "tokyo"},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737, "slug": "shanghai"},
    "Qingdao": {"lat": 36.0671, "lon": 120.3826, "slug": "qingdao"},
    "Seoul": {"lat": 37.5665, "lon": 126.9780, "slug": "seoul"},
    "Guangzhou": {"lat": 23.1291, "lon": 113.2644, "slug": "guangzhou"},
    "Shenzhen": {"lat": 22.5431, "lon": 114.0579, "slug": "shenzhen"}
}

CSV_FILE = "polymarket_weather_live_log.csv"

def get_weather_forecast(lat, lon):
    """Fetch next day max temperature from Open-Meteo (ECMWF & GFS)."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&models=ecmwf_ifs025,gfs_seamless&timezone=UTC"
    res = requests.get(url, timeout=10).json()
    target_date = res['daily']['time'][1] # Tomorrow's date string (YYYY-MM-DD)
    ecmwf_max = res['daily']['temperature_2m_max_ecmwf_ifs025'][1]
    gfs_max = res['daily']['temperature_2m_max_gfs_seamless'][1]
    return target_date, ecmwf_max, gfs_max

def parse_event_markets(event_data):
    """Extract YES outcome prices across all temperature buckets in an event."""
    bucket_prices = {}
    if not event_data or 'markets' not in event_data:
        return bucket_prices

    for market in event_data.get('markets', []):
        # Extract temperature bucket title (e.g., "31°C", "32°C or higher")
        bucket = market.get('groupItemTitle') or market.get('question')
        raw_prices = market.get('outcomePrices')
        
        if bucket and raw_prices:
            # Polymarket prices are JSON arrays formatted like ["0.25", "0.75"]
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            if prices:
                bucket_prices[bucket] = float(prices[0]) # YES price
    return bucket_prices

def get_polymarket_prices(city_name, city_slug, target_date_str):
    """Fetches Polymarket bucket prices using exact slug lookups with search fallback."""
    # Format date components (e.g. "2026-08-15" -> month="august", day=15, year=2026)
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year

    # Method 1: Direct Slug Lookup
    event_slug = f"highest-temperature-in-{city_slug}-on-{month_name}-{day}-{year}"
    url_slug = f"https://gamma-api.polymarket.com/events/slug/{event_slug}"

    try:
        res = requests.get(url_slug, timeout=10)
        if res.status_code == 200:
            prices = parse_event_markets(res.json())
            if prices:
                return prices
    except Exception as e:
        print(f"[Warning] Slug lookup failed for {city_slug}: {e}")

    # Method 2: Query Search Fallback
    url_search = "https://gamma-api.polymarket.com/events"
    params = {"active": "true", "closed": "false", "q": city_name}

    try:
        res = requests.get(url_search, params=params, timeout=10)
        if res.status_code == 200:
            events = res.json()
            if isinstance(events, list):
                for event in events:
                    title = event.get('title', '').lower()
                    desc = event.get('description', '').lower()
                    if "highest temperature" in title and (target_date_str in title or target_date_str in desc):
                        prices = parse_event_markets(event)
                        if prices:
                            return prices
    except Exception as e:
        print(f"[Warning] Search query fallback failed for {city_name}: {e}")

    return {}

def log_snapshot():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    records = []
    
    for city_name, info in CITIES.items():
        try:
            target_date, ecmwf_t, gfs_t = get_weather_forecast(info['lat'], info['lon'])
            poly_prices = get_polymarket_prices(city_name, info['slug'], target_date)
            
            predicted_bucket = f"{int(round(ecmwf_t))}°C"
            matching_price = poly_prices.get(predicted_bucket, None)
            
            records.append({
                'timestamp_utc': now_utc,
                'city': city_name,
                'target_date': target_date,
                'ecmwf_max_c': ecmwf_t,
                'gfs_max_c': gfs_t,
                'predicted_bucket': predicted_bucket,
                'polymarket_price': matching_price,
                'all_bucket_prices': json.dumps(poly_prices)
            })
            print(f"[{now_utc}] Logged {city_name} for {target_date} -> Forecast: {predicted_bucket}, Poly Price: {matching_price}")
        except Exception as e:
            print(f"Error processing {city_name}: {e}")
            
    if records:
        df = pd.DataFrame(records)
        if os.path.exists(CSV_FILE):
            df.to_csv(CSV_FILE, mode='a', header=False, index=False)
        else:
            df.to_csv(CSV_FILE, mode='w', header=True, index=False)

if __name__ == "__main__":
    log_snapshot()
