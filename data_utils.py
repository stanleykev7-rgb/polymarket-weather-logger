"""
Shared helpers for the polymarket-weather-logger project.

This module exists to fix two cross-cutting bugs identified in the
2026-08 audit:

1. lead_time_hours / settlement day boundaries must be computed against
   each city's LOCAL calendar day, not UTC midnight.
2. The raw log CSV's schema evolves over time (new columns get added).
   Plain `df.to_csv(mode="a")` against a file whose header was written
   once, historically, silently misaligns or drops rows once the column
   count changes. We fix this by versioning the schema: whenever the
   column set changes, new rows go into a new file
   (`<base>_v{N}.csv`), and `load_combined_log()` unions all schema
   versions back together for anything that reads the log.

Historical rows are NEVER rewritten, recalculated, or migrated. They are
read as-is and unioned with newer schema versions.
"""

from __future__ import annotations

import glob
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd


# ---------------------------------------------------------------------------
# Timezone-correct local-day math
# ---------------------------------------------------------------------------

def local_midnight_to_utc(target_date_str: str, iana_tz: str) -> datetime:
    """Return the UTC instant corresponding to LOCAL midnight of
    `target_date_str` (YYYY-MM-DD) in the given IANA timezone.

    This correctly handles fixed offsets (Tokyo +9, Hong Kong +8) and
    DST-observing zones (New York, London, Chicago) because it builds an
    aware datetime in the local zone first and lets zoneinfo resolve the
    correct UTC offset for that specific calendar date, rather than
    assuming a fixed offset or using UTC midnight as a stand-in.
    """
    local_dt = datetime.strptime(target_date_str, "%Y-%m-%d").replace(
        tzinfo=ZoneInfo(iana_tz)
    )
    return local_dt.astimezone(timezone.utc)


def compute_lead_time_hours(
    now_utc: datetime, target_date_str: str, iana_tz: str
) -> float | None:
    """Hours between `now_utc` and the start (local midnight) of the
    target local calendar day. Negative values mean the target day has
    already locally begun.

    This is the STANDARD meteorological verification convention (lead
    time = forecast issue time to the start of the valid period), and
    is what every existing row's lead_time_hours has always meant --
    kept unchanged for continuity.

    IMPORTANT DISTINCTION (raised 2026-08): this measures distance to
    the START of the target day, not to when the day's outcome is
    actually decided. The daily max temperature -- and therefore the
    market's resolution -- isn't final until the day ENDS. A forecast
    made 6 hours before the target day starts is actually ~30 hours
    before the day is over. If you want to study "does accuracy improve
    in the final hours before resolution", use
    compute_hours_to_resolution() below instead -- they answer
    different questions and shouldn't be confused for each other.
    """
    if not target_date_str:
        return None
    try:
        target_midnight_utc = local_midnight_to_utc(target_date_str, iana_tz)
    except Exception:
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return round((target_midnight_utc - now_utc).total_seconds() / 3600.0, 2)


def compute_hours_to_resolution(
    now_utc: datetime, target_date_str: str, iana_tz: str
) -> float | None:
    """Hours between `now_utc` and the END of the target local calendar
    day (i.e. target local midnight + 24h) -- the point at which that
    day's actual max temperature, and therefore the market outcome, is
    fully decided. Negative values mean the day has already ended
    locally.

    This is the complementary "how close to the answer being locked in"
    framing: a forecast made 6 hours before the target day STARTS is
    ~30 hours before the day ENDS. Use this (not lead_time_hours) to
    check whether model accuracy sharpens in the final hours before a
    market actually resolves, independent of how far ahead the day
    itself was.
    """
    if not target_date_str:
        return None
    try:
        target_midnight_utc = local_midnight_to_utc(target_date_str, iana_tz)
    except Exception:
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    target_day_end_utc = target_midnight_utc + timedelta(hours=24)
    return round((target_day_end_utc - now_utc).total_seconds() / 3600.0, 2)


def local_day_open_meteo_timezone_param(iana_tz: str) -> str:
    """Open-Meteo's `timezone=` param accepts IANA tz names directly and
    will aggregate `daily` values (e.g. temperature_2m_max) by that
    zone's local calendar day. Previously the settlement script passed
    the literal string "UTC" regardless of city, which silently shifts
    the day boundary by the city's UTC offset. Just pass the city's own
    IANA tz through.
    """
    return iana_tz


# ---------------------------------------------------------------------------
# Schema-versioned CSV read/write
# ---------------------------------------------------------------------------

def _versioned_path(base_path: str, version: int) -> str:
    root, ext = os.path.splitext(base_path)
    if version <= 1:
        return base_path  # v1 keeps the original, un-suffixed filename
    return f"{root}_v{version}{ext}"


def _existing_versions(base_path: str) -> list[int]:
    root, ext = os.path.splitext(base_path)
    versions = []
    if os.path.exists(base_path):
        versions.append(1)
    for f in glob.glob(f"{root}_v*{ext}"):
        m = re.search(r"_v(\d+)" + re.escape(ext) + r"$", f)
        if m:
            versions.append(int(m.group(1)))
    return sorted(set(versions))


def resolve_write_target(base_path: str, columns: list[str]) -> tuple[str, bool]:
    """Decide which physical file new rows with `columns` should be
    appended to, and whether a header needs to be written.

    - If no file exists yet for the highest known version, or the
      highest-version file's header exactly matches `columns`, append
      there (writing a header only if the file is new/empty).
    - If the highest-version file exists but has a DIFFERENT column
      set than `columns`, this is a schema change: start a new,
      higher-numbered version file instead of corrupting the old one.

    Returns (path_to_write, need_header).
    """
    versions = _existing_versions(base_path)
    if not versions:
        return base_path, True

    latest_version = max(versions)
    latest_path = _versioned_path(base_path, latest_version)

    try:
        existing_header = pd.read_csv(latest_path, nrows=0).columns.tolist()
    except Exception:
        # Unreadable/empty file at the latest version path -> just append
        # a header and go.
        return latest_path, True

    if existing_header == columns:
        return latest_path, False

    # Schema changed: never widen/rewrite the old file. Start a new one.
    new_version = latest_version + 1
    new_path = _versioned_path(base_path, new_version)
    return new_path, not os.path.exists(new_path)


def load_combined_log(base_path: str) -> pd.DataFrame:
    """Load and union ALL schema versions of a log file, PLUS -- for the
    original v1 file specifically -- the recovered legacy generations
    (see recover_legacy_log.py). The v1 file predates schema
    versioning and already contains multiple mixed row layouts under
    one stale header; a plain pd.read_csv on it silently drops any row
    whose field count doesn't match the (oldest, 8-column) header.

    Columns that don't exist in a given generation/version are filled
    with NaN for those rows -- never backfilled or estimated, just
    genuinely absent, exactly as the historical-data constraint
    requires.
    """
    frames = []

    # 1. Recovered legacy generations of the original (un-suffixed)
    #    v1 file, if a recovery has been run. This replaces a bare
    #    pd.read_csv(base_path), which would otherwise only see the
    #    oldest 8-column generation and silently drop the rest.
    root, ext = os.path.splitext(base_path)
    recovered_dir = os.path.join(os.path.dirname(base_path) or ".", "recovered")
    recovered_any = False
    if os.path.isdir(recovered_dir):
        for fname in sorted(os.listdir(recovered_dir)):
            if not fname.startswith("live_log_gen_") or not fname.endswith(".csv"):
                continue
            path = os.path.join(recovered_dir, fname)
            try:
                df = pd.read_csv(path, engine="python", on_bad_lines="warn")
            except Exception as e:
                print(f"[data_utils] Failed to read recovered file {path}: {e}")
                continue
            if df.empty:
                continue
            df["_schema_version"] = fname.replace("live_log_", "").replace(".csv", "")
            frames.append(df)
            recovered_any = True

    # 2. Fall back to reading base_path directly if no recovery output
    #    exists yet (e.g. a fresh project with no legacy mixed-schema
    #    rows to recover).
    if not recovered_any and os.path.exists(base_path):
        try:
            df = pd.read_csv(base_path, engine="python", on_bad_lines="warn")
            if not df.empty:
                df["_schema_version"] = 1
                frames.append(df)
        except Exception as e:
            print(f"[data_utils] Failed to read {base_path}: {e}")

    # 3. Any explicit later schema-version files (_v2, _v3, ...) written
    #    going forward by resolve_write_target().
    for v in _existing_versions(base_path):
        if v <= 1:
            continue  # v1 handled above (raw file or its recovery)
        path = _versioned_path(base_path, v)
        try:
            df = pd.read_csv(path, engine="python", on_bad_lines="warn")
        except Exception as e:
            print(f"[data_utils] Failed to read schema version {v} ({path}): {e}")
            continue
        if df.empty:
            continue
        df["_schema_version"] = v
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)
