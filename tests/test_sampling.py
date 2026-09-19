from wata.gtfs import GtfsFeed
from wata.sampling import select_stratified_stops


def test_sample_covers_every_active_route():
    feed = GtfsFeed.load()
    sample = select_stratified_stops(feed, n_total=100, min_per_route=6)
    active_routes = set(feed.trips["route_id"].unique())
    covered = set(",".join(sample["covered_routes"]).split(","))
    assert active_routes <= covered


def test_sample_size_is_capped_at_n_total():
    feed = GtfsFeed.load()
    sample = select_stratified_stops(feed, n_total=100, min_per_route=6)
    assert len(sample) <= 100


def test_smaller_sample_still_respects_min_per_route_where_possible():
    feed = GtfsFeed.load()
    sample = select_stratified_stops(feed, n_total=100, min_per_route=6)
    # No duplicate stops
    assert sample["stop_id"].is_unique
