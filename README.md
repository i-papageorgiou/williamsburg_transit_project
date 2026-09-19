# WATA Transit Effectiveness Analysis

An analysis of how well the Williamsburg Area Transit Authority (WATA) bus
network serves riders in Williamsburg, VA — service level, coverage/equity,
reliability, and rider-facing quality. See [Project_Plan.md](Project_Plan.md)
for the full plan and rationale.

## Status

**Phase 0 (API access requests) is in progress.** The Transit API and
WATA's Swiftly GTFS-RT key are both pending, which blocks the real-time
reliability analysis (Phase 3–4). Everything that only needs WATA's public
GTFS static feed does **not** depend on those keys and is implemented now:

- Phase 1 — GTFS download/archive pipeline (`wata.gtfs`)
- Phase 2 — Service-level metrics: trip volume, span, headway by route
  (`wata.metrics.service`)
- Phase 2 — Coverage: 800m walksheds around served stops, tagged by
  service frequency tier (`wata.metrics.access`)

Not yet started: real-time collection, reliability metrics, equity join
against Census data, and the published dashboard (Phases 3–5).

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
