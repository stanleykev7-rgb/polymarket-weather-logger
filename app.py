import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(
    page_title="PolyMarket Weather Betting Terminal",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Fetch data directly from file (Auto-invalidates cache every 60s)
@st.cache_data(ttl=60)
def load_data():
    df = pd.read_csv("polymarket_weather_evaluated.csv")
    return df


df = load_data()

st.title("☀️ Weather Market Value & EV Tracker")

# Sidebar Controls & Manual Reload
st.sidebar.header("Controls")
if st.sidebar.button("🔄 Force Refresh Data"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()
st.sidebar.header("Strategy Settings")
target_date = st.sidebar.selectbox(
    "Select Target Date",
    options=sorted(df["target_date"].unique(), reverse=True),
)
min_ev = st.sidebar.slider(
    "Minimum Expected Value (EV %)", min_value=0, max_value=500, value=50
)

# Filter Data for Selected Target Date
df_active = df[df["target_date"] == target_date].copy()
latest = (
    df_active.sort_values("timestamp_utc").groupby("city").last().reset_index()
)

# Model Strategy Parameters
ECMWF_ACCURACY_RATE = 0.571  # 57.1% historical hit rate
latest["market_price"] = latest["polymarket_price"].fillna(0.0)
latest["ev_percentage"] = (
    (ECMWF_ACCURACY_RATE - latest["market_price"])
    / latest["market_price"]
    * 100
)
latest["gfs_diff"] = latest["gfs_max_c"] - latest["ecmwf_max_c"]


# Action Recommendation Logic
def get_recommendation(row):
    if row["ev_percentage"] >= 200:
        return "STRONG BUY 🔥"
    elif row["ev_percentage"] >= 50:
        return "BUY 🟢"
    else:
        return "HOLD / PASS ⚪"


latest["Signal"] = latest.apply(get_recommendation, axis=1)

# Dashboard Top Metrics
col1, col2, col3 = st.columns(3)
col1.metric("Active Target Date", target_date)
col2.metric("ECMWF Win Rate Baseline", "57.1%")
col3.metric(
    "High EV Opportunities", len(latest[latest["ev_percentage"] >= min_ev])
)

st.divider()

# High-Value Opportunities Table
st.subheader("🎯 Actionable Positioning Opportunities")

filtered_df = latest[latest["ev_percentage"] >= min_ev].sort_values(
    by="ev_percentage", ascending=False
)

st.dataframe(
    filtered_df[
        [
            "Signal",
            "city",
            "predicted_bucket",
            "ecmwf_max_c",
            "gfs_max_c",
            "market_price",
            "ev_percentage",
        ]
    ].rename(
        columns={
            "city": "City",
            "predicted_bucket": "Target Bucket",
            "ecmwf_max_c": "ECMWF (°C)",
            "gfs_max_c": "GFS (°C)",
            "market_price": "Poly Market Price",
            "ev_percentage": "Expected Value (EV %)",
        }
    ),
    column_config={
        "Poly Market Price": st.column_config.NumberColumn(format="$%.3f"),
        "Expected Value (EV %)": st.column_config.NumberColumn(format="+%.1f%%"),
        "ECMWF (°C)": st.column_config.NumberColumn(format="%.1f °C"),
        "GFS (°C)": st.column_config.NumberColumn(format="%.1f °C"),
    },
    use_container_width=True,
    hide_index=True,
)

# Visualizing GFS Divergence Exploits
st.subheader("📊 Model Divergence (GFS vs ECMWF Heat Bias)")
fig = px.bar(
    latest,
    x="city",
    y="gfs_diff",
    color="gfs_diff",
    title="GFS Temperature Over-Estimation relative to ECMWF (°C)",
    labels={
        "gfs_diff": "GFS Bias (°C)",
        "city": "City",
    },
    color_continuous_scale="Reds",
)
st.plotly_chart(fig, use_container_width=True)
