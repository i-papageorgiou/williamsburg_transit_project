"""Run the GTFS-only metrics (Phase 1-2) and write outputs to data/processed/
and dashboard/data/.

Usage: python -m wata.pipeline
"""

from __future__ import annotations

import json
from pathlib import Path

from wata.gtfs import GtfsFeed, REPO_ROOT
from wata.metrics.access import coverage_summary, walksheds
from wata.metrics.service import all_route_spans, service_summary

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

    dashboard_payload = {
        "snapshot_date": feed.snapshot_date.isoformat(),
        "routes": json.loads(summary.to_json(orient="records")),
        "coverage_km2_by_tier": coverage,
        "reliability": {
            "status": "not_yet_available",
            "note": (
                "Requires several weeks of real-time polling once a "
                "Transit API or WATA Swiftly key is granted (Phase 0)."
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
    print(f"Wrote {DASHBOARD_DATA_DIR / 'gtfs_metrics.json'}")
    print(f"Coverage by frequency tier (km^2): {coverage}")


if __name__ == "__main__":
    main()
