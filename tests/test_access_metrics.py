from wata.gtfs import GtfsFeed
from wata.metrics.access import coverage_summary, frequency_tier, served_stops_gdf


def test_frequency_tier_thresholds():
    assert frequency_tier(0) == "unserved"
    assert frequency_tier(5) == "infrequent"
    assert frequency_tier(20) == "moderate"
    assert frequency_tier(60) == "frequent"


def test_served_stops_have_departure_counts():
    feed = GtfsFeed.load()
    stops = served_stops_gdf(feed)
    assert len(stops) == 303
    assert (stops["departures"] > 0).all()


def test_coverage_summary_has_all_tiers_present():
    feed = GtfsFeed.load()
    coverage = coverage_summary(feed)
    # "unserved" is expected to be absent: only served stops get buffers.
    assert set(coverage.keys()) <= {"frequent", "moderate", "infrequent", "unserved"}
    assert all(v > 0 for v in coverage.values())
