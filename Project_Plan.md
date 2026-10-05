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
    quality.py              # cancellations, alerts, O-D trip plans
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

**Randomized poll timing (from 2026-10-05).** With polls fixed at :00/:30, WATA's 30/60-minute timetables meant every stop was seen at the same point in its schedule every day. That made which buses got measured depend on their delay (see Phase 4).
- **How each poll is timed.** Each run draws a uniformly random *phase* of the 30-minute cycle from OS entropy (`random.SystemRandom`). It then waits for that phase's next occurrence and records it as `poll_target`.
- **Why a phase, not a wait.** Runner start-up delay only changes how long the run waits, never where the poll lands. A random *wait* counted from start-up would under-sample the first minutes of every half-hour; simulated, about half the expected polls fell in the first 3 minutes.
- **Which polls count.** A poll counts as random only if its response landed within 2 minutes of its target.
- **The check.** `reliability.poll_timing_check` runs a Kolmogorov–Smirnov test on the realized phases over the 30- and 60-minute cycles each pipeline run. It warns at p < 0.001; the old :00/:30 polls give p = 0.
- **Cost.** The wait is runner minutes only (the repo is public), never API calls. A run that overruns the next dispatch is queued by the workflow's concurrency group.

**Calibration — superseded 2026-10-05.** A single call on 2026-09-19 suggested `is_real_time` only turns true in the last ~10 minutes before departure, implying a ~33% capture rate at 30-minute polling. Two weeks of collection show otherwise: **~99% of departures within the next 4 hours carry a real-time prediction**, and every scheduled trip is seen every day. How often we see a prediction is not the limit on reliability data. What matters is what those predictions can and cannot show (Phase 4).

## Phase 4 — Reliability analysis

**Method revised 2026-10-05: accuracy over a flattering number.** The 2026-09-20 rule took the last prediction made ≤5 minutes before departure. It was tested against two weeks of snapshots (9/22–10/5) and the archived GTFS, and replaced, for these reasons:

| Finding | Evidence | Consequence |
|---|---|---|
| No observed departures exist | A trip leaves a stop's listing when its *predicted* departure passes (never >0.5 min after) | Every number is a Transit/CAD-AVL prediction; the most accurate information is that disappearance, i.e. the prediction at the moment the bus leaves |
| Predictions drift later as the bus approaches | Same stop, ~30 min out → 0–5 min out: mean revision +1.5 min, p90 +7.6 | Longer-lead predictions understate lateness |
| Picking readings by prediction lead biases the sample | Mid-route late share >5 min: 12.4% (≤3 min), 7.5% (≤5), 5.6% (≤10) | Whether a bus is measured depended on its own delay |
| Fixed :00/:30 polls lock to the timetable | WATA timetables repeat every 30/60 min, so each stop was seen at the same point in its schedule daily; checks per minute ranged 119–1,461 | Fixed by randomized poll timing (Phase 3) |
| One time per stop | `arrival_time == departure_time` on every item, and in GTFS | Holding at timepoints is invisible; early running only as an upper bound |
| Predicted early ≠ departed early | 31% of ≤5-min predictions said >1 min early, but only ~2.5% of trips actually vanished >1 min before schedule | "Early" predictions mostly didn't happen |
| Origins are clamped, terminals are arrivals | First stop: 21.5% exactly 0, 0% early | Only mid-route stops are measured |
| Feed's scheduled times are GTFS rounded to the minute; predictions carry seconds | All 621,506 items within ±30 s of a `stop_times` row; `rt_trip_id` = GTFS `trip_id` | Delay is measured against the exact GTFS time in the archive in effect that day (`gtfs.snapshot_dir_for`) |

**The estimator (`metrics/reliability.py`).**
- **Each check.** Take each randomly timed poll *t*, each polled mid-route stop, and each departure scheduled there at *S* with *u = t − S* in [−10, 30] min. If the trip is still listed, its predicted departure is still ahead, so delay > *u*; otherwise delay ≤ *u*.
- **Why it's unbiased.** Departures are enumerated from GTFS, not from what was listed, so departed buses are counted. Selection depends only on the schedule, and *t* is random, so the share still listed at *u* estimates P(delay > *u*).
- **The fit.** That share is fitted as a non-increasing curve (pool-adjacent-violators, the standard estimator for "status at a random time" data).
- **What it reports.**
  - P(delay > 5 min), with a 95% CI from a bootstrap over service days.
  - P(delay > 10 min).
  - An early-departure upper bound.
  - On-time (−1/+5) as a range, never a single number.
  - `monotone_adjustment`, a check that the timing really is random.
- **What's excluded.**
  - Trip-days that were ever flagged cancelled (cancellations are counted separately in `quality.py`).
  - Trip-days with no real-time prediction (untracked trips vanish at their scheduled time and would read as on time).
- **Minimum data.** Groups with fewer than 300 checks or 10 service days get `insufficient_data`, not a number.
- **Tested.** A simulation recovers a known 20% late share to within 2 points.

**Indicative only, not publishable.** Run on the fixed-time polls, the estimator gives 16.4% of mid-route departures >5 min late (CI 13–19%) and ~2.5% leaving >1 min early. That data is the phase-locked kind this method exists to avoid, so these numbers only motivate the change; they are not results.

Outputs are written by `wata.pipeline` to `data/processed/lateness_network.csv`, `lateness_by_route.csv` and `lateness_curve.csv`, using randomized polls only.

**Do not publish reliability numbers before ~3 weeks of randomized polls (from 2026-10-05, so ~2026-10-26).** Anything less cannot separate a bad week from a bad route. The dashboard should ship in Phase 5 with the GTFS analyses and a visible "collecting since <date>" placeholder for the reliability panel.

Out of scope for now: headway adherence (needs consecutive-trip pairing) and origin pull-out lateness (origin predictions are clamped to the schedule).

## Phase 5 — Published dashboard

A published Artifact page (HTML), with analysis output exported to static JSON under `dashboard/data/` and baked in — no live API calls from the page, so the key is never exposed.

Panels: service level by route and day type; span and frequency heatmap by hour; a coverage map with equity overlay; the reliability panel; and rider-facing quality (active alerts, and `/v4/public/plan` results for a handful of representative origin–destination pairs such as a low-income neighborhood to the hospital, to W&M, and to the outlet-area job cluster).

**Accessibility is not reportable — dropped 2026-09-20.** WATA does not publish wheelchair data in any form: `wheelchair_accessible` is `0` for all 2,797 observed `schedule_items` (re-confirmed 2026-10-05: still `0` on all 611,816 items from two weeks of collection), `trips.txt` has no wheelchair column at all, and `stops.wheelchair_boarding` is null for all 614 stops. Per the GTFS spec `0` means *"no information"*, not *"not accessible"*, so any accessibility rate computed from it would be reporting absent data as a finding. The honest treatment is to state that WATA does not publish it.

Load the `dataviz` skill before writing any chart code, and `artifact-design` before writing the page. Every panel must state its data source, date range, and sample scope — a dashboard citing 100 sampled stops must say so where a reader sees the number, not in a footnote.

## Verification

- **GTFS pipeline:** loaded feed reports 12 routes with trips, 303 served stops, 1,840 trips; service-day expansion from `calendar_dates.txt` returns non-zero trips for a known weekday, Saturday, and Sunday.
- **Stop mapping (done):** all 303 served stops resolved to a `global_stop_id`, 0 unresolved, in 65 clustered `nearby_stops` calls; spot-checked against `stops.txt` via the exact `raw_stop_id` match.
- **Budget guard:** unit-test that the client refuses the call that would exceed the monthly ledger, and that it sustains ≤5 calls/minute.
- **Collector:** run one manual snapshot, confirm the NDJSON parses and that `scheduled_departure_time` values line up with GTFS for the same stop and time. Then confirm the Actions cron produces a full day's file unattended.
- **Reliability sanity:** on randomized polls, the raw still-listed shares should fall steadily with *u* (`monotone_adjustment` near zero), and checks should be spread roughly evenly across the minutes of *u*. Uneven counts or a large adjustment mean poll timing isn't random, not that buses behave oddly.
- **Access metrics:** cross-check that total population within any walkshed is plausible against the area's Census total — an implausibly high number usually means a projection or CRS error in the buffering.
- **Dashboard:** publish, open the URL, verify it renders at phone width and that every panel reports its date range.

## Principal risks

- **The free tier (1,500 calls/month) is the permanent ceiling, not a temporary one.** Swiftly is not being pursued, so there is no fallback path if this turns out to be too little. Phases 1, 2, and most of 5 are unaffected regardless.
- **Reliability is prediction-based, not observed.** A "departure" is the moment the feed's prediction says the bus left; there is no independent record of buses passing stops. Label this on the dashboard, not in a footnote.
- **Early running is not measurable.** The feed has one time per stop, so a bus holding at a timepoint is indistinguishable from one leaving early. Early departures are reported only as an upper bound, and on-time performance only as a range.
- **The estimator depends on random poll timing.** If polls stop landing at uniformly random phases (e.g. the random-phase wait is removed, or polls routinely miss their target), the estimates become biased again. `poll_timing_check` (the KS test on poll phases) is the direct check; `monotone_adjustment` is a secondary one.
- **GTFS feed churn.** Schedules change and the feed window is only a month; archiving is what protects past measurements.
- **Core+rotation sampling bias.** The ~48-stop core is fixed every poll; the rotating ~52 slots reach each of the other ~255 stops roughly once per 5-poll cycle (~1.5 hours). A reliability number for a rotating-only stop rests on far fewer observations than one for a core stop — state the sample composition wherever a reliability number appears, not just "100 stops."
