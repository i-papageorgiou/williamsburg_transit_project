"""Client for the Transit API (https://external.transitapp.com), with a
persisted monthly call budget and a rate limiter — both mandatory given the
free tier's 5 calls/minute and 1,500 calls/month, which a single stray
retry loop could burn through in seconds.

Nothing here has been run against a real key yet (Phase 0 is pending);
CallBudget and RateLimiter are unit-tested with a fake clock, and the
client itself is tested against a mocked `requests.Session` so the
behavior is verified before a key exists.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from wata.gtfs import REPO_ROOT

BASE_URL = "https://external.transitapp.com"
BUDGET_LEDGER_PATH = REPO_ROOT / "data" / "processed" / "api_call_budget.json"

FREE_TIER_MONTHLY_CALLS = 1500
FREE_TIER_CALLS_PER_MINUTE = 5


class BudgetExceededError(RuntimeError):
    """Raised when a call would exceed the configured monthly budget."""


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` calls in any `period_seconds`.

    Takes a `sleep_fn` and `now_fn` so tests can run instantly instead of
    waiting on a real clock.
    """

    def __init__(
        self,
        max_calls: int = FREE_TIER_CALLS_PER_MINUTE,
        period_seconds: float = 60.0,
        *,
        now_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._now = now_fn
        self._sleep = sleep_fn
        self._call_times: list[float] = []

    def acquire(self) -> None:
        """Block (via sleep_fn) until a call is allowed, then record it."""
        now = self._now()
        window_start = now - self.period_seconds
        self._call_times = [t for t in self._call_times if t > window_start]

        if len(self._call_times) >= self.max_calls:
            wait = self._call_times[0] + self.period_seconds - now
            if wait > 0:
                self._sleep(wait)
            now = self._now()
            window_start = now - self.period_seconds
            self._call_times = [t for t in self._call_times if t > window_start]

        self._call_times.append(now)


@dataclass
class CallBudget:
    """Tracks cumulative calls against a monthly cap, persisted to disk so
    it survives across collector runs (e.g. separate GitHub Actions jobs).
    """

    monthly_cap: int = FREE_TIER_MONTHLY_CALLS
    ledger_path: Path = field(default_factory=lambda: BUDGET_LEDGER_PATH)

    def _load(self) -> dict:
        if not self.ledger_path.exists():
            return {}
        return json.loads(self.ledger_path.read_text())

    def _save(self, data: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps(data, indent=2))

    def calls_this_month(self, month_key: str) -> int:
        return self._load().get(month_key, 0)

    def check_and_record(self, month_key: str, n_calls: int = 1) -> None:
        """Raise BudgetExceededError if this would exceed the cap; otherwise
        record the call(s) and return.
        """
        data = self._load()
        used = data.get(month_key, 0)
        if used + n_calls > self.monthly_cap:
            raise BudgetExceededError(
                f"Call would use {used + n_calls}/{self.monthly_cap} calls "
                f"for {month_key}; refusing to exceed the monthly budget."
            )
        data[month_key] = used + n_calls
        self._save(data)


class TransitApiClient:
    """Thin wrapper around the Transit API v4 endpoints this project uses.

    `session` is injectable so tests can pass a mock instead of hitting
    the network.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
        budget: CallBudget | None = None,
        month_key_fn: Callable[[], str] = lambda: time.strftime("%Y-%m"),
    ):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.budget = budget or CallBudget()
        self._month_key_fn = month_key_fn

    def _get(self, path: str, params: dict) -> dict:
        self.budget.check_and_record(self._month_key_fn())
        self.rate_limiter.acquire()
        resp = self.session.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"apiKey": self.api_key},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def available_networks(self, lat: float | None = None, lon: float | None = None) -> dict:
        params = {}
        if lat is not None and lon is not None:
            params = {"lat": lat, "lon": lon}
        return self._get("/v4/public/available_networks", params)

    def nearby_stops(self, lat: float, lon: float, max_distance: int = 1500) -> dict:
        """max_distance is capped by the API itself at 1500m (its documented
        maximum); values above that are rejected by the server, not clamped
        here, so callers should stay at or under it.
        """
        return self._get(
            "/v4/public/nearby_stops",
            {"lat": lat, "lon": lon, "max_distance": max_distance},
        )

    def stop_departures(self, global_stop_ids: list[str]) -> dict:
        if len(global_stop_ids) > 100:
            raise ValueError(
                f"stop_departures accepts at most 100 stop IDs, got {len(global_stop_ids)}"
            )
        return self._get(
            "/v4/public/stop_departures",
            {"global_stop_ids": ",".join(global_stop_ids)},
        )

    def plan(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> dict:
        return self._get(
            "/v4/public/plan",
            {
                "from_lat": from_lat,
                "from_lon": from_lon,
                "to_lat": to_lat,
                "to_lon": to_lon,
            },
        )
