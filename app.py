import os
import json
import re
import math
import pandas as pd
import numpy as np
import streamlit as st

LIVE_FILE = "polymarket_weather_live_log.csv"

# --- PAGE CONFIG ---
st.set_page_config(page_title="Weather Market EV Tracker", layout="wide")

# --- MATHEMATICAL & STATISTICAL HELPER FUNCTIONS ---

def normal_cdf(x: float, mean: float, std_dev: float) -> float:
    """Calculates Cumulative Distribution Function (CDF) for normal distribution without scipy."""
    if std_dev <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (std_dev * math.sqrt(2))))

def parse_bucket_bounds(bucket_str: str) -> tuple[float, float]:
    """
    Parses bucket strings into numerical bounds.
    Examples:
      - "32°C" -> (31.5, 32.5)
      - "29°C or below" or "<=29°C" -> (-inf, 29.5)
      - "37°C or higher" or ">=37°C" -> (36.5, inf)
    """
    if not isinstance(bucket_str, str) or bucket_str == "N/A":
        return np.nan, np.nan
    
    clean_str = bucket_str.replace("°C", "").strip().lower()
    
    if "below" in clean_str or "<=" in clean_str:
        val = float(clean_str.replace("or below", "").replace("<=", "").strip())
        return -np.inf, val + 0.5
    elif "higher" in clean_str or "above" in clean_str or ">=" in clean_str:
        val = float(clean_str.replace("or higher", "").replace("or above", "").replace(">=", "").strip())
        return val - 0.5, np.inf
    elif "to" in clean_str:
        parts = clean_str.split("to")
        return float(parts[0].strip()), float(parts[1].strip())
    else:
        try:
            val = float(clean_str)
            return val - 0.5, val + 0.5
        except ValueError:
            return np.nan, np.nan

def calculate_gaussian_probability(
    ecmwf_temp: float, 
    gfs_temp: float, 
    low_bound: float, 
    high_bound: float, 
    base_std: float = 1.2
) -> float:
    """
    Calculates bucket probability using continuous Gaussian distribution 
    centered at the ensemble mean of ECMWF and GFS.
    """
    if pd.isna(ecmwf_temp) or pd.isna(gfs_temp) or pd.isna(low_bound) or pd.isna(high_bound):
        return 0.0
    
    ensemble_mean = (ecmwf_temp + gfs_temp) / 2.0
    model_spread = abs(ecmwf_temp - gfs_temp)
    effective_std = base_std + (model_spread * 0.3)
    
    if np.isneginf(low_bound):
        return float(normal_cdf(high_bound, ensemble_mean, effective_std))
    elif np.isposinf(high_bound):
        return float(1.0 - normal_cdf(low_bound, ensemble_mean, effective_std))
    else:
        return float(
            normal_cdf(high_bound, ensemble_mean, effective_std) - 
            normal_cdf(low_bound, ensemble_mean, effective_std)
        )

def generate_trade_signal(ev_pct: float, model_spread: float, price: float) -> tuple[str, str]:
    """Applies strict trading filters for liquidity and model divergence."""
    if pd.isna(price) or price < 0.03:
        return "SKIP", "Low Price / Illiquid (< 3¢)"
    if model_spread > 1.5:
        return "NEUTRAL", f"High Model Divergence ({model_spread:.1f}°C)"
    
    if ev_pct >= 50.0:
        return "STRONG BUY 🔥", "High Model Consensus & Edge"
    elif ev_pct >= 20.0:
        return "BUY 🟢", "Positive EV Edge"
    elif ev_pct <= -20.0:
        return "NO BUY ❌", "Negative EV Edge"
    else:
        return "NEUTRAL ⚪", "Fairly Priced Market"

# --- DATA LOADER FOR SHIFTING SCHEMA CSV ---
@st.cache_data(ttl=30)
def load_and_clean_data(filepath):
    if not os.path.exists(filepath):
        return None, f"File not found: `{filepath}`"

    try:
        data = []
        with open(filepath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if not lines:
            return None, "File exists but contains no data."

        for line in lines[1:]:
            parts = re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', line)
            if len(parts) >= 8:
                row_dict = {
                    "timestamp_utc": parts[0].strip(),
                    "city": parts[1].strip(),
                    "target_date": parts[2].strip(),
                    "ecmwf_max_c": parts[3].strip(),
                    "gfs_max_c": parts[4].strip(),
                    "predicted_bucket": parts[5].strip(),
                    "polymarket_price": parts[6].strip(),
                    "all_bucket_prices": parts[-1].strip().strip('"')
                }
                data.append(row_dict)

        df = pd.DataFrame(data)
        if df.empty:
            return None, "No valid rows extracted."

        df["ecmwf_max_c"] = pd.to_numeric(df["ecmwf_max_c"], errors="coerce")
        df["gfs_max_c"] = pd.to_numeric(df["gfs_max_c"], errors="coerce")
        df["polymarket_price"] = pd.to_numeric(df["polymarket_price"], errors="coerce")
        df["target_date"] = pd.to_datetime(df["target_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        
        return df.dropna(subset=["target_date"]), None

    except Exception as e:
        return None, f"Error parsing file: {str(e)}"

# --- STREAMLIT DASHBOARD UI ---

st.title("☀️ Weather Market Value & EV Tracker")

live_df, error_msg = load_and_clean_data(LIVE_FILE)

if error_msg:
    st.error(f"⚠️ {error_msg}")
    st.stop()

# Filter active date
available_dates = sorted(live_df["target_date"].dropna().unique())
selected_date = st.selectbox("Active Target Date", available_dates, index=len(available_dates)-1)

target_df = live_df[live_df["target_date"] == selected_date].copy()
latest_snapshots = target_df.sort_values("timestamp_utc").groupby("city").last().reset_index()

# Extract individual buckets from JSON field
processed_rows = []
for idx, row in latest_snapshots.iterrows():
    city = row.get("city")
    ecmwf = row.get("ecmwf_max_c")
    gfs = row.get("gfs_max_c")
    raw_json = row.get("all_bucket_prices")
    
    spread = abs(gfs - ecmwf) if (pd.notna(ecmwf) and pd.notna(gfs)) else np.nan

    if raw_json and raw_json != "{}":
        try:
            cleaned_json = raw_json.replace('""', '"')
            prices_dict = json.loads(cleaned_json)
            
            for bucket, price in prices_dict.items():
                price = float(price)
                low_b, high_b = parse_bucket_bounds(bucket)
                win_prob = calculate_gaussian_probability(ecmwf, gfs, low_b, high_b)
                
                if pd.notna(price) and price > 0:
                    ev_pct = ((win_prob - price) / price) * 100.0
                else:
                    ev_pct = 0.0
                    
                signal, reason = generate_trade_signal(ev_pct, spread, price)
                
                processed_rows.append({
                    "city": city,
                    "bucket": bucket,
                    "ecmwf_max_c": ecmwf,
                    "gfs_max_c": gfs,
                    "model_spread": spread,
                    "market_price": price,
                    "gaussian_prob": win_prob,
                    "ev_pct": ev_pct,
                    "signal": signal,
                    "signal_reason": reason
                })
        except Exception:
            continue

display_df = pd.DataFrame(processed_rows)

if display_df.empty:
    st.warning("No bucket evaluation data available for selected date.")
    st.stop()

# Filter high EV opportunities
high_ev_df = display_df[display_df["signal"].str.contains("BUY")].copy()

# Metric Cards Header
col1, col2, col3 = st.columns(3)
col1.metric("Selected Target Date", selected_date)
col2.metric("Total Buckets Evaluated", len(display_df))
col3.metric("Actionable Buy Signals", len(high_ev_df))

st.markdown("---")

# Actionable Opportunities Table
st.subheader("🎯 Actionable Positioning Opportunities")

if not high_ev_df.empty:
    st.dataframe(
        high_ev_df[[
            "signal", "city", "bucket", "ecmwf_max_c", "gfs_max_c", 
            "model_spread", "market_price", "gaussian_prob", "ev_pct"
        ]].style.format({
            "ecmwf_max_c": "{:.1f}°C",
            "gfs_max_c": "{:.1f}°C",
            "model_spread": "{:.1f}°C",
            "market_price": "${:.3f}",
            "gaussian_prob": "{:.1%}",
            "ev_pct": "{:+.1f}%"
        }),
        use_container_width=True
    )
else:
    st.info("No high-confidence buy opportunities identified for this target date under current risk thresholds.")

# Full Market Depth Table
st.markdown("---")
st.subheader("📊 Full Market Snapshot & Diagnostics")
st.dataframe(
    display_df[[
        "city", "bucket", "signal", "signal_reason", "ecmwf_max_c", "gfs_max_c", 
        "market_price", "gaussian_prob", "ev_pct"
    ]].style.format({
        "ecmwf_max_c": "{:.1f}°C",
        "gfs_max_c": "{:.1f}°C",
        "market_price": "${:.3f}",
        "gaussian_prob": "{:.1%}",
        "ev_pct": "{:+.1f}%"
    }),
    use_container_width=True
)
