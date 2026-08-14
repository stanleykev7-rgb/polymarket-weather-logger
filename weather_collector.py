import os
import requests
import pandas as pd
from datetime import datetime, timezone

# Target Asian Coastal Cities (High Win-Rate Locations)
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
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&models=ecmwf_ifs025,gfs_seamless&timezone=UTC"
    res = requests.get(url, timeout=10).json()
    target_date = res['daily']['time'][1] # Day + 1 forecast
    ecmwf_max = res['daily']['temperature_2m_max_ecmwf_ifs025'][1]
    gfs_max = res['daily']['temperature_2m_max_gfs_seamless'][1]
    return target_date, ecmwf_max, gfs_max

def get_polymarket_prices(city_slug, target_date):
    # Formats target_date to match Gamma API slug conventions (e.g. august-15-2026)
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    date_str = dt.strftime("%B-%d-%Y").lower().replace("-0", "-")
    
    event_slug = f"highest-temperature-in-{city_slug}-on-{date_str}"
    url = f"https://gamma-api.polymarket.com/events?slug={event_slug}"
    
    try:
        res = requests.get(url, timeout=10).json()
        if not res or len(res) == 0:
            return {}
        
        markets = res[0].get('markets', [])
        bucket_prices = {}
        for m in markets:
            group_item = m.get('groupItemTitle', '')
            outcome_prices = m.get('outcomePrices', [])
            if group_item and outcome_prices:
                bucket_prices[group_item] = float(outcome_prices[0])
        return bucket_prices
    except Exception as e:
        print(f"Error fetching Polymarket for {city_slug}: {e}")
        return {}

def log_snapshot():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    records = []
    
    for city_name, info in CITIES.items():
        try:
            target_date, ecmwf_t, gfs_t = get_weather_forecast(info['lat'], info['lon'])
            poly_prices = get_polymarket_prices(info['slug'], target_date)
            
            predicted_bucket = f"{round(ecmwf_t)}°C"
            matching_price = poly_prices.get(predicted_bucket, None)
            
            records.append({
                'timestamp_utc': now_utc,
                'city': city_name,
                'target_date': target_date,
                'ecmwf_max_c': ecmwf_t,
                'gfs_max_c': gfs_t,
                'predicted_bucket': predicted_bucket,
                'polymarket_price': matching_price,
                'all_bucket_prices': str(poly_prices)
            })
        except Exception as e:
            print(f"Error processing {city_name}: {e}")
            
    if records:
        df = pd.DataFrame(records)
        if os.path.exists(CSV_FILE):
            df.to_csv(CSV_FILE, mode='a', header=False, index=False)
        else:
            df.to_csv(CSV_FILE, mode='w', header=True, index=False)
        print(f"[{now_utc}] Logged snapshot successfully.")

if __name__ == "__main__":
    log_snapshot()
