# Analyzing WATA Bus Service Effectiveness

## Context

Williamsburg Area Transit Authority (WATA) runs 12 active bus routes across the Williamsburg / James City County / upper York County area. There is no public accounting of how well that service actually performs — whether buses run on time, how much service each neighborhood gets, and who can actually reach jobs and services by bus.

This project builds that accounting from open data and publishes it as a dashboard anyone can open: WATA staff, city and county planners, W&M researchers, and riders.

The goal is a defensible, reproducible picture of effectiveness across four dimensions: **reliability**, **service level and span**, **coverage and access equity**, and **rider-facing quality**.

## What the data actually supports

Research confirmed the following, and it shapes every decision below.

**WATA GTFS (static schedule) — free, public, no key.**
`https://gtfs.wata.cadavl.com/WATA/GTFS/GTFS_WATA.zip`. Verified live today. 13 routes defined (12 with trips — route `12A` currently has none), 614 stops of which **303 are actually served**, 1,840 trips, 26k stop_times. Service span 05:54–22:57. `calendar.txt` is empty — all service is defined in `calendar_dates.txt` against four service IDs: `Full-Weekday`, `Full-Sa`, `Full-Su`, `CWF-Weekday`. **The feed only covers ~1 month forward (currently 2026-09-18 → 2026-10-19), so it must be re-downloaded and archived on a schedule.**

**Transit API — key required, and it is the binding constraint.**
Base URL `https://external.transitapp.com`, auth via an `apiKey` **header**. Relevant v4 endpoints:

| Endpoint | Use here |
|---|---|
| `/v4/public/available_networks` | Confirm WATA is in Transit's system; get its network name |
| `/v4/public/nearby_stops` | Resolve WATA GTFS `stop_id` → Transit `global_stop_id` |
| `/v4/public/stop_departures` | **The core call.** Up to **100 stop IDs per request**; returns `departure_time` vs `scheduled_departure_time`, plus `is_real_time`, `is_cancelled`, `wheelchair_accessible`, `rt_trip_id` |
| `/v4/public/schedule_for_dates`, `/trips_for_dates` | Scheduled times (no real-time applied) — we use GTFS directly instead |
| `/v4/public/plan` | Multimodal itineraries for the origin–destination quality checks |

Two limits matter enormously:

1. **There is no historical endpoint and no vehicle-positions endpoint.** Reliability data does not exist until we collect it ourselves, snapshot by snapshot. Nothing meaningful about on-time performance can be produced on day one.
2. **Free tier is 5 calls/minute and 1,500 calls/month.** That is the entire reliability budget. Every call must be justified.

**Honest caveat to carry into the writeup:** polling `stop_departures` yields *prediction-based* on-time performance — the last predicted departure before a bus leaves, not a confirmed observed departure. It is a well-established proxy and defensible, but it is a proxy. It should be labeled as such on the dashboard.

## Phase 0 — Request access (do first; it blocks Phase 3)

Both requests have approval lead time, so send them before writing code. Work in Phases 1–2 proceeds without them.

1. **Transit API key** — submit the form at `https://transitapp.com/partners/apis`. In the request, describe the project as non-commercial transit research on WATA and **ask explicitly for a higher call allowance than the free tier**, explaining the polling need. A granted increase removes the single biggest limitation in this plan.
2. **WATA GTFS-Realtime key** — WATA publishes GTFS-RT through Swiftly at `https://api.goswift.ly/real-time/wata/gtfs-rt-vehicle-positions` and `.../gtfs-rt-trip-updates` (returns 403 without credentials; key request at `https://goswift.ly/realtime-api-key`). Email WATA directly (gowata.org) as a local researcher. **If this is granted it is strictly better than the Transit API for reliability**: real vehicle positions, the full 303-stop network, no 1,500-call ceiling, 30-second polling. Treat Transit API polling as the fallback path, and design the collector so either source can feed the same reliability tables.

## Phase 1 — Repo scaffold and GTFS pipeline

The repo is currently empty (only `.gitignore`, `.gitattributes`). Build from scratch in Python.

```
pyproject.toml              # pandas, geopandas, requests, duckdb, protobuf (for GTFS-RT)
src/wata/
  gtfs.py                   # download, archive by date, load into DataFrames
  transit_api.py            # client: apiKey header, 5-req/min limiter, call-budget ledger
  stop_mapping.py           # GTFS stop_id -> Transit global_stop_id
  collect.py                # snapshot collector (Transit API and/or Swiftly GTFS-RT)
  metrics/
    service.py              # span, frequency, trips per day
    access.py               # walksheds + ACS equity join
    reliability.py          # OTP / headway adherence from snapshots
    quality.py              # alerts, accessibility, O-D trip plans
data/
  gtfs/YYYY-MM-DD/          # archived feed snapshots (committed)
  raw/YYYY-MM-DD.ndjson.gz  # raw API snapshots (committed)
  processed/*.parquet
dashboard/                  # published artifact page + its JSON data
.github/workflows/
  collect.yml               # cron collector
  refresh-gtfs.yml          # weekly GTFS archive
```

`gtfs.py` must **archive each downloaded feed under its date**. Reliability analysis compares observed departures against the schedule *that was in effect that day* — if the schedule is overwritten, past measurements become uninterpretable. This is the most common way projects like this silently produce wrong numbers.

Handle the two feed quirks explicitly: derive service days from `calendar_dates.txt` (`calendar.txt` is empty and will yield zero service if used naively), and drop `12A` from route-level outputs while noting it as defined-but-unserved.

## Phase 2 — GTFS-only analyses (no API key needed; start immediately)

These produce most of the dashboard and require zero API calls.

**Service level and span** (`metrics/service.py`) — per route and per service ID (`Full-Weekday`, `Full-Sa`, `Full-Su`, `CWF-Weekday`): trips per day, headway by hour of day, first and last departure, evening and weekend span. Note the service concentration up front — route 15 (Colonial) has 536 trips against route 11 (Lackey)'s 54, a 10× spread that is itself a finding. `CWF-Weekday` vs `Full-Weekday` quantifies how much of the network depends on the W&M academic calendar.

**Coverage and access equity** (`metrics/access.py`) — 800m walk buffers around the 303 served stops, joined to Census ACS block groups (population, income, vehicle availability, race) pulled from the Census API, plus LEHD LODES for job counts. Weight each buffer by service intensity (departures per day) rather than treating a 1-trip stop the same as the transit center. Output: share of population and of zero-vehicle households within walk access to frequent vs infrequent service, and the geographic gaps.

Use simple buffers, not a routing engine — at this network's scale the added rigor of true walk-network isochrones does not change the conclusions. If genuinely needed later, `r5py` is the upgrade path.

## Phase 3 — Real-time collection (starts when a key lands)

**Step 1 — confirm and map (one time, budget-critical).**
Call `/v4/public/available_networks` with Williamsburg coordinates to confirm WATA is present. Then resolve WATA GTFS stops to Transit `global_stop_id` values — these are Transit-internal IDs (`STM:17989` style), **not** WATA's stop IDs, so the join must be built.

Naive mapping is one `nearby_stops` call per stop: 303 calls, **20% of the monthly free budget burned before any data is collected**. Instead, cluster the 303 stop coordinates and call `nearby_stops` at cluster centroids with a large `max_distance`, since each call returns many stops. This should resolve the network in roughly 40–60 calls. **Cache the mapping to a committed JSON file** so it is never rebuilt. Verify coverage and fall back to individual calls only for stops the clustered pass misses.

**Step 2 — sample selection.** 303 stops is 4 calls per full-network snapshot; at 1,500 calls/month that allows only ~12 snapshots/day, too sparse to measure anything. Instead select **100 stops = 1 call per snapshot**, stratified: guarantee ≥6 stops per active route (72 stops, so low-frequency routes are not erased), then fill the remaining 28 with the highest-volume stops not already included. Pure top-100-by-volume would over-sample the transit center hub (stop `0_2577091`, 1,502 departures) and the high-frequency corridors, making the OTP number unrepresentative of the routes riders complain about.

**Step 3 — collector.** Poll the 100-stop call every 20 minutes across the 05:54–22:57 span: ~51 snapshots/day, ~1,480/month — fits the free tier with a thin margin for the mapping and quality calls. Append raw responses as gzipped NDJSON, one file per day. Run it from **GitHub Actions cron**, not a laptop, with the key in repo secrets and snapshots committed back; Actions cron drifts by a few minutes, which is harmless at 20-minute granularity. `transit_api.py` must track cumulative monthly calls and refuse to exceed the budget rather than getting the key throttled.

If the Swiftly key comes through, switch the collector to GTFS-RT trip updates over all 303 stops at 30-second polling and retire the sampling scheme entirely.

## Phase 4 — Reliability analysis

`metrics/reliability.py` reduces the snapshot stream to per-departure records: for each scheduled departure, take the **last observation before its predicted departure time** as the best estimate of actual departure, and compute delay against the archived schedule for that date. Departures seen with `is_cancelled` become cancellations; departures that appear in the schedule but never in any snapshot become unobserved (distinguish these from cancellations — with 20-minute polling, some are simply missed).

Report on-time performance (standard −1/+5 minute window), delay distributions by route and hour, headway adherence on the frequent routes, and real-time coverage (`is_real_time` share, which tells you how often riders actually get a live prediction rather than a schedule guess).

**Do not publish reliability numbers before ~3 weeks of collection.** Anything less cannot separate a bad week from a bad route. The dashboard should ship in Phase 5 with the GTFS analyses and a visible "collecting since <date>" placeholder for the reliability panel.

## Phase 5 — Published dashboard

A published Artifact page (HTML), with analysis output exported to static JSON under `dashboard/data/` and baked in — no live API calls from the page, so the key is never exposed.

Panels: service level by route and day type; span and frequency heatmap by hour; a coverage map with equity overlay; the reliability panel; and rider-facing quality (active alerts, wheelchair-accessible trip share from `stop_departures`, and `/v4/public/plan` results for a handful of representative origin–destination pairs such as a low-income neighborhood to the hospital, to W&M, and to the outlet-area job cluster).

Load the `dataviz` skill before writing any chart code, and `artifact-design` before writing the page. Every panel must state its data source, date range, and sample scope — a dashboard citing 100 sampled stops must say so where a reader sees the number, not in a footnote.

## Verification

- **GTFS pipeline:** loaded feed reports 12 routes with trips, 303 served stops, 1,840 trips; service-day expansion from `calendar_dates.txt` returns non-zero trips for a known weekday, Saturday, and Sunday.
- **Stop mapping:** ≥95% of the 100 sampled stops resolve to a `global_stop_id`; spot-check five by comparing the API's returned stop name and coordinates against `stops.txt`.
- **Budget guard:** unit-test that the client refuses the call that would exceed the monthly ledger, and that it sustains ≤5 calls/minute.
- **Collector:** run one manual snapshot, confirm the NDJSON parses and that `scheduled_departure_time` values line up with GTFS for the same stop and time. Then confirm the Actions cron produces a full day's file unattended.
- **Reliability sanity:** delay distribution should center near zero with a right tail. A symmetric distribution centered well below zero means the "last prediction" extraction is wrong, not that buses run early.
- **Access metrics:** cross-check that total population within any walkshed is plausible against the area's Census total — an implausibly high number usually means a projection or CRS error in the buffering.
- **Dashboard:** publish, open the URL, verify it renders at phone width and that every panel reports its date range.

## Principal risks

- **The key request may be denied or capped at free tier.** Phases 1, 2, and most of 5 are unaffected. Reliability is the only casualty, and the Swiftly path is the mitigation — pursue both in Phase 0.
- **Reliability is prediction-based, not observed.** Label it on the dashboard. Swiftly GTFS-RT removes this caveat if granted.
- **GTFS feed churn.** Schedules change and the feed window is only a month; archiving is what protects past measurements.
- **Sampling bias.** 100 of 303 stops, stratified by route, is a real limitation. State the sample scope wherever a reliability number appears.
