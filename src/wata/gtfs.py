"""Download, archive, and load the WATA GTFS static feed.

WATA republishes its schedule at a fixed URL with no versioning, and the
published window is short (about a month forward). Reliability analysis
needs the schedule that was in effect on the date a real-time snapshot was
taken, so every download is archived under its date rather than overwriting
the last one.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

GTFS_URL = "https://gtfs.wata.cadavl.com/WATA/GTFS/GTFS_WATA.zip"

REPO_ROOT = Path(__file__).resolve().parents[2]
GTFS_ARCHIVE_DIR = REPO_ROOT / "data" / "gtfs"

# calendar.txt is present in the WATA feed but always empty; every service
# day is instead defined as an exception in calendar_dates.txt.
GTFS_FILES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "shapes.txt",
    "transfers.txt",
)


def download_feed(dest_dir: Path | None = None, *, date: dt.date | None = None) -> Path:
    """Download the current WATA GTFS feed and archive it under today's date.

    Returns the directory the feed was extracted into
    (``data/gtfs/YYYY-MM-DD/``).
    """
    date = date or dt.date.today()
    dest_dir = dest_dir or GTFS_ARCHIVE_DIR / date.isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)

    resp = requests.get(GTFS_URL, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(dest_dir)

    return dest_dir


def latest_snapshot_dir(archive_dir: Path | None = None) -> Path:
    """Return the most recently archived feed directory."""
    archive_dir = archive_dir or GTFS_ARCHIVE_DIR
    snapshots = sorted(p for p in archive_dir.iterdir() if p.is_dir())
    if not snapshots:
        raise FileNotFoundError(
            f"No archived GTFS feed found under {archive_dir}. "
            "Run download_feed() first."
        )
    return snapshots[-1]


@dataclass
class GtfsFeed:
    """The WATA GTFS static feed as a set of DataFrames."""

    snapshot_date: dt.date
    agency: pd.DataFrame
    stops: pd.DataFrame
    routes: pd.DataFrame
    trips: pd.DataFrame
    stop_times: pd.DataFrame
    calendar: pd.DataFrame
    calendar_dates: pd.DataFrame
    shapes: pd.DataFrame

    @classmethod
    def load(cls, snapshot_dir: Path | None = None) -> "GtfsFeed":
        snapshot_dir = snapshot_dir or latest_snapshot_dir()
        snapshot_date = dt.date.fromisoformat(snapshot_dir.name)

        def read(name: str, **kwargs) -> pd.DataFrame:
            return pd.read_csv(snapshot_dir / name, dtype=str, **kwargs)

        stop_times = read("stop_times.txt")
        stop_times["stop_sequence"] = stop_times["stop_sequence"].astype(int)

        calendar_dates = read("calendar_dates.txt")
        calendar_dates["date"] = pd.to_datetime(
            calendar_dates["date"], format="%Y%m%d"
        )
        calendar_dates["exception_type"] = calendar_dates["exception_type"].astype(
            int
        )

        stops = read("stops.txt")
        stops["stop_lat"] = stops["stop_lat"].astype(float)
        stops["stop_lon"] = stops["stop_lon"].astype(float)

        return cls(
            snapshot_date=snapshot_date,
            agency=read("agency.txt"),
            stops=stops,
            routes=read("routes.txt"),
            trips=read("trips.txt"),
            stop_times=stop_times,
            calendar=read("calendar.txt"),
            calendar_dates=calendar_dates,
            shapes=read("shapes.txt"),
        )

    # -- derived views -----------------------------------------------

    def service_ids_on(self, date: dt.date) -> set[str]:
        """Service IDs active on a given calendar date.

        WATA's calendar.txt is empty, so this reads only
        calendar_dates.txt (exception_type 1 = added/active).
        """
        ts = pd.Timestamp(date)
        active = self.calendar_dates[
            (self.calendar_dates["date"] == ts)
            & (self.calendar_dates["exception_type"] == 1)
        ]
        return set(active["service_id"])

    def trips_on(self, date: dt.date) -> pd.DataFrame:
        """Trips (joined with routes) that run on a given calendar date."""
        service_ids = self.service_ids_on(date)
        trips = self.trips[self.trips["service_id"].isin(service_ids)]
        return trips.merge(self.routes, on="route_id", how="left")

    def served_stop_ids(self) -> set[str]:
        """Stop IDs that appear at least once in stop_times.txt.

        stops.txt defines more stops (614) than are actually served
        by any trip (303) — this is the set that matters for analysis.
        """
        return set(self.stop_times["stop_id"])
