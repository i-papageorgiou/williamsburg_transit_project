import datetime as dt

from wata.gtfs import GtfsFeed


def test_load_latest_snapshot():
    feed = GtfsFeed.load()
    assert len(feed.routes) == 13
    assert len(feed.served_stop_ids()) == 303


def test_calendar_dates_used_not_empty_calendar():
    """calendar.txt is empty in the WATA feed; service must come from
    calendar_dates.txt or every date looks like it has zero service."""
    feed = GtfsFeed.load()
    assert feed.calendar.empty
    a_weekday = next(
        d
        for d in feed.calendar_dates["date"].dt.date
        if d.weekday() < 5
    )
    assert len(feed.service_ids_on(a_weekday)) > 0


def test_trips_on_returns_trips_for_each_known_day_type():
    feed = GtfsFeed.load()
    dates_with_service = feed.calendar_dates.loc[
        feed.calendar_dates["exception_type"] == 1, "date"
    ].dt.date

    weekday = next(d for d in dates_with_service if d.weekday() < 5)
    saturday = next(d for d in dates_with_service if d.weekday() == 5)
    sunday = next(d for d in dates_with_service if d.weekday() == 6)

    assert len(feed.trips_on(weekday)) > 0
    assert len(feed.trips_on(saturday)) > 0
    assert len(feed.trips_on(sunday)) > 0


def test_route_12a_has_no_trips():
    """12A is defined in routes.txt but currently has zero scheduled trips."""
    feed = GtfsFeed.load()
    assert "12A" in set(feed.routes["route_id"])
    assert (feed.trips["route_id"] == "12A").sum() == 0
