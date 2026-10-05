import datetime as dt
import gzip
import json
import random

import numpy as np

from wata.collect import (
    POLL_CYCLE_SECONDS,
    append_snapshot,
    collect_one_snapshot,
    next_poll_index,
    random_poll_target,
)
from wata.metrics.reliability import _ks_uniform


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def stop_departures(self, global_stop_ids):
        self.calls.append(global_stop_ids)
        return self.response


def test_collect_one_snapshot_tags_fetch_time_and_ids():
    client = FakeClient({"stops": []})
    record = collect_one_snapshot(client, ["A:1", "A:2"])
    assert record["global_stop_ids"] == ["A:1", "A:2"]
    assert record["response"] == {"stops": []}
    assert "fetched_at" in record
    assert client.calls == [["A:1", "A:2"]]


def test_append_snapshot_writes_gzipped_ndjson(tmp_path):
    path = tmp_path / "2026-09-18.ndjson.gz"
    append_snapshot({"a": 1}, path=path)
    append_snapshot({"b": 2}, path=path)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    assert lines == [{"a": 1}, {"b": 2}]


def test_next_poll_index_starts_at_zero_and_increments(tmp_path):
    path = tmp_path / "poll_index.json"
    assert next_poll_index(path) == 0
    assert next_poll_index(path) == 1
    assert next_poll_index(path) == 2
    assert json.loads(path.read_text()) == {"poll_index": 3}


def test_poll_target_is_the_next_occurrence_of_its_phase():
    now = dt.datetime(2026, 10, 6, 14, 1, 30, tzinfo=dt.timezone.utc)
    for seed in range(200):
        drawn_phase = random.Random(seed).uniform(0, POLL_CYCLE_SECONDS)
        target = random_poll_target(now, random.Random(seed))
        wait = (target - now).total_seconds()
        assert 0 <= wait < POLL_CYCLE_SECONDS
        # The poll lands exactly on the drawn phase, wherever `now` fell.
        assert abs(target.timestamp() % POLL_CYCLE_SECONDS - drawn_phase) < 1e-3


def poll_phases(rng: random.Random, days: int = 60) -> np.ndarray:
    """Simulate the real chain: a dispatch every 30 min, a random runner
    start-up delay, queueing behind an overrunning previous run, then the
    wait for a random phase. Returns each poll's time in epoch seconds."""
    polls, previous_end = [], 0.0
    first = dt.datetime(2026, 10, 6, 9, 30, tzinfo=dt.timezone.utc).timestamp()
    for i in range(days * 34):
        dispatch = first + i * 1800
        ready = max(dispatch, previous_end) + rng.uniform(30, 180)
        now = dt.datetime.fromtimestamp(ready, dt.timezone.utc)
        poll = random_poll_target(now, rng).timestamp()
        polls.append(poll)
        previous_end = poll + 60
    return np.array(polls)


def test_poll_phases_are_uniform_despite_startup_delay_and_queueing():
    polls = poll_phases(random.Random(0))
    for cycle in (1800, 3600):
        _, p_value = _ks_uniform((polls % cycle) / cycle)
        assert p_value > 0.01, f"poll phase not uniform over a {cycle // 60}-min cycle"


def test_a_random_wait_from_startup_would_fail_the_same_check():
    """Guards the design: waiting a random 0-28 min from whenever the runner
    starts under-samples the start of each half-hour."""
    rng = random.Random(0)
    polls = np.array(
        [1800 * i + rng.uniform(30, 180) + rng.uniform(0, 28 * 60) for i in range(2000)]
    )
    _, p_value = _ks_uniform((polls % 1800) / 1800)
    assert p_value < 0.01
