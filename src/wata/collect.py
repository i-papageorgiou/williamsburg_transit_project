"""Real-time snapshot collector: polls /v4/public/stop_departures for the
stratified 100-stop sample and appends the raw response to a daily NDJSON
file.

Not runnable yet — needs TRANSIT_API_KEY (Phase 0, pending) and the cached
stop-id mapping (built once via wata.stop_mapping once a key exists). The
structure is here now so .github/workflows/collect.yml has something to
call the moment the key lands; wiring it up is then a `TRANSIT_API_KEY`
secret away, not new code.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from pathlib import Path

from wata.gtfs import GtfsFeed, REPO_ROOT
from wata.sampling import select_stratified_stops
from wata.stop_mapping import load_mapping
from wata.transit_api import TransitApiClient

RAW_SNAPSHOT_DIR = REPO_ROOT / "data" / "raw"


def snapshot_path(when: dt.datetime | None = None) -> Path:
    when = when or dt.datetime.now()
    RAW_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return RAW_SNAPSHOT_DIR / f"{when.date().isoformat()}.ndjson.gz"


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
            "Run wata.stop_mapping.resolve_cluster_mapping() once, against "
            "the sampled stops from wata.sampling.select_stratified_stops(), "
            "and save it before running the collector."
        )

    feed = GtfsFeed.load()
    sample = select_stratified_stops(feed)
    global_stop_ids = [
        mapping[stop_id] for stop_id in sample["stop_id"] if stop_id in mapping
    ]
    missing = len(sample) - len(global_stop_ids)
    if missing:
        print(f"Warning: {missing} sampled stops have no cached mapping and will be skipped.")

    client = TransitApiClient(api_key)
    record = collect_one_snapshot(client, global_stop_ids)
    append_snapshot(record)
    print(f"Collected snapshot for {len(global_stop_ids)} stops at {record['fetched_at']}")


if __name__ == "__main__":
    run()
