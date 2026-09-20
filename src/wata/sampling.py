"""Select which stops the real-time collector polls, once an API key exists.

WATA has 303 served stops, but a single stop_departures call is capped at
100 stop IDs and costs the same (1 call) whether it carries 60 IDs or 100 -
so the design problem isn't "how many stops fit," it's "which ones are
worth the slot."

Two findings from the live GTFS data drive this:

1. A single stop query returns EVERY route serving that stop, and WATA's
   network overlaps heavily at its hub - 4 stops cover all 12 active
   routes in both directions. Guaranteeing "coverage" is nearly free; the
   real risk is that a route looks fine at the hub but degrades further
   out, so a *few* points per route+direction (not just the hub) still
   matter for catching that.

2. Because a call's cost doesn't depend on which 100 IDs it carries, a
   permanently fixed 100-stop sample leaves ~200 stops with literally zero
   data for the entire collection period, for no budget saving over
   rotating them in. So the sample here is split into a small **core**
   (fixed every poll, for day-over-day trend continuity on the routes that
   matter most) plus a **rotating remainder** that cycles through the rest
   of the network across polls - sweeping the full 303 stops into the
   dataset over a few weeks, at zero extra call cost.
"""

from __future__ import annotations

import pandas as pd

from wata.gtfs import GtfsFeed

CALL_MAX_STOP_IDS = 100


def stop_route_direction_departures(
    feed: GtfsFeed, service_id: str = "Full-Weekday"
) -> pd.DataFrame:
    """One row per (stop_id, route_id, direction_id) with departure count.

    Splitting by direction_id (not just route_id) matters: a route's two
    directions can have very different reliability (e.g. inbound traffic
    congestion vs outbound), so coverage should guarantee both, not just
    "the route appears somewhere."
    """
    trips = feed.trips[feed.trips["service_id"] == service_id][
        ["trip_id", "route_id", "direction_id"]
    ]
    st = feed.stop_times.merge(trips, on="trip_id", how="inner")
    return (
        st.groupby(["stop_id", "route_id", "direction_id"])
        .size()
        .rename("departures")
        .reset_index()
    )


def select_core_stops(
    feed: GtfsFeed,
    *,
    min_per_route_direction: int = 2,
    service_id: str = "Full-Weekday",
) -> list[str]:
    """The fixed part of every poll: enough stops that every (route,
    direction) pair is represented by at least `min_per_route_direction`
    stops, picked by volume. Hub overlap keeps this small in practice
    (WATA's network needs well under 30 stops for min_per_route_direction=2),
    which is the point - it leaves most of a 100-ID call for the rotating
    remainder below.
    """
    by_rd = stop_route_direction_departures(feed, service_id=service_id)
    selected: list[str] = []
    selected_set: set[str] = set()

    route_dirs = by_rd[["route_id", "direction_id"]].drop_duplicates()
    for _, rd in route_dirs.iterrows():
        candidates = (
            by_rd[
                (by_rd["route_id"] == rd["route_id"])
                & (by_rd["direction_id"] == rd["direction_id"])
            ]
            .sort_values("departures", ascending=False)
            .loc[lambda d: ~d["stop_id"].isin(selected_set), "stop_id"]
        )
        for stop_id in candidates.head(min_per_route_direction):
            if stop_id not in selected_set:
                selected.append(stop_id)
                selected_set.add(stop_id)

    return selected


def rotating_groups(
    feed: GtfsFeed, core_stop_ids: list[str], *, group_size: int
) -> list[list[str]]:
    """Partition every served stop NOT in the core into fixed-size groups,
    to be cycled through one-per-poll (poll N uses group N % len(groups)).

    Sorted by stop_id (not shuffled) so the partition is deterministic and
    reproducible from the GTFS snapshot alone - re-running this after a
    feed refresh regroups consistently rather than randomly reshuffling
    stops that haven't changed.
    """
    remainder = sorted(feed.served_stop_ids() - set(core_stop_ids))
    return [remainder[i : i + group_size] for i in range(0, len(remainder), group_size)]


def poll_stop_ids(
    feed: GtfsFeed,
    poll_index: int,
    *,
    core_stop_ids: list[str] | None = None,
    call_max_stop_ids: int = CALL_MAX_STOP_IDS,
) -> list[str]:
    """The stop IDs for one poll: the fixed core plus one rotating group,
    filled to (at most) call_max_stop_ids. `poll_index` should increment
    every call (e.g. a persisted counter in the collector) so successive
    polls advance through the rotation instead of repeating a group.
    """
    core_stop_ids = core_stop_ids if core_stop_ids is not None else select_core_stops(feed)
    remaining_budget = call_max_stop_ids - len(core_stop_ids)
    if remaining_budget <= 0:
        return core_stop_ids[:call_max_stop_ids]

    groups = rotating_groups(feed, core_stop_ids, group_size=remaining_budget)
    if not groups:
        return core_stop_ids
    group = groups[poll_index % len(groups)]
    return core_stop_ids + group


def rotation_cycle_length(feed: GtfsFeed, core_stop_ids: list[str] | None = None) -> int:
    """Number of distinct polls needed before every stop has been sampled
    at least once (i.e. the rotation repeats).
    """
    core_stop_ids = core_stop_ids if core_stop_ids is not None else select_core_stops(feed)
    remaining_budget = CALL_MAX_STOP_IDS - len(core_stop_ids)
    return len(rotating_groups(feed, core_stop_ids, group_size=remaining_budget))
