from wata.gtfs import GtfsFeed
from wata.metrics.service import headway_by_hour, service_summary


def test_service_summary_ranks_route_15_first():
    """Route 15 (Colonial) is WATA's highest-frequency route; this is a
    sanity check that the summary isn't silently mis-joining routes."""
    feed = GtfsFeed.load()
    summary = service_summary(feed)
    assert summary.iloc[0]["route_id"] == "15"


def test_route_11_has_no_sunday_service():
    feed = GtfsFeed.load()
    summary = service_summary(feed).set_index("route_id")
    assert summary.loc["11", "sunday_trips"] == 0


def test_headway_is_positive_and_reasonable():
    feed = GtfsFeed.load()
    hw = headway_by_hour(feed, "15", "Full-Weekday")
    midday = hw[hw["hour"] == 12]
    assert not midday.empty
    assert 0 < midday.iloc[0]["median_headway_minutes"] < 60
