# WATA Transit Effectiveness Analysis

An analysis of how well the Williamsburg Area Transit Authority (WATA) bus
network serves riders in Williamsburg, VA — service level, coverage/equity,
reliability, and rider-facing quality. See [Project_Plan.md](Project_Plan.md)
for the full plan and rationale.

## Status

**The real-time collector is live.** Transit API access is granted (free
tier: 5 calls/min, 1,500/month — enforced at 1,400 as a safety margin); WATA's
Swiftly GTFS-RT feed is not being pursued, so the Transit API is the only
real-time data source for this project. `wata.collect` runs on GitHub Actions
every 30 minutes (`.github/workflows/collect.yml`), deciding live against the
archived GTFS calendar whether anything is actually running before spending a
call — so it correctly skips outside service hours (spans vary sharply by day
type: weekday 05:54–22:57, Saturday 05:55–21:24, Sunday 07:55–18:04) rather
than wasting calls or needing per-day-type cron entries.

First live snapshot (2026-09-20) confirmed the design end to end: 100 sampled
stops → 1 call, 1,359 schedule items returned, and **29.4% carried a live
`is_real_time` reading** — close to the ~33% capture rate predicted from
calibration (real-time predictions only populate in roughly the last 10
minutes before departure, so a 30-minute poll interval only catches a
minority of trips' live state; this is a disclosed limitation, not a bug).

Implemented:

- Phase 1 — GTFS download/archive pipeline (`wata.gtfs`)
- Phase 2 — Service-level metrics: trip volume, span, headway by route
  (`wata.metrics.service`)
- Phase 2 — Coverage: 800m walksheds around served stops, tagged by
  service frequency tier (`wata.metrics.access`)
- Phase 3 — Core+rotation stop sample (`wata.sampling`): a fixed ~48-stop
  core guarantees every (route, direction) pair every poll; the remaining
  ~52 slots rotate through the other ~255 served stops so the full network
  gets sampled over a few days at no extra call cost
- Phase 3 — Transit API client with a persisted monthly call budget and a
  rate limiter, tested against a mocked session/fake clock (`wata.transit_api`)
- Phase 3 — WATA `stop_id` → Transit `global_stop_id` mapping: **all 303
  served stops resolved, 0 unresolved**, via 65 clustered `nearby_stops`
  calls, cached to `data/processed/stop_id_mapping.json` (`wata.stop_mapping`)
- Phase 3 — Live snapshot collector (`wata.collect`) and its GitHub Actions
  cron (`.github/workflows/collect.yml`) — running unattended
- `.github/workflows/refresh-gtfs.yml` — weekly GTFS re-archive, no key needed

Not yet built: the published dashboard, `wata.metrics.quality` (alerts,
accessibility — free from data already being collected), and
`wata.metrics.reliability` (OTP/delay distributions — needs ~3 weeks of
accumulated snapshots before numbers are meaningful, though the parser can be
built against day-1 data now). The Census API key (for the equity join in
`wata.metrics.access`) has been requested but not yet received by email — a
separate, free, instant credential, unrelated to the Transit API key above.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Known issue (macOS):** pip-installed native wheels (pandas, geopandas, …) in
this venv can hang indefinitely on first `import` due to a macOS Gatekeeper
check on the compiled `.so` files (`spctl -a -v` reports them `rejected` —
ad-hoc signed, no Team ID) rather than failing fast. If imports hang: a
system reboot has resolved this before. As a workaround, an existing Anaconda
Python install's own pandas (not this venv's) imports fine, suggesting it's
specific to these pip-built wheels rather than a system-wide policy — using
`/path/to/anaconda3/bin/python3.11` directly with `pip install -e .` run
against that interpreter instead is a viable fallback if the venv stays stuck.

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

32 tests run against the real archived feed or mocked HTTP/clock/filesystem —
none require a live API key or network access.

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
- The Transit API's `stop_departures` endpoint returns Transit-internal
  `global_stop_id`s (e.g. `WATAVA:4126`), not WATA's own `stop_id`s (e.g.
  `0_2577091`) — its `raw_stop_id` field is an exact match to WATA's GTFS
  `stop_id` in practice, used directly for the mapping.
- `max_num_departures` on `stop_departures` defaults to 3 but goes up to
  10 at no extra call cost — always requested at 10.
