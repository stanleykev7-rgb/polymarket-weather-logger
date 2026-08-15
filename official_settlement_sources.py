"""
Official (non-proxy) settlement source fetchers.

Confirmed by directly reading multiple live Polymarket market rules pages
(2026-08):

  City          Official resolution source                  Station
  ------------  -------------------------------------------  --------
  New York      Wunderground -> LaGuardia Airport Station     KLGA
  Chicago       Wunderground -> O'Hare Intl Airport Station   KORD
  Miami         Wunderground -> Miami Intl Airport Station    KMIA
  Hong Kong     Hong Kong Observatory (NOT Wunderground)      HKO HQ

Rules text also specifies: "highest temperature recorded in the 'Daily
Observations' table ... not the ... 'Day High & Low' summary" -- i.e.
the max across all individual sub-daily observations for the station's
local calendar day. That's exactly what fetch_noaa_daily_max_c below
computes (max of all METAR/ASOS observations for the local day), using
the SAME underlying instrument feed Wunderground displays for these
airport stations (both ultimately source from the NWS/FAA ASOS network
for these station codes).

WHAT THIS DOES NOT COVER YET:
Tokyo, Shanghai, Qingdao, Seoul, Guangzhou, Shenzhen, London, Paris,
Ankara, and Buenos Aires all resolve via Wunderground too, but Wunderground
itself has no public, documented API for non-US stations, and no
government-run equivalent was verified for all of them in this pass.
Scraping Wunderground's site directly is possible but fragile (their
page is JS-rendered; any workable approach depends on an undocumented
internal endpoint that can change without notice) and was intentionally
NOT implemented here rather than shipping something unverified. These
cities continue to use the Open-Meteo proxy, clearly labeled as such in
`settlement_source`. See SCHEMA.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

NOAA_USER_AGENT = "polymarket-weather-logger (contact: set-a-real-contact-email)"

# Confirmed via live Polymarket rules pages, 2026-08.
NOAA_STATIONS = {
    "New York": "KLGA",
    "Chicago": "KORD",
    "Miami": "KMIA",
}

HKO_STATION = "HKO"  # Hong Kong Observatory headquarters


def _local_day_utc_bounds(target_date_str: str, iana_tz: str) -> tuple[datetime, datetime]:
    """UTC start/end instants for the LOCAL calendar day of target_date_str."""
    tz = ZoneInfo(iana_tz)
    start_local = datetime.strptime(target_date_str, "%Y-%m-%d").replace(tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def fetch_noaa_daily_max_c(
    icao_station: str, target_date_str: str, iana_tz: str, timeout: int = 15
) -> float | None:
    """Max observed temperature (°C) for the station's LOCAL calendar day,
    from NOAA/NWS's public api.weather.gov observations endpoint. This is
    a real government data source, not a proxy -- no API key required,
    but NOAA does require a descriptive User-Agent header.

    Returns None (never raises) on any network/parsing failure, so the
    caller can fall back to the Open-Meteo proxy.
    """
    try:
        start_utc, end_utc = _local_day_utc_bounds(target_date_str, iana_tz)
        url = (
            f"https://api.weather.gov/stations/{icao_station}/observations"
            f"?start={start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&end={end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        headers = {"User-Agent": NOAA_USER_AGENT, "Accept": "application/geo+json"}
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code != 200:
            return None
        data = res.json()
        temps = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            t = props.get("temperature", {}).get("value")
            if t is not None:
                temps.append(float(t))
        if not temps:
            return None
        return round(max(temps), 2)
    except Exception as e:
        print(f"[official_settlement_sources] NOAA fetch failed for {icao_station} {target_date_str}: {e}")
        return None


def fetch_hko_daily_max_c(target_date_str: str, timeout: int = 15) -> float | None:
    """Max daily temperature (°C) for Hong Kong Observatory HQ from HKO's
    public open data API (data.weather.gov.hk). Returns None on any
    failure so the caller can fall back to the Open-Meteo proxy.

    NOTE: the exact JSON field names for HKO's CLMTEMP (daily climate
    temperature) dataset were not verified against a live network call
    in the environment this was written in. The parsing below tries
    several plausible field-name variants; if HKO's actual response
    shape doesn't match, this safely returns None (falls back to proxy)
    rather than silently returning a wrong number. Verify this against
    a real response and adjust field names if the first live run
    logs a parsing miss.
    """
    try:
        dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        url = (
            "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
            f"?dataType=CLMTEMP&station={HKO_STATION}"
            f"&year={dt.year}&month={dt.month:02d}&format=json"
        )
        res = requests.get(url, timeout=timeout)
        if res.status_code != 200:
            return None
        payload = res.json()
        rows = payload.get("data", [])
        fields = [f.lower() for f in payload.get("fields", [])]

        day_idx = next((i for i, f in enumerate(fields) if "day" in f), None)
        value_idx = next((i for i, f in enumerate(fields) if "value" in f or "data" in f), None)

        if day_idx is None or value_idx is None or not rows:
            print(
                "[official_settlement_sources] HKO response shape unrecognized"
                f" (fields={payload.get('fields')}); falling back to proxy."
            )
            return None

        for row in rows:
            try:
                if int(row[day_idx]) == dt.day:
                    return round(float(row[value_idx]), 2)
            except (ValueError, IndexError, TypeError):
                continue
        return None
    except Exception as e:
        print(f"[official_settlement_sources] HKO fetch failed for {target_date_str}: {e}")
        return None


def fetch_official_actual_max_c(
    city: str, target_date_str: str, iana_tz: str
) -> tuple[float | None, str | None]:
    """Dispatch to the right official fetcher for `city`, if one exists.

    Returns (value_c, source_label). value_c is None and source_label is
    None if no official fetcher is implemented for this city yet, or if
    the fetch failed -- callers should fall back to the proxy in either
    case.
    """
    if city in NOAA_STATIONS:
        val = fetch_noaa_daily_max_c(NOAA_STATIONS[city], target_date_str, iana_tz)
        return (val, "noaa_nws") if val is not None else (None, None)
    if city == "Hong Kong":
        val = fetch_hko_daily_max_c(target_date_str)
        return (val, "hko_opendata") if val is not None else (None, None)
    return None, None
