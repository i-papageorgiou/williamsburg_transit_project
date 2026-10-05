"""Rider-facing quality metrics from the real-time snapshots.

Currently: trip cancellations. These need no extra API calls; `is_cancelled`
already arrives on every schedule item the collector polls, and the
collector keeps cancelled items (remove_cancelled=False) for exactly this.

Two things about how WATA's feed reports cancellations shape the logic
here (both seen in the first two weeks of collection):

- A trip's flag changes over time. Trips are typically flagged about an
  hour before departure, and a flagged trip is sometimes reinstated later
  (five Route 15 trips on 2026-10-03 were flagged at 17:30 and running
  again by 18:00). So a trip counts as cancelled only if its *latest*
  observation is cancelled; trips flagged and later restored are counted
  separately as reinstated, not as cancellations.
- `is_cancelled` is trip-level only. A stop closed by a detour (e.g. the
  2026-10-02 Arts Festival closure of stop 1036) is announced in alerts
  but never flags any trip, so this undercounts lost service at the stop
  level.

Trip coverage is near-complete: since 2026-09-22 every scheduled trip
(580 weekday, 460 Saturday, 284 Sunday) has been observed at least once a
day, so trips_observed is a sound denominator.
"""

from __future__ import annotations

import pandas as pd

TRIP_KEYS = ["service_date", "route_short_name", "rt_trip_id"]


def trip_cancellations(items: pd.DataFrame) -> pd.DataFrame:
    """One row per (service date, route, trip) observed, with its final
    cancellation status and how much notice riders got.

    `items` is wata.snapshots.schedule_items() output. The service date is
    the local date of the scheduled departure; WATA's last trip leaves
    before 23:00, so no trip spans midnight.

    Only trips whose earliest observed departure is at or before the
    latest poll are kept. Low-frequency stops list trips days ahead (10
    departures can reach into next week), and a trip that hasn't run yet
    has no final status.

    notice_minutes is measured from the trip's earliest *observed*
    scheduled departure, which may be a mid-route stop if the first stop
    wasn't polled. A negative value means the flag appeared only after
    that departure time.
    """
    items = items.assign(service_date=items["scheduled_departure_time"].dt.date)

    # One status per trip per poll. Every stop in a poll agrees, but take
    # any() so a single flagged stop is never lost.
    per_poll = (
        items.groupby(TRIP_KEYS + ["fetched_at"])["is_cancelled"]
        .any()
        .reset_index()
        .sort_values("fetched_at")
    )
    status = per_poll.groupby(TRIP_KEYS).agg(
        polls_observed=("fetched_at", "size"),
        cancelled=("is_cancelled", "last"),
        ever_flagged=("is_cancelled", "any"),
    )
    first_flagged = (
        per_poll[per_poll["is_cancelled"]].groupby(TRIP_KEYS)["fetched_at"].min().rename("first_flagged_at")
    )
    trip_info = items.groupby(TRIP_KEYS).agg(
        direction_id=("direction_id", "first"),
        headsign=("headsign", "first"),
        first_scheduled_departure=("scheduled_departure_time", "min"),
    )

    trips = trip_info.join(status).join(first_flagged).reset_index()
    trips = trips[trips["first_scheduled_departure"] <= items["fetched_at"].max()].copy()
    trips["reinstated"] = trips["ever_flagged"] & ~trips["cancelled"]
    notice = trips["first_scheduled_departure"] - trips["first_flagged_at"]
    trips["notice_minutes"] = (notice.dt.total_seconds() / 60).round(1).where(trips["cancelled"])
    return trips.drop(columns="ever_flagged").sort_values(
        ["service_date", "route_short_name", "first_scheduled_departure"]
    )


def cancellations_by_day(trips: pd.DataFrame) -> pd.DataFrame:
    """Per (service date, route): trips observed, cancelled, and reinstated."""
    daily = (
        trips.groupby(["service_date", "route_short_name"])
        .agg(
            trips_observed=("rt_trip_id", "size"),
            trips_cancelled=("cancelled", "sum"),
            trips_reinstated=("reinstated", "sum"),
        )
        .reset_index()
    )
    daily["cancelled_share"] = (daily["trips_cancelled"] / daily["trips_observed"]).round(3)
    daily["weekday"] = pd.to_datetime(daily["service_date"]).dt.day_name()
    return daily


def recurring_cancellations(trips: pd.DataFrame, *, min_days: int = 2) -> pd.DataFrame:
    """Trips cancelled on at least `min_days` service dates.

    WATA's rt_trip_id names a scheduled trip within a service pattern
    (e.g. Full-WeekdayFW_15_2577091_3423007_1807 is the same 18:07 Route 15
    trip every weekday), so a repeating ID means the same scheduled run is
    being dropped day after day, the signature of a missing vehicle or
    driver rather than one-off breakdowns.
    """
    per_trip = trips.groupby(["route_short_name", "rt_trip_id"]).agg(
        direction_id=("direction_id", "first"),
        headsign=("headsign", "first"),
        scheduled_time=("first_scheduled_departure", lambda s: s.dt.strftime("%H:%M").min()),
        days_observed=("service_date", "nunique"),
        days_cancelled=("cancelled", "sum"),
        median_notice_minutes=("notice_minutes", "median"),
    )
    cancelled_dates = trips[trips["cancelled"]].groupby(["route_short_name", "rt_trip_id"])["service_date"]
    per_trip["first_cancelled"] = cancelled_dates.min()
    per_trip["last_cancelled"] = cancelled_dates.max()
    per_trip["cancelled_weekdays"] = cancelled_dates.agg(
        lambda s: ",".join(sorted({d.strftime("%a") for d in s}, key=_weekday_order))
    )
    per_trip["cancel_rate"] = (per_trip["days_cancelled"] / per_trip["days_observed"]).round(2)

    recurring = per_trip[per_trip["days_cancelled"] >= min_days].reset_index()
    return recurring.sort_values(["route_short_name", "scheduled_time"]).reset_index(drop=True)


def _weekday_order(abbr: str) -> int:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(abbr)
