"""Real-time snapshot collector: polls /v4/public/stop_departures for a
core+rotating stop sample (see wata.sampling) and appends the raw response
to a daily NDJSON file.

Not runnable yet in production — needs the cached stop-id mapping (built
once via wata.stop_mapping.resolve_cluster_mapping once a key exists) and
TRANSIT_API_KEY set. The structure is here now so
.github/workflows/collect.yml has something to call the moment both exist.

Design (see wata.sampling module docstring for the reasoning): a fixed
~48-stop core guarantees every (route, direction) pair is represented every
poll, for trend continuity; the rest of each 100-ID call rotates through
the other ~255 served stops across successive polls, so the whole network
gets swept into the dataset over a few days at no extra call cost. This
requires a persisted poll counter (`poll_index`) so consecutive runs (each
a fresh checkout in CI) advance the rotation instead of resetting it.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from pathlib import Path

from wata.gtfs import GtfsFeed, REPO_ROOT
from wata.sampling import poll_stop_ids, select_core_stops
from wata.stop_mapping import load_mapping
from wata.transit_api import TransitApiClient

RAW_SNAPSHOT_DIR = REPO_ROOT / "data" / "raw"
POLL_INDEX_PATH = REPO_ROOT / "data" / "processed" / "poll_index.json"


def snapshot_path(when: dt.datetime | None = None) -> Path:
    when = when or dt.datetime.now()
    RAW_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return RAW_SNAPSHOT_DIR / f"{when.date().isoformat()}.ndjson.gz"


def next_poll_index(path: Path | None = None) -> int:
    """Read the persisted poll counter, increment it, and save it back.

    Each call advances the rotation by one; a fresh repo checkout with no
    counter file yet starts at 0.
    """
    path = path or POLL_INDEX_PATH
    current = 0
    if path.exists():
        current = json.loads(path.read_text()).get("poll_index", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"poll_index": current + 1}))
    return current


def collect_one_snapshot(client: TransitApiClient, global_stop_ids: list[str]) -> dict:
    """Fetch one stop_departures response and return it tagged with the
    time it was fetched, ready to append to the daily NDJSON file.
    """
    response = client.stop_departures(global_stop_ids)
    return {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "global_stop_ids": global_stop_ids,
        "response": response,
    }


def append_snapshot(record: dict, path: Path | None = None) -> None:
    path = path or snapshot_path()
    with gzip.open(path, "at", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run() -> None:
    api_key = os.environ.get("TRANSIT_API_KEY")
    if not api_key:
        raise SystemExit(
            "TRANSIT_API_KEY is not set. This collector is not usable until "
            "Phase 0 (Transit API key request) is granted — see Project_Plan.md."
        )

    mapping = load_mapping()
    if not mapping:
        raise SystemExit(
            "No stop-id mapping cached at data/processed/stop_id_mapping.json. "
            "Run wata.stop_mapping.resolve_cluster_mapping() once and save it "
            "before running the collector."
        )

    feed = GtfsFeed.load()
    core = select_core_stops(feed)
    poll_index = next_poll_index()
    sample = poll_stop_ids(feed, poll_index, core_stop_ids=core)

    global_stop_ids = [mapping[stop_id] for stop_id in sample if stop_id in mapping]
    missing = len(sample) - len(global_stop_ids)
    if missing:
        print(f"Warning: {missing} sampled stops have no cached mapping and will be skipped.")

    client = TransitApiClient(api_key)
    record = collect_one_snapshot(client, global_stop_ids)
    record["poll_index"] = poll_index
    append_snapshot(record)
    print(
        f"Collected snapshot #{poll_index} for {len(global_stop_ids)} stops "
        f"at {record['fetched_at']}"
    )


if __name__ == "__main__":
    run()
