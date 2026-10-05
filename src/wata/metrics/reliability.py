"""Reliability metrics from the real-time snapshots: how late buses run.

What the feed can and cannot support (tested on 2026-09-22 to 2026-10-05,
see Project_Plan.md Phase 4) decides every choice here:

- There are no observed departures. A trip leaves a stop's listing when its
  *predicted* departure passes (never more than 0.5 min after), so the
  listing's best information is that disappearance: the prediction at the
  moment the bus leaves. Predictions made further out drift later as the
  bus approaches (+1.5 min on average from ~30 min out), so they
  understate lateness.
- Selecting readings by how close the prediction is to departure (e.g.
  "within 5 min") makes whether a bus is measured depend on its own delay.
  On the first two weeks the late share moved from 5.6% to 12.4% with the
  cutoff alone.
- Polls used to land at :00/:30, and WATA's timetables repeat every 30 or
  60 minutes, so each stop was seen at the same point in its schedule every
  day. Since 2026-10-05 the collector polls at a random phase of the cycle
  and records its `poll_target`. Only polls that landed on target are
  used, and poll_timing_check() tests that their phases really are uniform.
- The feed carries one time per stop (arrival == departure, in the schedule
  too), so a bus that arrives early and holds at a timepoint looks early.
  Early running is reported only as an upper bound.
- A trip's first stop is clamped to the schedule until the bus leaves, and
  its last stop is an arrival. Only mid-route stops are measured.

The estimator. At a randomly timed poll t, every scheduled departure at a
polled stop with scheduled time S has u = t - S. If the trip is still
listed there, its predicted departure is still ahead, so delay > u;
otherwise delay <= u. Because t is random, the share still listed among
departures checked at u estimates P(delay > u). Selection depends only on
the schedule, never on the delay. That share must fall as u grows, so it
is fitted as a non-increasing step function (pool-adjacent-violators, the
standard estimator for this kind of "status at a random time" data), and
the size of that adjustment is reported as a check that the timing really
is random.

rt_trip_id is WATA's GTFS trip_id, so each listing joins exactly to the
stop_times row of the archived feed in effect on its service date.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Callable

import numpy as np
import pandas as pd

from wata.gtfs import GtfsFeed, snapshot_dir_for
from wata.sampling import select_core_stops
from wata.stop_mapping import load_mapping

LATE_THRESHOLD_MINUTES = 5
EARLY_THRESHOLD_MINUTES = 1

# The range of u = poll time - scheduled time, in whole-minute bins, over
# which the still-listed share is estimated.
U_MIN_MINUTES = -10
U_MAX_MINUTES = 30

# Below either threshold a group gets no number, only "insufficient_data".
MIN_CHECKS = 300
MIN_SERVICE_DAYS = 10

BOOTSTRAP_REPLICATES = 1000

# Significance level for poll_timing_check. Truly random timing would trip
# a 1% test about once in 100 pipeline runs; :00/:30-style timing gives
# p-values near zero, so 0.1% loses no real detection power.
TIMING_ALPHA = 0.001

CHECK_COLUMNS = [
    "fetched_at",
    "service_date",
    "route_short_name",
    "rt_trip_id",
    "stop_id",
    "sched_sec",
    "gtfs_scheduled_departure",
    "u_min",
    "still_listed",
    "timepoint",
    "in_core",
]


@lru_cache(maxsize=None)
def _archived_feed(date: dt.date) -> GtfsFeed:
    return GtfsFeed.load(snapshot_dir_for(date))


def schedule_context(feed: GtfsFeed) -> pd.DataFrame:
    """Every stop_times row with what reliability needs to know about it:
    its position on the trip (origin, mid, terminal), whether it is a
    timepoint, and whether the collector's fixed core polls it.

    Keyed on the scheduled second of day as well as (trip, stop), because a
    looping trip can serve the same stop twice.
    """
    st = feed.stop_times[["trip_id", "stop_id", "stop_sequence", "departure_time", "timepoint"]].copy()
    hms = st["departure_time"].str.split(":", expand=True).astype(int)
    st["sched_sec"] = hms[0] * 3600 + hms[1] * 60 + hms[2]
    last_seq = st.groupby("trip_id")["stop_sequence"].transform("max")
    st["position"] = np.select(
        [st["stop_sequence"] == 1, st["stop_sequence"] == last_seq], ["origin", "terminal"], "mid"
    )
    st["timepoint"] = st["timepoint"].fillna("0").astype(int).astype(bool)
    st["in_core"] = st["stop_id"].isin(select_core_stops(feed))
    return st.drop(columns="departure_time").rename(columns={"trip_id": "rt_trip_id"})


def _gtfs_time(service_date: dt.date, sched_sec: pd.Series, tz) -> pd.Series:
    # GTFS times count from "noon minus 12h", which is midnight except on
    # DST-change days.
    noon = pd.Timestamp(service_date).tz_localize(tz).replace(hour=12)
    return noon - pd.Timedelta(hours=12) + pd.to_timedelta(sched_sec, unit="s")


def attach_schedule(
    items: pd.DataFrame,
    *,
    feed_for: Callable[[dt.date], GtfsFeed] = _archived_feed,
    mapping: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Join schedule items (wata.snapshots.schedule_items()) to the archived
    schedule for their service date. Items that don't match a stop_times
    row are dropped.

    The feed's scheduled times are GTFS times rounded to the whole minute,
    while its predictions carry seconds. So each item is matched to the
    stop_times row within 30 s, and delay is measured against the exact
    GTFS time. Measuring against the rounded one would add up to ±30 s of
    error to every delay.
    """
    mapping = mapping if mapping is not None else load_mapping()
    to_stop_id = {global_id: stop_id for stop_id, global_id in mapping.items()}

    sched = items["scheduled_departure_time"]
    items = items.assign(
        service_date=sched.dt.date,
        feed_sched_sec=sched.dt.hour * 3600 + sched.dt.minute * 60 + sched.dt.second,
        stop_id=items["global_stop_id"].map(to_stop_id),
        _row=np.arange(len(items)),
    )

    contexts: dict[int, pd.DataFrame] = {}  # one per distinct feed, not per day
    joined = []
    for service_date, day in items.groupby("service_date"):
        feed = feed_for(service_date)
        if id(feed) not in contexts:
            contexts[id(feed)] = schedule_context(feed)
        day = day.merge(contexts[id(feed)], on=["rt_trip_id", "stop_id"], how="inner")
        offset = (day["feed_sched_sec"] - day["sched_sec"]).abs()
        day = day[offset <= 30].assign(_offset=offset).sort_values("_offset").drop_duplicates("_row")
        day["gtfs_scheduled_departure"] = _gtfs_time(service_date, day["sched_sec"], sched.dt.tz)
        joined.append(day)
    if not joined:
        return items.iloc[0:0]

    scheduled = pd.concat(joined, ignore_index=True).drop(columns=["_row", "_offset", "feed_sched_sec"])
    scheduled["delay_min"] = (
        scheduled["departure_time"] - scheduled["gtfs_scheduled_departure"]
    ).dt.total_seconds() / 60
    return scheduled


def departure_checks(
    items: pd.DataFrame,
    polls: pd.DataFrame,
    *,
    feed_for: Callable[[dt.date], GtfsFeed] = _archived_feed,
    mapping: dict[str, str] | None = None,
) -> pd.DataFrame:
    """One row per (randomly timed poll, scheduled mid-route departure at a
    stop that poll asked about) with u = poll time - scheduled time in
    [U_MIN_MINUTES, U_MAX_MINUTES], and whether the trip was still listed.

    `items` is wata.snapshots.schedule_items() output and `polls` is
    wata.snapshots.polled_stops() output, both over the same snapshots.

    The denominator comes from the GTFS schedule, not from what was listed,
    so departures that had already gone are counted. Trip-days are left out
    if the trip was ever flagged cancelled that day, or never had a
    real-time prediction that day. An untracked trip drops off at its
    scheduled time whatever the bus does, which would read as on time.
    """
    mapping = mapping if mapping is not None else load_mapping()
    to_stop_id = {global_id: stop_id for stop_id, global_id in mapping.items()}

    polls = polls[polls["randomized"]]
    if polls.empty or items.empty:
        return pd.DataFrame(columns=CHECK_COLUMNS)
    polls = polls.assign(
        stop_id=polls["global_stop_id"].map(to_stop_id), service_date=polls["fetched_at"].dt.date
    )

    scheduled = attach_schedule(items, feed_for=feed_for, mapping=mapping)
    trip_day = scheduled.groupby(["service_date", "rt_trip_id"]).agg(
        tracked=("is_real_time", "any"), flagged=("is_cancelled", "any")
    )
    measurable = trip_day[trip_day["tracked"] & ~trip_day["flagged"]].reset_index()[
        ["service_date", "rt_trip_id"]
    ]
    listing_keys = ["fetched_at", "service_date", "rt_trip_id", "stop_id", "sched_sec"]
    listed = scheduled[listing_keys].drop_duplicates().assign(still_listed=True)

    checks = []
    for service_date, day_polls in polls.groupby("service_date"):
        feed = feed_for(service_date)
        running = feed.trips_on(service_date)[["trip_id", "route_short_name"]].rename(
            columns={"trip_id": "rt_trip_id"}
        )
        context = schedule_context(feed).merge(running, on="rt_trip_id")
        context = context[context["position"] == "mid"]
        context["gtfs_scheduled_departure"] = _gtfs_time(
            service_date, context["sched_sec"], polls["fetched_at"].dt.tz
        )
        day = day_polls[["fetched_at", "service_date", "stop_id"]].merge(context, on="stop_id")
        day["u_min"] = (day["fetched_at"] - day["gtfs_scheduled_departure"]).dt.total_seconds() / 60
        checks.append(day[day["u_min"].between(U_MIN_MINUTES, U_MAX_MINUTES)])

    checks = pd.concat(checks, ignore_index=True).merge(measurable, on=["service_date", "rt_trip_id"])
    checks = checks.merge(listed, on=listing_keys, how="left")
    checks["still_listed"] = checks["still_listed"].fillna(False).astype(bool)
    return checks[CHECK_COLUMNS]


def poll_timing_check(polls: pd.DataFrame) -> pd.DataFrame:
    """Test that randomized polls really are spread uniformly over the
    30- and 60-minute cycles WATA's timetables repeat on. This is the
    assumption the whole estimator rests on.

    One Kolmogorov-Smirnov test per cycle on the polls' phases (seconds
    past the cycle start). A p-value below TIMING_ALPHA means poll timing
    is not uniform and lateness estimates should not be trusted until the
    cause is found. `polls` is wata.snapshots.polled_stops() output.
    """
    fetched = polls.loc[polls["randomized"], "fetched_at"].drop_duplicates()
    # Unit-safe: the datetime resolution (ns, us) varies with how it was built.
    epoch_seconds = (fetched - pd.Timestamp(0, tz="UTC")).dt.total_seconds().to_numpy()
    rows = []
    for cycle_minutes in (30, 60):
        statistic, p_value = _ks_uniform((epoch_seconds % (cycle_minutes * 60)) / (cycle_minutes * 60))
        rows.append(
            {
                "cycle_minutes": cycle_minutes,
                "polls": len(fetched),
                "ks_statistic": round(statistic, 4),
                "p_value": round(p_value, 4),
                "uniform": bool(p_value >= TIMING_ALPHA) if len(fetched) else None,
            }
        )
    return pd.DataFrame(rows)


def _ks_uniform(x: np.ndarray) -> tuple[float, float]:
    """One-sample KS test of `x` against Uniform(0, 1), with the asymptotic
    Kolmogorov p-value (accurate for the hundreds of polls this sees)."""
    n = len(x)
    if n == 0:
        return np.nan, np.nan
    x = np.sort(x)
    i = np.arange(1, n + 1)
    d = max((i / n - x).max(), (x - (i - 1) / n).max())
    k = np.arange(1, 101)
    p = 2 * np.sum((-1) ** (k - 1) * np.exp(-2 * k**2 * n * d**2))
    return float(d), float(min(max(p, 0.0), 1.0))


def _nonincreasing(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted least-squares fit of a non-increasing sequence
    (pool-adjacent-violators)."""
    blocks: list[list[float]] = []  # [mean, weight, length]
    for v, w in zip(values, weights):
        blocks.append([v, w, 1])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    return np.concatenate([np.full(n, v) for v, _, n in blocks])


def _bin_counts(checks: pd.DataFrame) -> pd.DataFrame:
    """Checks and still-listed counts per whole-minute u bin."""
    u = checks["u_min"].round().astype(int)
    counts = checks["still_listed"].groupby(u).agg(["size", "sum"])
    counts.index.name = "u"
    return counts.rename(columns={"size": "checks", "sum": "listed"})


def _fitted_at(counts: pd.DataFrame) -> pd.Series:
    """The fitted P(delay > u) for every whole minute in range. A bin with
    no checks takes the value of the nearest checked bin below it, the
    conservative (higher) side for a non-increasing curve."""
    counts = counts[counts["checks"] > 0]
    fitted = _nonincreasing((counts["listed"] / counts["checks"]).to_numpy(), counts["checks"].to_numpy())
    full = pd.Series(fitted, index=counts.index).reindex(range(U_MIN_MINUTES, U_MAX_MINUTES + 1))
    return full.ffill().bfill()


def lateness_curve(checks: pd.DataFrame) -> pd.DataFrame:
    """Per whole-minute u: checks, the raw still-listed share, and the
    fitted P(delay > u)."""
    if checks.empty:
        return pd.DataFrame(columns=["u", "checks", "listed", "raw_share", "p_delay_over_u"])
    counts = _bin_counts(checks)
    curve = counts.assign(raw_share=(counts["listed"] / counts["checks"]).round(4))
    curve["p_delay_over_u"] = _fitted_at(counts).reindex(curve.index).round(4)
    return curve.reset_index()


def lateness_summary(
    checks: pd.DataFrame, by: list[str] | None = None, *, seed: int = 0
) -> pd.DataFrame:
    """Lateness per group (network-wide if `by` is empty), read off the
    fitted curve:

    - late_share = P(delay > 5 min), with a 95% CI from a bootstrap over
      service days (checks on the same day share weather, traffic and
      incidents, so they are not independent).
    - late10_share = P(delay > 10 min).
    - early_upper = P(left > 1 min early). An upper bound, since a bus
      holding at a timepoint looks early.
    - ontime_low / ontime_high: the on-time (-1/+5 min) range those imply.
    - monotone_adjustment: how far the raw shares had to move to fall
      steadily (check-weighted mean). Near zero when poll timing is random
      and samples are large; a large value is a warning, not noise.

    Groups under MIN_CHECKS checks or MIN_SERVICE_DAYS days get
    status="insufficient_data" and no numbers.
    """
    by = by or []
    rng = np.random.default_rng(seed)
    groups = checks.groupby(by) if by else [((), checks)]

    rows = []
    for key, group in groups:
        key = key if isinstance(key, tuple) else (key,)
        days = group["service_date"].nunique()
        row = dict(zip(by, key))
        row.update(
            checks=len(group),
            service_days=days,
            core_share=round(group["in_core"].mean(), 3) if len(group) else np.nan,
            first_date=group["service_date"].min(),
            last_date=group["service_date"].max(),
        )
        if len(group) < MIN_CHECKS or days < MIN_SERVICE_DAYS:
            row["status"] = "insufficient_data"
            rows.append(row)
            continue

        counts = _bin_counts(group)
        curve = _fitted_at(counts)
        raw = counts["listed"] / counts["checks"]
        adjustment = np.average((raw - curve.reindex(counts.index)).abs(), weights=counts["checks"])

        late = curve[LATE_THRESHOLD_MINUTES]
        not_early = curve[-EARLY_THRESHOLD_MINUTES]
        row.update(
            status="ok",
            late_share=round(late, 3),
            **dict(zip(("late_ci_low", "late_ci_high"), _bootstrap_late_ci(group, rng))),
            late10_share=round(curve[10], 3),
            early_upper=round(1 - not_early, 3),
            ontime_low=round(not_early - late, 3),
            ontime_high=round(1 - late, 3),
            monotone_adjustment=round(adjustment, 4),
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _bootstrap_late_ci(group: pd.DataFrame, rng: np.random.Generator) -> tuple[float, float]:
    u = group["u_min"].round().astype(int)
    per_day = (
        group.assign(u=u)
        .groupby(["service_date", "u"])["still_listed"]
        .agg(["size", "sum"])
        .unstack("u", fill_value=0)
    )
    checks = per_day["size"].to_numpy()
    listed = per_day["sum"].to_numpy()
    bins = per_day["size"].columns

    estimates = []
    for _ in range(BOOTSTRAP_REPLICATES):
        days = rng.integers(0, len(per_day), size=len(per_day))
        counts = pd.DataFrame(
            {"checks": checks[days].sum(axis=0), "listed": listed[days].sum(axis=0)}, index=bins
        )
        estimates.append(_fitted_at(counts)[LATE_THRESHOLD_MINUTES])
    low, high = np.quantile(estimates, [0.025, 0.975])
    return round(low, 3), round(high, 3)
