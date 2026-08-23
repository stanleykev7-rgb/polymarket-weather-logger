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
  Tokyo         Wunderground -> Haneda Airport                RJTT
  Shanghai      Wunderground -> Pudong Intl Airport           ZSPD
  Qingdao       Wunderground -> Jiaodong Intl Airport         ZSQD
  Seoul         Wunderground -> Incheon Intl Airport          RKSI
  Guangzhou     Wunderground -> Baiyun Intl Airport           ZGGG
  Shenzhen      Wunderground -> Bao'an Intl Airport           ZGSZ
  London        Wunderground -> London City Airport           EGLC
  Paris         Wunderground -> Le Bourget Airport            LFPB
  Ankara        Wunderground -> Esenboğa Intl Airport         LTAC
  Buenos Aires  Wunderground -> Ezeiza Intl Airport           SAEZ

Rules text also specifies: "highest temperature recorded in the 'Daily
Observations' table ... not the ... 'Day High & Low' summary" -- i.e.
the max across all individual sub-daily observations for the station's
local calendar day.

--- METAR SOURCE (added 2026-08, second pass) --------------------------
For every station above except HKO (which isn't an airport and has its
own direct government source instead), Wunderground's "Daily
Observations" table for an airport station is itself built from that
station's raw METAR feed. Rather than only covering the 3 US cities via
NOAA's api.weather.gov, fetch_metar_daily_max_c below queries
aviationweather.gov's official Data API (run by the same agency,
NOAA/NWS -- see https://aviationweather.gov/data/api/) directly for the
underlying METAR reports, which covers international ICAO stations too
(their own docs example a London City Airport query). This extends
real official-equivalent settlement from 4 cities to potentially all 14.

METAR reports are also the FASTEST-updating source available to this
project -- typically hourly (sometimes more often at busier airports),
versus reanalysis products like Open-Meteo Archive which have their own
processing/publication lag. Rows settled via METAR are labeled
`metar_aviationweather` in `settlement_source` so this is visible in
the data, not just in this comment.

Priority order per city (see fetch_official_actual_max_c): the 3 NOAA
cities and Hong Kong keep their existing direct-government-API sources
unchanged (no reason to replace something already working with a
same-underlying-data alternative). Every other city now tries METAR via
aviationweather.gov before falling back to the Open-Meteo proxy.
"""

from __future__ import annotations

import json
import re
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

# ICAO codes for every remaining city, confirmed via the same rules-page
# research (see SCHEMA.md for the full per-city source table).
METAR_STATIONS = {
    "Tokyo": "RJTT",
    "Shanghai": "ZSPD",
    "Qingdao": "ZSQD",
    "Seoul": "RKSI",
    "Guangzhou": "ZGGG",
    "Shenzhen": "ZGSZ",
    "London": "EGLC",
    "Paris": "LFPB",
    "Ankara": "LTAC",
    "Buenos Aires": "SAEZ",
}

# Standard WMO METAR temperature/dewpoint group, e.g. " 13/07 " or
# " M05/M10 " (M prefix = negative). Used as a fallback if the API's
# decoded `temp` field is absent for a given report -- this text format
# is a stable international standard, not expected to change.
_METAR_TEMP_RE = re.compile(r"(?:^|\s)(M?\d{2})/(M?\d{2})(?:\s|$)")


def _parse_metar_temp_c(raw_ob: str) -> float | None:
    if not raw_ob:
        return None
    m = _METAR_TEMP_RE.search(raw_ob)
    if not m:
        return None
    temp_str = m.group(1)
    try:
        return -float(temp_str[1:]) if temp_str.startswith("M") else float(temp_str)
    except ValueError:
        return None


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


def fetch_metar_daily_max_c(
    icao_station: str, target_date_str: str, iana_tz: str, timeout: int = 15
) -> float | None:
    """Max observed temperature (°C) for the station's LOCAL calendar day,
    from every METAR report issued during that window, via NOAA's public
    aviationweather.gov Data API (no API key required; rate limit is a
    generous 100 requests/minute, confirmed via their own docs).

    Prefers the API's decoded `temp` field per report; falls back to
    regex-parsing the raw METAR text's standard temperature group if
    `temp` is missing for a given report (rare, but AUTO/partial reports
    can omit it). Returns None (never raises) on any failure, so the
    caller can fall back further to the Open-Meteo proxy.

    NOTE: aviationweather.gov's documented history window is the past
    ~15 days -- more than enough for same-day/next-day settlement, but
    this will NOT work for retroactively backfilling older rows.
    """
    try:
        start_utc, end_utc = _local_day_utc_bounds(target_date_str, iana_tz)
        hours_back = (end_utc - start_utc).total_seconds() / 3600.0 + 1  # +1 margin
        url = (
            "https://aviationweather.gov/api/data/metar"
            f"?ids={icao_station}&format=json"
            f"&date={end_utc.strftime('%Y%m%d_%H%M')}"
            f"&hours={hours_back:.0f}"
        )
        res = requests.get(url, timeout=timeout)
        if res.status_code != 200:
            return None
        reports = res.json()
        if not isinstance(reports, list):
            return None

        temps = []
        for report in reports:
            obs_time_str = report.get("obsTime") or report.get("reportTime")
            # Confirm this specific report actually falls within the
            # target LOCAL day -- the API's date/hours params bound the
            # query window but individual report timestamps should
            # still be checked against the precise UTC boundary.
            obs_dt = None
            if isinstance(obs_time_str, (int, float)):
                obs_dt = datetime.fromtimestamp(obs_time_str, tz=ZoneInfo("UTC"))
            elif isinstance(obs_time_str, str):
                try:
                    obs_dt = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
                except ValueError:
                    obs_dt = None
            if obs_dt is not None and not (start_utc <= obs_dt < end_utc):
                continue

            t = report.get("temp")
            if t is None:
                t = _parse_metar_temp_c(report.get("rawOb", ""))
            if t is not None:
                temps.append(float(t))

        if not temps:
            return None
        return round(max(temps), 2)
    except Exception as e:
        print(f"[official_settlement_sources] METAR fetch failed for {icao_station} {target_date_str}: {e}")
        return None


def fetch_official_actual_max_c(
    city: str, target_date_str: str, iana_tz: str
) -> tuple[float | None, str | None]:
    """Dispatch to the right official (or official-equivalent) fetcher
    for `city`, if one exists.

    Returns (value_c, source_label). value_c is None and source_label is
    None if no fetcher is implemented for this city yet, or if every
    attempted fetch failed -- callers should fall back to the Open-Meteo
    proxy in either case.
    """
    if city in NOAA_STATIONS:
        val = fetch_noaa_daily_max_c(NOAA_STATIONS[city], target_date_str, iana_tz)
        return (val, "noaa_nws") if val is not None else (None, None)
    if city == "Hong Kong":
        val = fetch_hko_daily_max_c(target_date_str)
        return (val, "hko_opendata") if val is not None else (None, None)
    if city in METAR_STATIONS:
        val = fetch_metar_daily_max_c(METAR_STATIONS[city], target_date_str, iana_tz)
        return (val, "metar_aviationweather") if val is not None else (None, None)
    return None, None
