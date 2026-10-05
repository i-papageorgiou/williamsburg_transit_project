"""Read the collector's raw snapshot files (data/raw/*.ndjson.gz) back into
one flat table of schedule items, for the real-time metrics to build on.

Each snapshot is one stop_departures response. The parse path, verified
against real payloads, is route_departures[] -> merged_itineraries[] ->
schedule_items[]; everything above schedule_items is copied down onto each
row so a row stands alone (one route, at one stop, in one poll).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

from wata.collect import RAW_SNAPSHOT_DIR, SERVICE_TZ

SCHEDULE_ITEM_COLUMNS = [
    "fetched_at",
    "poll_index",
    "global_stop_id",
    "route_short_name",
    "direction_id",
    "headsign",
    "rt_trip_id",
    "trip_search_key",
    "scheduled_departure_time",
    "departure_time",
    "is_real_time",
    "is_cancelled",
]


def iter_snapshots(paths: Iterable[Path] | None = None) -> Iterator[dict]:
    """Yield every snapshot record, file by file, in file-name (date) order."""
    paths = sorted(paths) if paths is not None else sorted(RAW_SNAPSHOT_DIR.glob("*.ndjson.gz"))
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def schedule_items(snapshots: Iterable[dict]) -> pd.DataFrame:
    """One row per schedule item per poll.

    Times stay as tz-aware datetimes in SERVICE_TZ, so service dates and
    hours of day come out in WATA's local time rather than UTC.
    """
    rows = []
    for snap in snapshots:
        fetched_at = dt.datetime.fromisoformat(snap["fetched_at"])
        for rd in snap["response"].get("route_departures", []):
            for itinerary in rd.get("merged_itineraries", []):
                variants = itinerary.get("itineraries") or [{}]
                for item in itinerary.get("schedule_items", []):
                    rows.append(
                        {
                            "fetched_at": fetched_at,
                            "poll_index": snap.get("poll_index"),
                            "global_stop_id": rd["global_stop_id"],
                            "route_short_name": rd["route_short_name"],
                            "direction_id": itinerary.get("direction_id"),
                            "headsign": variants[0].get("direction_headsign"),
                            "rt_trip_id": item.get("rt_trip_id"),
                            "trip_search_key": item.get("trip_search_key"),
                            "scheduled_departure_time": item["scheduled_departure_time"],
                            "departure_time": item["departure_time"],
                            "is_real_time": item["is_real_time"],
                            "is_cancelled": item["is_cancelled"],
                        }
                    )

    items = pd.DataFrame(rows, columns=SCHEDULE_ITEM_COLUMNS)
    items["fetched_at"] = pd.to_datetime(items["fetched_at"], utc=True).dt.tz_convert(SERVICE_TZ)
    for col in ("scheduled_departure_time", "departure_time"):
        items[col] = pd.to_datetime(items[col], unit="s", utc=True).dt.tz_convert(SERVICE_TZ)
    return items
