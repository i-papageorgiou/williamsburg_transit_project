"""Service-level metrics: span, frequency, and trip volume by route.

Pure GTFS analysis. Needs no API key and no real-time data — these are
descriptive facts about what WATA schedules, not about how well it runs.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from wata.gtfs import GtfsFeed, time_to_minutes

# Representative dates used to characterize each service pattern. The
# WATA feed only guarantees a ~1 month forward window, so callers should
# pick dates inside the currently archived snapshot's covered range.
SERVICE_ID_LABELS = {
    "Full-Weekday": "Weekday (full)",
    "CWF-Weekday": "Weekday (W&M academic calendar)",
    "Full-Sa": "Saturday",
    "Full-Su": "Sunday",
}


def trips_per_route_by_service(feed: GtfsFeed) -> pd.DataFrame:
    """Trip counts per route, broken out by service_id (day type)."""
    merged = feed.trips.merge(feed.routes, on="route_id", how="left")
    counts = (
        merged.groupby(["route_id", "route_short_name", "route_long_name", "service_id"])
        .size()
        .rename("trip_count")
        .reset_index()
    )
    counts["service_label"] = counts["service_id"].map(SERVICE_ID_LABELS).fillna(
        counts["service_id"]
    )
    return counts.sort_values(["route_id", "service_id"])


def route_span(feed: GtfsFeed, route_id: str, service_id: str) -> dict:
    """First departure, last departure, and span (in minutes) for a route/service_id."""
    trip_ids = feed.trips.loc[
        (feed.trips["route_id"] == route_id) & (feed.trips["service_id"] == service_id),
        "trip_id",
    ]
    st = feed.stop_times[feed.stop_times["trip_id"].isin(trip_ids)]
    if st.empty:
        return {
            "route_id": route_id,
            "service_id": service_id,
            "first_departure": None,
            "last_departure": None,
            "span_minutes": None,
            "trip_count": 0,
        }

    minutes = st["departure_time"].map(time_to_minutes)
    return {
        "route_id": route_id,
        "service_id": service_id,
        "first_departure": st.loc[minutes.idxmin(), "departure_time"],
        "last_departure": st.loc[minutes.idxmax(), "departure_time"],
        "span_minutes": round(minutes.max() - minutes.min(), 1),
        "trip_count": trip_ids.nunique(),
    }


def all_route_spans(feed: GtfsFeed) -> pd.DataFrame:
    """route_span() for every (route_id, service_id) combination present in the feed."""
    combos = feed.trips[["route_id", "service_id"]].drop_duplicates()
    rows = [route_span(feed, r.route_id, r.service_id) for r in combos.itertuples()]
    spans = pd.DataFrame(rows)
    routes = feed.routes[["route_id", "route_short_name", "route_long_name"]]
    spans = spans.merge(routes, on="route_id", how="left")
    spans["service_label"] = spans["service_id"].map(SERVICE_ID_LABELS).fillna(
        spans["service_id"]
    )
    return spans.sort_values(["route_id", "service_id"])


def headway_by_hour(feed: GtfsFeed, route_id: str, service_id: str) -> pd.DataFrame:
    """Median scheduled headway (minutes between consecutive departures) per hour
    of day, computed at the route's most-served stop for that route/service_id.

    Using a single representative stop avoids conflating multiple branches
    or directions, which would understate true headway.
    """
    trip_ids = feed.trips.loc[
        (feed.trips["route_id"] == route_id) & (feed.trips["service_id"] == service_id),
        "trip_id",
    ]
    st = feed.stop_times[feed.stop_times["trip_id"].isin(trip_ids)]
    if st.empty:
        return pd.DataFrame(columns=["hour", "median_headway_minutes", "departures"])

    busiest_stop = st["stop_id"].value_counts().idxmax()
    at_stop = st[st["stop_id"] == busiest_stop].copy()
    at_stop["minutes"] = at_stop["departure_time"].map(time_to_minutes)
    at_stop = at_stop.sort_values("minutes")
    at_stop["hour"] = (at_stop["minutes"] // 60).astype(int) % 24
    at_stop["headway"] = at_stop["minutes"].diff()

    grouped = (
        at_stop.groupby("hour")
        .agg(
            median_headway_minutes=("headway", "median"),
            departures=("minutes", "count"),
        )
        .reset_index()
    )
    return grouped


def service_summary(feed: GtfsFeed) -> pd.DataFrame:
    """One row per route: total weekday trips, weekend trips, and span,
    for a quick cross-route comparison of how much service each route gets.
    """
    spans = all_route_spans(feed)
    weekday = spans[spans["service_id"] == "Full-Weekday"].set_index("route_id")
    saturday = spans[spans["service_id"] == "Full-Sa"].set_index("route_id")
    sunday = spans[spans["service_id"] == "Full-Su"].set_index("route_id")

    routes = feed.routes.set_index("route_id")
    summary = pd.DataFrame(index=routes.index)
    summary["route_short_name"] = routes["route_short_name"]
    summary["route_long_name"] = routes["route_long_name"]
    summary["weekday_trips"] = weekday["trip_count"].reindex(summary.index).fillna(0).astype(int)
    summary["weekday_span_minutes"] = weekday["span_minutes"].reindex(summary.index)
    summary["saturday_trips"] = saturday["trip_count"].reindex(summary.index).fillna(0).astype(int)
    summary["sunday_trips"] = sunday["trip_count"].reindex(summary.index).fillna(0).astype(int)
    summary["has_weekend_service"] = (summary["saturday_trips"] > 0) | (
        summary["sunday_trips"] > 0
    )
    return summary.reset_index().sort_values("weekday_trips", ascending=False)
