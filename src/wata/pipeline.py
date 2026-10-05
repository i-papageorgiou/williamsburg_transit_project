"""Run the GTFS-only metrics (Phase 1-2) and the snapshot-based cancellation
and lateness metrics, and write outputs to data/processed/ and
dashboard/data/.

Usage: python -m wata.pipeline
"""

from __future__ import annotations

import json
from pathlib import Path

from wata.gtfs import GtfsFeed, REPO_ROOT
from wata.metrics.access import coverage_summary, walksheds
from wata.metrics.quality import cancellations_by_day, recurring_cancellations, trip_cancellations
from wata.metrics.reliability import (
    departure_checks,
    lateness_curve,
    lateness_summary,
    poll_timing_check,
)
from wata.metrics.service import all_route_spans, service_summary
from wata.sampling import poll_stop_ids, select_core_stops
from wata.snapshots import iter_snapshots, polled_stops, schedule_items

PROCESSED_DIR = REPO_ROOT / "data" / "processed"
DASHBOARD_DATA_DIR = REPO_ROOT / "dashboard" / "data"


def main() -> None:
    feed = GtfsFeed.load()
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)

    summary = service_summary(feed)
    spans = all_route_spans(feed)
    summary.to_csv(PROCESSED_DIR / "route_service_summary.csv", index=False)
    spans.to_csv(PROCESSED_DIR / "route_spans_by_service_id.csv", index=False)

    sheds = walksheds(feed)
    sheds.to_file(PROCESSED_DIR / "walksheds.geojson", driver="GeoJSON")

    coverage = coverage_summary(feed)

    # The core+rotation sample the real-time collector polls (see
    # wata.sampling). Recomputed here so it stays in sync with the current
    # feed rather than going stale between GTFS refreshes.
    core = select_core_stops(feed)
    first_poll = poll_stop_ids(feed, 0, core_stop_ids=core)
    sample_summary = feed.stops[feed.stops["stop_id"].isin(first_poll)][
        ["stop_id", "stop_name", "stop_lat", "stop_lon"]
    ].copy()
    sample_summary["in_core"] = sample_summary["stop_id"].isin(core)
    sample_summary.to_csv(PROCESSED_DIR / "stop_sample.csv", index=False)

    snapshots = list(iter_snapshots())
    items = schedule_items(snapshots)
    trips = trip_cancellations(items)
    cancellations_by_day(trips).to_csv(PROCESSED_DIR / "cancellations_by_day.csv", index=False)
    recurring_cancellations(trips).to_csv(PROCESSED_DIR / "recurring_cancellations.csv", index=False)

    # Uses only randomly timed polls (collected from 2026-10-05 on), so
    # these stay empty until they accumulate; a route needs 10+ service
    # days before it gets a number. Not published to the dashboard yet.
    polls = polled_stops(snapshots)
    timing = poll_timing_check(polls)
    timing.to_csv(PROCESSED_DIR / "poll_timing_check.csv", index=False)
    checks = departure_checks(items, polls)
    lateness_summary(checks).to_csv(PROCESSED_DIR / "lateness_network.csv", index=False)
    lateness_summary(checks, ["route_short_name"]).to_csv(
        PROCESSED_DIR / "lateness_by_route.csv", index=False
    )
    lateness_curve(checks).to_csv(PROCESSED_DIR / "lateness_curve.csv", index=False)

    dashboard_payload = {
        "snapshot_date": feed.snapshot_date.isoformat(),
        "routes": json.loads(summary.to_json(orient="records")),
        "coverage_km2_by_tier": coverage,
        "reliability": {
            "status": "not_yet_available",
            "note": (
                "Requires several weeks of real-time polling against the "
                "Transit API (Phase 0, granted; WATA's own Swiftly "
                "GTFS-RT feed is not being pursued)."
            ),
        },
    }
    (DASHBOARD_DATA_DIR / "gtfs_metrics.json").write_text(
        json.dumps(dashboard_payload, indent=2)
    )

    print(f"Loaded GTFS snapshot dated {feed.snapshot_date}")
    print(f"Wrote {PROCESSED_DIR / 'route_service_summary.csv'}")
    print(f"Wrote {PROCESSED_DIR / 'route_spans_by_service_id.csv'}")
    print(f"Wrote {PROCESSED_DIR / 'walksheds.geojson'}")
    print(f"Wrote {PROCESSED_DIR / 'stop_sample.csv'}")
    print(f"Wrote {PROCESSED_DIR / 'cancellations_by_day.csv'}")
    print(f"Wrote {PROCESSED_DIR / 'recurring_cancellations.csv'}")
    print(f"Wrote {PROCESSED_DIR / 'lateness_network.csv'}, lateness_by_route.csv, lateness_curve.csv "
          f"({len(checks)} departure checks from randomly timed polls)")
    print(f"Wrote {PROCESSED_DIR / 'poll_timing_check.csv'}")
    if (timing["uniform"] == False).any():  # noqa: E712 - None means no polls yet
        print("WARNING: randomized poll times are not uniform over the cycle; "
              "do not trust the lateness estimates until this is explained.")
    print(f"Wrote {DASHBOARD_DATA_DIR / 'gtfs_metrics.json'}")
    print(f"Coverage by frequency tier (km^2): {coverage}")


if __name__ == "__main__":
    main()
