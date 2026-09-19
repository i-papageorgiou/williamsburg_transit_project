"""Select which stops the real-time collector polls, once an API key exists.

WATA has 303 served stops. The Transit API's free tier allows ~1,500
calls/month, and /v4/public/stop_departures takes up to 100 stop IDs per
call — so polling all 303 stops every snapshot is 4 calls/snapshot,
leaving only ~12 snapshots/day, too sparse to measure anything.

Sampling 100 stops = 1 call/snapshot instead. A naive top-100-by-volume
selection would over-represent the transit center and the busiest
corridors, making the resulting on-time-performance numbers describe the
hub rather than the network riders actually experience on low-frequency
routes. This stratifies: guarantee a minimum number of stops per active
route first, then fill the remainder by volume.
"""

from __future__ import annotations

import pandas as pd

from wata.gtfs import GtfsFeed


def stop_route_departures(feed: GtfsFeed, service_id: str = "Full-Weekday") -> pd.DataFrame:
    """One row per (stop_id, route_id) with departure count on that service_id.

    A stop can appear multiple times if it's served by more than one route.
    """
    trips = feed.trips[feed.trips["service_id"] == service_id][
        ["trip_id", "route_id"]
    ]
    st = feed.stop_times.merge(trips, on="trip_id", how="inner")
    return (
        st.groupby(["stop_id", "route_id"])
        .size()
        .rename("departures")
        .reset_index()
    )


def select_stratified_stops(
    feed: GtfsFeed,
    *,
    n_total: int = 100,
    min_per_route: int = 6,
    service_id: str = "Full-Weekday",
) -> pd.DataFrame:
    """Pick up to n_total stops: at least min_per_route per active route
    (by that route's busiest stops), then fill the rest by total stop
    volume across all routes.

    Returns a DataFrame with stop_id, stop_name, stop_lat, stop_lon,
    total_departures, and covered_routes (comma-separated route_ids),
    sorted by total_departures descending.
    """
    by_route = stop_route_departures(feed, service_id=service_id)
    total_by_stop = (
        by_route.groupby("stop_id")["departures"].sum().rename("total_departures")
    )

    selected: list[str] = []
    selected_set: set[str] = set()

    active_routes = sorted(by_route["route_id"].unique())
    for route_id in active_routes:
        route_stops = (
            by_route[by_route["route_id"] == route_id]
            .sort_values("departures", ascending=False)
            .loc[lambda d: ~d["stop_id"].isin(selected_set), "stop_id"]
        )
        take = route_stops.head(min_per_route).tolist()
        for stop_id in take:
            if stop_id not in selected_set:
                selected.append(stop_id)
                selected_set.add(stop_id)

    remaining_slots = n_total - len(selected)
    if remaining_slots > 0:
        fill_candidates = (
            total_by_stop[~total_by_stop.index.isin(selected_set)]
            .sort_values(ascending=False)
            .head(remaining_slots)
        )
        selected.extend(fill_candidates.index.tolist())

    routes_covered = (
        by_route.groupby("stop_id")["route_id"]
        .apply(lambda ids: ",".join(sorted(ids)))
        .rename("covered_routes")
    )

    result = feed.stops[feed.stops["stop_id"].isin(selected)][
        ["stop_id", "stop_name", "stop_lat", "stop_lon"]
    ].copy()
    result = result.merge(total_by_stop, on="stop_id", how="left")
    result = result.merge(routes_covered, on="stop_id", how="left")
    return result.sort_values("total_departures", ascending=False).reset_index(drop=True)
