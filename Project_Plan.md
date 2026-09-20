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

1. **Transit API key** — **granted** (free tier: 5 calls/min, 1,500/month). Confirmed working against `available_networks`, `nearby_stops`, and `stop_departures`; WATA is present as network `WATA|Williamsburg`.
2. **WATA GTFS-Realtime key (Swiftly)** — **not being pursued.** The Transit API free tier is the only real-time data source for this project; every design decision in Phase 3–4 below assumes no vehicle-positions feed and no historical endpoint ever arrive, and is built to make the free tier's 1,500 calls/month go as far as possible rather than to be a fallback for something better.

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

## Phase 3 — Real-time collection (Transit API only — every call has to earn its place)

With no Swiftly fallback, the free tier's 1,500 calls/month is the permanent ceiling on reliability data, not a temporary one. Everything here is designed around extracting the most information per call rather than the most calls per day.

**Step 1 — stop-id mapping (done).** `/v4/public/nearby_stops` returns Transit-internal `global_stop_id`s (e.g. `WATAVA:4126`), not WATA's own `stop_id`s (e.g. `0_2577091`) — confirmed live; the `raw_stop_id` field in its response is an exact match to WATA's GTFS `stop_id` and is matched on directly, falling back to nearest-coordinate only when it isn't. Naive mapping is one call per stop (303 calls, 20% of the monthly budget). Instead, `wata.stop_mapping` clusters stop coordinates (900m radius — `nearby_stops`'s own `max_distance` is capped at 1500m server-side) and calls at each cluster's centroid, resolving all 303 served stops in **65 calls**, a one-time cost. Cached to `data/processed/stop_id_mapping.json`, never rebuilt.

**Step 2 — sample selection: `wata.sampling`, core + rotation.** Two things about the network changed the design from the original "flat stratified 100-stop sample" plan:

- *A `stop_departures` call returns every route serving the queried stop*, and WATA's network overlaps heavily at its hub — 4 stops cover all 12 active routes in both directions. Guaranteeing route coverage is nearly free; it does not need 72 of the 100 slots the original plan reserved for it.
- *A call costs the same (1 call) whether it carries 60 IDs or 100.* A permanently fixed 100-stop sample leaves ~200 of the 303 served stops with zero data for the entire collection period, for no budget saving over rotating them in.

So each poll's 100 IDs are split into a **fixed core** (~48 stops, `select_core_stops`: ≥2 stops per (route, direction) pair, so every route's both directions are represented every poll, for day-over-day trend continuity) plus a **rotating remainder** (`poll_stop_ids`: the other ~52 slots cycle through the remaining ~255 stops in 5 groups). Over 5 polls — about 2.5 hours at 30-minute cadence — every one of the 303 served stops has been sampled at least once, at zero extra call cost over the flat design.

**Step 3 — squeeze each call.** `stop_departures` takes `max_num_departures` (default 3, **max 10**) — set to 10 always. Same call, same budget cost, up to 3.3× the schedule items back per merged itinerary, which is real signal (more upcoming trips per route/direction observed per poll), not padding. `remove_cancelled` stays `false` (the default) — a cancellation is a reliability signal to keep, not noise to filter. `merge_platform_stops` was checked and dropped: WATA's GTFS gives every stop a 1:1 parent station, so there's no multi-platform station to merge. `include_stops_and_shapes`/`stop_detailed` stay `false` — no budget effect, but keeps every archived response smaller.

**Step 4 — collector.** `wata.collect` polls the core+rotation sample every 30 minutes, but the cron schedule itself is deliberately wider than any service span — `should_poll_now()` checks the archived GTFS calendar live and exits *before spending a call* whenever nothing is running. This matters because spans differ sharply by day type (weekday 05:54–22:57, Saturday 05:55–21:24, Sunday 07:55–18:04) and cron can't see `calendar_dates.txt`, so baking spans into cron would drift at every schedule change and break twice a year at DST.

Resulting spend: weekday ~34 polls/day, Saturday ~31/day, Sunday ~20/day ≈ 221/week, **≈960/month** — well under the cap, leaving room for `plan()` calls and mapping rebuilds. `wata.transit_api`'s `CallBudget` also now enforces 1,400 rather than the full 1,500 as a safety margin, since the ledger is only accurate if the collector's commit actually lands — a failed push under-counts real usage against Transit's own server-side limit, which won't forgive that.

A persisted `poll_index` (`data/processed/poll_index.json`) advances the rotation across runs — incremented only on an actual poll, never on a run skipped by `should_poll_now()` — since each GitHub Actions run is a fresh checkout. Appends raw responses as gzipped NDJSON, one file per day, committed back by the workflow, which retries with a rebase on a push conflict rather than silently dropping an already-paid-for snapshot.

**Calibration (done) — and a real limitation this exposes.** A live call at the transit-center hub (Saturday 9:04pm ET; most routes had already ended Saturday service — only Route 15 was still running, consistent with Phase 2's finding that it alone carries meaningful weekend evening service) showed `is_real_time` only turns `true` in roughly the last ~10 minutes before departure: Route 15's two imminent trips read `is_real_time=true` at 1.4 and 6.5 minutes out, while every other returned item — including Route 15's own later trips — was still schedule-only (`is_real_time=false`) up to 20+ hours out.

That means a 30-minute poll interval has only roughly a **~33% chance** (real-time window ÷ poll interval ≈ 10/30) of ever catching any given trip's one real-time-flagged reading — the rest get scheduled-time-only, which is not a delay measurement. This is disclosed as a real limitation rather than solved: tightening the interval for high-frequency routes specifically would need a second call per poll on top of the core+rotation call, and the budget has no room for that while also sustaining every day of the month (a 20-minute interval alone would exceed 1,500 calls/month by day 29). The reliability panel should report this expected ~33% capture rate next to its OTP numbers, and treat a longer collection period (more weeks) as the way to recover statistical power — not a shorter interval the budget can't afford.

## Phase 4 — Reliability analysis

`metrics/reliability.py` reduces the snapshot stream to per-departure records: for each scheduled departure, take the **last observation before its predicted departure time** as the best estimate of actual departure, and compute delay against the archived schedule for that date. Departures seen with `is_cancelled` become cancellations; departures that appear in the schedule but never in any snapshot become unobserved (distinguish these from cancellations — with 30-minute polling, some are simply missed).

Report on-time performance (standard −1/+5 minute window), delay distributions by route and hour, headway adherence on the frequent routes, and real-time coverage (`is_real_time` share, which tells you how often riders actually get a live prediction rather than a schedule guess — expected to land around the ~50% ceiling found in Phase 3's calibration, not 100%).

**Do not publish reliability numbers before ~3 weeks of collection.** Anything less cannot separate a bad week from a bad route. The dashboard should ship in Phase 5 with the GTFS analyses and a visible "collecting since <date>" placeholder for the reliability panel.

## Phase 5 — Published dashboard

A published Artifact page (HTML), with analysis output exported to static JSON under `dashboard/data/` and baked in — no live API calls from the page, so the key is never exposed.

Panels: service level by route and day type; span and frequency heatmap by hour; a coverage map with equity overlay; the reliability panel; and rider-facing quality (active alerts, wheelchair-accessible trip share from `stop_departures`, and `/v4/public/plan` results for a handful of representative origin–destination pairs such as a low-income neighborhood to the hospital, to W&M, and to the outlet-area job cluster).

Load the `dataviz` skill before writing any chart code, and `artifact-design` before writing the page. Every panel must state its data source, date range, and sample scope — a dashboard citing 100 sampled stops must say so where a reader sees the number, not in a footnote.

## Verification

- **GTFS pipeline:** loaded feed reports 12 routes with trips, 303 served stops, 1,840 trips; service-day expansion from `calendar_dates.txt` returns non-zero trips for a known weekday, Saturday, and Sunday.
- **Stop mapping (done):** all 303 served stops resolved to a `global_stop_id`, 0 unresolved, in 65 clustered `nearby_stops` calls; spot-checked against `stops.txt` via the exact `raw_stop_id` match.
- **Budget guard:** unit-test that the client refuses the call that would exceed the monthly ledger, and that it sustains ≤5 calls/minute.
- **Collector:** run one manual snapshot, confirm the NDJSON parses and that `scheduled_departure_time` values line up with GTFS for the same stop and time. Then confirm the Actions cron produces a full day's file unattended.
- **Reliability sanity:** delay distribution should center near zero with a right tail. A symmetric distribution centered well below zero means the "last prediction" extraction is wrong, not that buses run early.
- **Access metrics:** cross-check that total population within any walkshed is plausible against the area's Census total — an implausibly high number usually means a projection or CRS error in the buffering.
- **Dashboard:** publish, open the URL, verify it renders at phone width and that every panel reports its date range.

## Principal risks

- **The free tier (1,500 calls/month) is the permanent ceiling, not a temporary one.** Swiftly is not being pursued, so there is no fallback path if this turns out to be too little. Phases 1, 2, and most of 5 are unaffected regardless.
- **Reliability is prediction-based, not observed, and only ~33% of trips get even that.** Calibration showed `is_real_time` populates only in roughly the last 10 minutes before departure; at 30-minute polling, only about a third of trips will have a real-time-flagged reading at all, the rest just a schedule guess. Label both caveats on the dashboard, not just a footnote.
- **GTFS feed churn.** Schedules change and the feed window is only a month; archiving is what protects past measurements.
- **Core+rotation sampling bias.** The ~48-stop core is fixed every poll; the rotating ~52 slots reach each of the other ~255 stops roughly once per 5-poll cycle (~1.5 hours). A reliability number for a rotating-only stop rests on far fewer observations than one for a core stop — state the sample composition wherever a reliability number appears, not just "100 stops."
