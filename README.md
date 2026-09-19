# WATA Transit Effectiveness Analysis

An analysis of how well the Williamsburg Area Transit Authority (WATA) bus
network serves riders in Williamsburg, VA — service level, coverage/equity,
reliability, and rider-facing quality. See [Project_Plan.md](Project_Plan.md)
for the full plan and rationale.

## Status

**Phase 0 (API access requests) is in progress.** The Transit API and
WATA's Swiftly GTFS-RT key are both pending, which blocks actually
*running* the real-time collector. Everything that can be built and
tested without a live key is implemented now:

- Phase 1 — GTFS download/archive pipeline (`wata.gtfs`)
- Phase 2 — Service-level metrics: trip volume, span, headway by route
  (`wata.metrics.service`)
- Phase 2 — Coverage: 800m walksheds around served stops, tagged by
  service frequency tier (`wata.metrics.access`)
- Phase 3 (prep) — Stratified 100-stop sample for the collector to poll,
  guaranteeing every active route is represented rather than letting the
  busiest stops dominate (`wata.sampling`)
- Phase 3 (prep) — Transit API client with a persisted monthly call
  budget and a rate limiter, tested against a mocked session/fake clock
  (`wata.transit_api`)
- Phase 3 (prep) — WATA stop_id → Transit `global_stop_id` mapping logic,
  clustering stop coordinates to resolve the mapping in a handful of
  calls instead of one per stop; tested against a fake API response
  (`wata.stop_mapping`)
- Phase 3 (skeleton) — snapshot collector (`wata.collect`) and its
  GitHub Actions cron (`.github/workflows/collect.yml`); refuses to run
  without `TRANSIT_API_KEY` and a cached stop mapping, both pending Phase 0
- `.github/workflows/refresh-gtfs.yml` — weekly GTFS re-archive, no key
  needed

Blocked on Phase 0: actually running the collector, reliability metrics
computed from its output, and the equity join against Census data (a
separate, free, instant key — not blocked, just not yet requested). The
published dashboard currently ships the GTFS-only metrics with a
"reliability: collecting since —" placeholder.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Running the pipeline

```bash
# Refresh the GTFS feed (archives under data/gtfs/<today>/)
PYTHONPATH=src python -c "from wata.gtfs import download_feed; download_feed()"

# Compute service-level and coverage metrics, write to data/processed/
# and dashboard/data/
PYTHONPATH=src python -m wata.pipeline
```

## Tests

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

All 28 tests run against the real archived feed or mocked
HTTP/clock — none require a live API key.

## Data notes

- WATA's GTFS feed (`https://gtfs.wata.cadavl.com/WATA/GTFS/GTFS_WATA.zip`)
  only covers roughly a month forward and is republished at a fixed URL
  with no versioning — every download is archived under
  `data/gtfs/YYYY-MM-DD/` rather than overwritten, so past analyses stay
  reproducible against the schedule that was actually in effect.
- `calendar.txt` in the feed is always empty; all service days come from
  `calendar_dates.txt` (service IDs `Full-Weekday`, `CWF-Weekday`,
  `Full-Sa`, `Full-Su`).
- `routes.txt` currently defines 13 routes but route `12A` has zero
  scheduled trips — treated as defined-but-unserved throughout, not
  dropped, so its absence stays visible.
- `stops.txt` defines 614 stops; only 303 are served by any trip.
