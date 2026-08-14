import json
import os
import requests
import pandas as pd
from datetime import datetime, timezone

# Target Asian Coastal Cities
CITIES = {
    "Hong Kong": {"lat": 22.3193, "lon": 114.1694},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737},
    "Qingdao": {"lat": 36.0671, "lon": 120.3826},
    "Seoul": {"lat": 37.5665, "lon": 126.9780},
    "Guangzhou": {"lat": 23.1291, "lon": 113.2644},
    "Shenzhen": {"lat": 22.5431, "lon": 114.0579}
}

CSV_FILE = "polymarket_weather_live_log.csv"

def get_weather_forecast(lat, lon):
    """Fetch next day max temperature from Open-Meteo (ECMWF & GFS)."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&models=ecmwf_ifs025,gfs_seamless&timezone=UTC"
    res = requests.get(url, timeout=10).json()
    target_date = res['daily']['time'][1] # Tomorrow's date (YYYY-MM-DD)
    ecmwf_max = res['daily']['temperature_2m_max_ecmwf_ifs025'][1]
    gfs_max = res['daily']['temperature_2m_max_gfs_seamless'][1]
    return target_date, ecmwf_max, gfs_max

def get_polymarket_prices(city_name, target_date):
    """Dynamically search Polymarket Gamma API for active matching weather events."""
    url = "https://gamma-api.polymarket.com/events"
    params = {"active": "true", "closed": "false", "q": city_name}
    
    try:
        res = requests.get(url, params=params, timeout=10).json()
        if not res or not isinstance(res, list):
            return {}
        
        matching_event = None
        for event in res:
            title = event.get('title', '').lower()
            description = event.get('description', '').lower()
            # Match weather events containing 'highest temperature' and the target date
            if "highest temperature" in title and (target_date in title or target_date in description):
                matching_event = event
                break
                
        if not matching_event:
            return {}
            
        bucket_prices = {}
        for market in matching_event.get('markets', []):
            bucket = market.get('groupItemTitle') or market.get('question')
            raw_prices = market.get('outcomePrices')
            if bucket and raw_prices:
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                yes_price = float(prices[0]) if prices else 0.0
                bucket_prices[bucket] = round(yes_price, 3)
        return bucket_prices
    except Exception as e:
        print(f"Error fetching Polymarket for {city_name}: {e}")
        return {}

def log_snapshot():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    records = []
    
    for city_name, info in CITIES.items():
        try:
            target_date, ecmwf_t, gfs_t = get_weather_forecast(info['lat'], info['lon'])
            poly_prices = get_polymarket_prices(city_name, target_date)
            
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
            print(f"[{now_utc}] {city_name} for {target_date}: Predicted={predicted_bucket}, Poly Price={matching_price}")
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
