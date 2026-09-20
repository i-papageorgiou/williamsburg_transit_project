from wata.gtfs import GtfsFeed
from wata.sampling import (
    poll_stop_ids,
    rotating_groups,
    rotation_cycle_length,
    select_core_stops,
)


def test_core_covers_every_route_direction_pair():
    feed = GtfsFeed.load()
    core = select_core_stops(feed, min_per_route_direction=2)
    trips = feed.trips[feed.trips["service_id"] == "Full-Weekday"][
        ["trip_id", "route_id", "direction_id"]
    ]
    st = feed.stop_times.merge(trips, on="trip_id")
    at_core = st[st["stop_id"].isin(core)]
    covered = set(zip(at_core["route_id"], at_core["direction_id"]))
    all_pairs = set(zip(st["route_id"], st["direction_id"]))
    assert covered == all_pairs


def test_core_is_far_smaller_than_full_network():
    feed = GtfsFeed.load()
    core = select_core_stops(feed)
    assert len(core) < 100
    assert len(core) < len(feed.served_stop_ids())


def test_rotating_groups_partition_the_non_core_stops_without_overlap():
    feed = GtfsFeed.load()
    core = select_core_stops(feed)
    groups = rotating_groups(feed, core, group_size=50)
    seen: set[str] = set()
    for g in groups:
        assert not (seen & set(g))  # no stop appears in two groups
        seen |= set(g)
    assert seen == feed.served_stop_ids() - set(core)


def test_poll_stop_ids_stays_within_call_cap():
    feed = GtfsFeed.load()
    core = select_core_stops(feed)
    for i in range(5):
        ids = poll_stop_ids(feed, i, core_stop_ids=core)
        assert len(ids) <= 100
        assert set(core) <= set(ids)


def test_poll_stop_ids_rotates_to_a_different_group_each_poll():
    feed = GtfsFeed.load()
    core = select_core_stops(feed)
    n_groups = rotation_cycle_length(feed, core)
    assert n_groups > 1
    seen_rotating_parts = set()
    for i in range(n_groups):
        ids = set(poll_stop_ids(feed, i, core_stop_ids=core)) - set(core)
        seen_rotating_parts.add(frozenset(ids))
    # each poll in one full cycle should use a distinct rotating slice
    assert len(seen_rotating_parts) == n_groups


def test_full_rotation_cycle_touches_every_served_stop():
    feed = GtfsFeed.load()
    core = select_core_stops(feed)
    n_groups = rotation_cycle_length(feed, core)
    touched: set[str] = set()
    for i in range(n_groups):
        touched |= set(poll_stop_ids(feed, i, core_stop_ids=core))
    assert touched == feed.served_stop_ids()
