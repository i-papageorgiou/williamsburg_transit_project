import datetime as dt

import numpy as np
import pandas as pd
import pytest

import wata.metrics.reliability as reliability
from wata.collect import SERVICE_TZ
from wata.gtfs import GtfsFeed, snapshot_dir_for
from wata.metrics.reliability import (
    _nonincreasing,
    departure_checks,
    lateness_curve,
    lateness_summary,
    poll_timing_check,
)
from wata.snapshots import polled_stops, schedule_items

DAY = dt.date(2026, 10, 6)
MAPPING = {f"S{i}": f"G:{i}" for i in range(1, 5)}


def at(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=SERVICE_TZ)


def tiny_feed() -> GtfsFeed:
    """Three trips over the same four stops: S1 origin, S2-S3 mid-route, S4 terminal."""
    trips = pd.DataFrame(
        {"trip_id": ["T1", "T2", "T3"], "route_id": "R", "service_id": "WK", "direction_id": "0"}
    )
    stop_times = pd.DataFrame(
        [
            (trip, f"S{seq}", seq, f"08:{start + 10 * (seq - 1):02d}:00", "1")
            for trip, start in (("T1", 0), ("T2", 1), ("T3", 2))
            for seq in range(1, 5)
        ],
        columns=["trip_id", "stop_id", "stop_sequence", "departure_time", "timepoint"],
    )
    calendar_dates = pd.DataFrame(
        {"service_id": ["WK"], "date": [pd.Timestamp(DAY)], "exception_type": [1]}
    )
    empty = pd.DataFrame()
    return GtfsFeed(
        snapshot_date=DAY,
        agency=empty,
        stops=empty,
        routes=pd.DataFrame({"route_id": ["R"], "route_short_name": ["15"]}),
        trips=trips,
        stop_times=stop_times,
        calendar=empty,
        calendar_dates=calendar_dates,
        shapes=empty,
    )


def item(trip: str, sched: dt.datetime, *, rt: bool = True, cancelled: bool = False, delay_s: int = 0) -> dict:
    return {
        "rt_trip_id": trip,
        "trip_search_key": trip,
        "scheduled_departure_time": int(sched.timestamp()),
        "departure_time": int(sched.timestamp()) + delay_s,
        "is_real_time": rt,
        "is_cancelled": cancelled,
    }


def snapshot(fetched: dt.datetime, stops: list[str], listings: dict[str, list[dict]], *, randomized=True) -> dict:
    snap = {
        "fetched_at": fetched.astimezone(dt.timezone.utc).isoformat(),
        "poll_index": 0,
        "global_stop_ids": stops,
        "response": {
            "route_departures": [
                {
                    "global_stop_id": stop,
                    "route_short_name": "15",
                    "merged_itineraries": [{"direction_id": 0, "schedule_items": items}],
                }
                for stop, items in listings.items()
            ]
        },
    }
    if randomized:
        snap["poll_target"] = (fetched - dt.timedelta(seconds=3)).astimezone(dt.timezone.utc).isoformat()
    return snap


@pytest.fixture
def no_core(monkeypatch):
    monkeypatch.setattr(reliability, "select_core_stops", lambda feed: [])


def test_checks_count_departed_trips_and_skip_unmeasurable_ones(no_core):
    feed = tiny_feed()
    snaps = [
        # Fixed-time poll: ignored entirely.
        snapshot(at(7, 30), ["G:2"], {"G:2": [item("T1", at(8, 10))]}, randomized=False),
        # 08:16: T1 is 6 min late (still listed at S2, scheduled 08:10).
        # T2 is flagged cancelled; T3 is listed but never real-time.
        snapshot(
            at(8, 16),
            ["G:1", "G:2", "G:3"],
            {
                "G:2": [item("T1", at(8, 10), delay_s=420)],
                "G:3": [
                    item("T1", at(8, 20), delay_s=420),
                    item("T2", at(8, 21), cancelled=True),
                    item("T3", at(8, 22), rt=False),
                ],
            },
        ),
    ]
    checks = departure_checks(
        schedule_items(snaps), polled_stops(snaps), feed_for=lambda d: feed, mapping=MAPPING
    )

    # Only T1 is measurable, and only at mid-route stops (S1 is its origin).
    assert set(checks["rt_trip_id"]) == {"T1"}
    assert set(checks["stop_id"]) == {"S2", "S3"}
    by_stop = checks.set_index("stop_id")
    assert by_stop.loc["S2", "u_min"] == 6
    assert by_stop.loc["S2", "still_listed"]
    assert by_stop.loc["S3", "u_min"] == -4


def test_checks_include_departures_that_are_no_longer_listed(no_core):
    feed = tiny_feed()
    snaps = [
        snapshot(at(7, 0), ["G:3"], {"G:3": [item("T1", at(8, 20))]}),
        # 08:12: T1 left S2 on time, so it is listed only at S3.
        snapshot(at(8, 12), ["G:2", "G:3"], {"G:3": [item("T1", at(8, 20))]}),
    ]
    checks = departure_checks(
        schedule_items(snaps), polled_stops(snaps), feed_for=lambda d: feed, mapping=MAPPING
    )
    s2 = checks[(checks["stop_id"] == "S2") & (checks["fetched_at"] == at(8, 12))]
    assert len(s2) == 1
    assert not s2["still_listed"].iloc[0]


def test_nonincreasing_pools_violations():
    fitted = _nonincreasing(np.array([0.9, 0.5, 0.7, 0.1]), np.array([1, 1, 1, 1]))
    np.testing.assert_allclose(fitted, [0.9, 0.6, 0.6, 0.1])


def simulated_checks(delays: np.ndarray, rng: np.random.Generator) -> pd.DataFrame:
    """Each departure checked once at a uniformly random u, as randomized
    polls produce."""
    u = rng.uniform(-10, 30, size=len(delays))
    return pd.DataFrame(
        {
            "service_date": [DAY + dt.timedelta(days=int(d)) for d in rng.integers(0, 15, len(delays))],
            "u_min": u,
            "still_listed": delays > u,
            "in_core": rng.random(len(delays)) < 0.5,
        }
    )


def test_estimator_recovers_a_known_late_share():
    rng = np.random.default_rng(1)
    n = 20000
    late = rng.random(n) < 0.2
    delays = np.where(late, rng.uniform(5.5, 15, n), rng.uniform(-0.5, 4, n))
    summary = lateness_summary(simulated_checks(delays, rng)).iloc[0]

    assert summary["status"] == "ok"
    assert summary["late_share"] == pytest.approx(0.2, abs=0.02)
    assert summary["late_ci_low"] <= 0.2 <= summary["late_ci_high"]
    assert summary["early_upper"] == pytest.approx(0.0, abs=0.02)
    assert summary["monotone_adjustment"] < 0.01

    curve = lateness_curve(simulated_checks(delays, rng))
    assert curve["p_delay_over_u"].is_monotonic_decreasing


def test_small_groups_get_no_number():
    rng = np.random.default_rng(2)
    summary = lateness_summary(simulated_checks(rng.uniform(0, 3, 100), rng)).iloc[0]
    assert summary["status"] == "insufficient_data"
    assert "late_share" not in summary or pd.isna(summary["late_share"])


def test_snapshot_dir_for_picks_the_feed_in_effect(tmp_path):
    for name in ("2026-09-18", "2026-09-21", "2026-09-28"):
        (tmp_path / name).mkdir()
    assert snapshot_dir_for(dt.date(2026, 9, 27), tmp_path).name == "2026-09-21"
    assert snapshot_dir_for(dt.date(2026, 9, 28), tmp_path).name == "2026-09-28"
    with pytest.raises(FileNotFoundError):
        snapshot_dir_for(dt.date(2026, 9, 1), tmp_path)


def test_polls_that_missed_their_target_do_not_count_as_random():
    on_time = snapshot(at(8, 16), ["G:2"], {})
    late = snapshot(at(8, 46), ["G:2"], {})
    late["poll_target"] = at(8, 40).astimezone(dt.timezone.utc).isoformat()
    fixed = snapshot(at(9, 0), ["G:2"], {}, randomized=False)
    flags = polled_stops([on_time, late, fixed]).set_index("fetched_at")["randomized"]
    assert flags.tolist() == [True, False, False]


def timing_polls(seconds: np.ndarray) -> pd.DataFrame:
    base = pd.Timestamp("2026-10-06 09:00", tz="UTC")
    return pd.DataFrame(
        {"fetched_at": base + pd.to_timedelta(seconds, unit="s"), "randomized": True, "global_stop_id": "G:1"}
    )


def test_poll_timing_check_accepts_uniform_and_flags_fixed_times():
    rng = np.random.default_rng(3)
    uniform = poll_timing_check(timing_polls(rng.uniform(0, 30 * 86400, 1000)))
    assert uniform["uniform"].all()

    # Always within a minute of :00/:30, like the original collector.
    slots = rng.integers(0, 1440, 1000) * 1800 + rng.uniform(0, 60, 1000)
    fixed = poll_timing_check(timing_polls(slots))
    assert not fixed["uniform"].any()
