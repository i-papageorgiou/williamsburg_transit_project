import json

import pytest

from wata.transit_api import (
    BudgetExceededError,
    CallBudget,
    RateLimiter,
    TransitApiClient,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_rate_limiter_allows_up_to_max_calls_without_sleeping():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=5, period_seconds=60, now_fn=clock.now, sleep_fn=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    assert clock.sleeps == []


def test_rate_limiter_sleeps_when_over_limit_in_window():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=5, period_seconds=60, now_fn=clock.now, sleep_fn=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    limiter.acquire()  # 6th call within the same 60s window must wait
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] > 0


def test_rate_limiter_does_not_sleep_once_window_has_passed():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=5, period_seconds=60, now_fn=clock.now, sleep_fn=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    clock.t += 61  # advance past the window manually
    limiter.acquire()
    assert clock.sleeps == []


def test_call_budget_records_and_persists(tmp_path):
    ledger = tmp_path / "budget.json"
    budget = CallBudget(monthly_cap=10, ledger_path=ledger)
    budget.check_and_record("2026-09", n_calls=3)
    budget.check_and_record("2026-09", n_calls=4)
    assert budget.calls_this_month("2026-09") == 7
    assert json.loads(ledger.read_text()) == {"2026-09": 7}


def test_call_budget_refuses_to_exceed_cap(tmp_path):
    ledger = tmp_path / "budget.json"
    budget = CallBudget(monthly_cap=10, ledger_path=ledger)
    budget.check_and_record("2026-09", n_calls=8)
    with pytest.raises(BudgetExceededError):
        budget.check_and_record("2026-09", n_calls=3)
    # the failed call must not have been recorded
    assert budget.calls_this_month("2026-09") == 8


def test_call_budget_tracks_months_independently(tmp_path):
    ledger = tmp_path / "budget.json"
    budget = CallBudget(monthly_cap=10, ledger_path=ledger)
    budget.check_and_record("2026-09", n_calls=10)
    budget.check_and_record("2026-10", n_calls=1)  # new month, fresh budget
    assert budget.calls_this_month("2026-09") == 10
    assert budget.calls_this_month("2026-10") == 1


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse(self.payload)


def test_client_sends_api_key_header_and_correct_path(tmp_path):
    session = FakeSession({"stops": []})
    budget = CallBudget(monthly_cap=10, ledger_path=tmp_path / "budget.json")
    client = TransitApiClient(
        "test-key",
        session=session,
        rate_limiter=RateLimiter(max_calls=100, period_seconds=60, now_fn=lambda: 0, sleep_fn=lambda s: None),
        budget=budget,
        month_key_fn=lambda: "2026-09",
    )
    client.nearby_stops(37.27, -76.70)
    assert session.calls[0]["headers"]["apiKey"] == "test-key"
    assert session.calls[0]["url"].endswith("/v4/public/nearby_stops")
    assert session.calls[0]["params"] == {"lat": 37.27, "lon": -76.70, "max_distance": 1500}


def test_client_rejects_more_than_100_stop_ids(tmp_path):
    session = FakeSession({})
    budget = CallBudget(monthly_cap=10, ledger_path=tmp_path / "budget.json")
    client = TransitApiClient("test-key", session=session, budget=budget, month_key_fn=lambda: "2026-09")
    with pytest.raises(ValueError):
        client.stop_departures([f"stop_{i}" for i in range(101)])
    assert session.calls == []  # never made the request


def test_client_refuses_call_over_budget(tmp_path):
    session = FakeSession({})
    budget = CallBudget(monthly_cap=1, ledger_path=tmp_path / "budget.json")
    client = TransitApiClient("test-key", session=session, budget=budget, month_key_fn=lambda: "2026-09")
    client.nearby_stops(37.27, -76.70)
    with pytest.raises(BudgetExceededError):
        client.nearby_stops(37.27, -76.70)
    assert len(session.calls) == 1
