import pandas as pd
import numpy as np
import streamlit as st
from scipy.stats import norm

# Page Configuration
st.set_page_config(page_title="Weather Market EV Tracker", layout="wide")

EVAL_FILE = "polymarket_weather_evaluated.csv"
LIVE_FILE = "polymarket_weather_live_log.csv"

# --- MATHEMATICAL & STATISTICAL HELPER FUNCTIONS ---

def parse_bucket_bounds(bucket_str: str) -> tuple[float, float]:
    """
    Parses bucket strings into numerical bounds.
    Examples:
      - "32°C" -> (31.5, 32.5)
      - "29°C or below" or "<=29°C" -> (-inf, 29.5)
      - "37°C or above" or ">=37°C" -> (36.5, inf)
    """
    if not isinstance(bucket_str, str) or bucket_str == "N/A":
        return np.nan, np.nan
    
    clean_str = bucket_str.replace("°C", "").strip().lower()
    
    if "below" in clean_str or "<=" in clean_str:
        val = float(clean_str.replace("or below", "").replace("<=", "").strip())
        return -np.inf, val + 0.5
    elif "above" in clean_str or ">=" in clean_str:
        val = float(clean_str.replace("or above", "").replace(">=", "").strip())
        return val - 0.5, np.inf
    elif "to" in clean_str:
        parts = clean_str.split("to")
        return float(parts[0].strip()), float(parts[1].strip())
    else:
        # Exact integer bucket (e.g., "32") represents [31.5, 32.5]
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
    
    # Ensemble mean
    ensemble_mean = (ecmwf_temp + gfs_temp) / 2.0
    
    # Expand variance if models diverge
    model_spread = abs(ecmwf_temp - gfs_temp)
    effective_std = base_std + (model_spread * 0.3)
    
    if np.isneginf(low_bound):
        return float(norm.cdf(high_bound, loc=ensemble_mean, scale=effective_std))
    elif np.isposinf(high_bound):
        return float(1.0 - norm.cdf(low_bound, loc=ensemble_mean, scale=effective_std))
    else:
        return float(
            norm.cdf(high_bound, loc=ensemble_mean, scale=effective_std) - 
            norm.cdf(low_bound, loc=ensemble_mean, scale=effective_std)
        )

def generate_trade_signal(ev_pct: float, model_spread: float, price: float) -> tuple[str, str]:
    """
    Applies strict trading filters for liquidity and model divergence.
    """
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

# --- STREAMLIT DASHBOARD UI ---

st.title("☀️ Weather Market Value & EV Tracker")

# Load Datasets
try:
    live_df = pd.read_csv(LIVE_FILE)
except Exception:
    live_df = pd.DataFrame()

if live_df.empty:
    st.warning("No live market data available in `polymarket_weather_live_log.csv`.")
    st.stop()

# Filter active date
available_dates = sorted(live_df["target_date"].dropna().unique())
selected_date = st.selectbox("Active Target Date", available_dates, index=len(available_dates)-1)

target_df = live_df[live_df["target_date"] == selected_date].copy()

# Calculate Gaussian EV Metrics
processed_rows = []
for idx, row in target_df.iterrows():
    ecmwf = row.get("ecmwf_max_c")
    gfs = row.get("gfs_max_c")
    price = row.get("market_price")
    bucket = row.get("bucket")
    
    low_b, high_b = parse_bucket_bounds(bucket)
    spread = abs(gfs - ecmwf) if (pd.notna(ecmwf) and pd.notna(gfs)) else np.nan
    
    # Model probability via Gaussian CDF
    win_prob = calculate_gaussian_probability(ecmwf, gfs, low_b, high_b)
    
    # Calculate EV %
    if pd.notna(price) and price > 0:
        ev_pct = ((win_prob - price) / price) * 100.0
    else:
        ev_pct = 0.0
        
    signal, reason = generate_trade_signal(ev_pct, spread, price)
    
    row_dict = row.to_dict()
    row_dict["model_spread"] = spread
    row_dict["gaussian_prob"] = win_prob
    row_dict["ev_pct"] = ev_pct
    row_dict["signal"] = signal
    row_dict["signal_reason"] = reason
    processed_rows.append(row_dict)

display_df = pd.DataFrame(processed_rows)

# Filter out dead / illiquid buckets from main opportunities view
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
