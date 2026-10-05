import datetime as dt
import gzip
import json

from wata.collect import SERVICE_TZ
from wata.metrics.quality import (
    cancellations_by_day,
    recurring_cancellations,
    trip_cancellations,
)
from wata.snapshots import iter_snapshots, schedule_items


def ts(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=SERVICE_TZ)


def item(trip: str, sched: dt.datetime, *, cancelled: bool = False, delay_s: int = 0) -> dict:
    return {
        "rt_trip_id": trip,
        "trip_search_key": f"WATAVA:{trip}",
        "scheduled_departure_time": int(sched.timestamp()),
        "departure_time": int(sched.timestamp()) + delay_s,
        "is_real_time": True,
        "is_cancelled": cancelled,
    }


def snapshot(fetched: dt.datetime, items: list[dict], *, route: str = "15", stop: str = "WATAVA:1") -> dict:
    return {
        "fetched_at": fetched.astimezone(dt.timezone.utc).isoformat(),
        "poll_index": 0,
        "global_stop_ids": [stop],
        "response": {
            "route_departures": [
                {
                    "global_stop_id": stop,
                    "route_short_name": route,
                    "merged_itineraries": [
                        {
                            "direction_id": 0,
                            "itineraries": [{"direction_headsign": "Market House"}],
                            "schedule_items": items,
                        }
                    ],
                }
            ]
        },
    }


def test_schedule_items_flattens_and_localizes(tmp_path):
    path = tmp_path / "2026-09-24.ndjson.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps(snapshot(ts(24, 17, 30), [item("T1", ts(24, 18, 7), delay_s=120)])) + "\n")

    items = schedule_items(iter_snapshots([path]))
    assert len(items) == 1
    row = items.iloc[0]
    assert row["route_short_name"] == "15"
    assert row["headsign"] == "Market House"
    assert row["scheduled_departure_time"].hour == 18  # local, not UTC
    assert (row["departure_time"] - row["scheduled_departure_time"]).total_seconds() == 120


def test_final_status_wins_and_reinstated_trips_are_not_cancellations():
    snaps = [
        snapshot(ts(24, 17, 0), [item("KEPT", ts(24, 18, 7)), item("DROPPED", ts(24, 18, 37))]),
        snapshot(ts(24, 17, 30), [item("KEPT", ts(24, 18, 7), cancelled=True), item("DROPPED", ts(24, 18, 37), cancelled=True)]),
        snapshot(ts(24, 18, 0), [item("KEPT", ts(24, 18, 7)), item("DROPPED", ts(24, 18, 37), cancelled=True)]),
        snapshot(ts(24, 18, 40), [item("DROPPED", ts(24, 18, 37), cancelled=True)]),
    ]
    trips = trip_cancellations(schedule_items(snaps)).set_index("rt_trip_id")

    assert not trips.loc["KEPT", "cancelled"]
    assert trips.loc["KEPT", "reinstated"]
    assert trips.loc["DROPPED", "cancelled"]
    assert not trips.loc["DROPPED", "reinstated"]
    # First flagged at 17:30 for an 18:37 departure.
    assert trips.loc["DROPPED", "notice_minutes"] == 67.0


def test_trips_not_yet_run_are_excluded():
    # A sparse stop lists tomorrow's trip; it has no final status yet.
    snaps = [snapshot(ts(24, 20, 0), [item("TONIGHT", ts(24, 20, 0)), item("TOMORROW", ts(25, 9, 55), cancelled=True)])]
    trips = trip_cancellations(schedule_items(snaps))
    assert trips["rt_trip_id"].tolist() == ["TONIGHT"]


def test_daily_counts_and_recurring_trips():
    snaps = []
    for day in (24, 25, 26):  # Thu, Fri, Sat
        snaps.append(
            snapshot(
                ts(day, 18, 10),
                [
                    item("EVENING", ts(day, 18, 7), cancelled=True),
                    item("ONE_OFF", ts(day, 17, 55), cancelled=(day == 25)),
                    item("FINE", ts(day, 17, 45)),
                ],
            )
        )
    trips = trip_cancellations(schedule_items(snaps))

    daily = cancellations_by_day(trips).set_index("service_date")
    assert daily.loc[dt.date(2026, 9, 24), "trips_observed"] == 3
    assert daily.loc[dt.date(2026, 9, 24), "trips_cancelled"] == 1
    assert daily.loc[dt.date(2026, 9, 25), "trips_cancelled"] == 2
    assert daily.loc[dt.date(2026, 9, 25), "weekday"] == "Friday"

    recurring = recurring_cancellations(trips, min_days=2)
    assert recurring["rt_trip_id"].tolist() == ["EVENING"]
    row = recurring.iloc[0]
    assert row["days_cancelled"] == 3
    assert row["cancel_rate"] == 1.0
    assert row["cancelled_weekdays"] == "Thu,Fri,Sat"
    assert row["scheduled_time"] == "18:07"
