"""Test rig: a fake Actor and a fake usage database.

The real `apify.Actor` is a platform singleton, so every test here swaps it for
a recorder. That keeps the tests honest about the two things that actually
matter - what the user sees in the log, and how many database calls we made.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apify_free_tier import guard as guard_module
from apify_free_tier.db import UsageDBError


class FakePricingInfo:
    def __init__(self, prices: dict[str, str] | None, is_ppe: bool = True) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = {k: Decimal(v) for k, v in (prices or {}).items()}


class FakeChargeResult:
    def __init__(self, limit_reached: bool = False) -> None:
        self.event_charge_limit_reached = limit_reached


class FakeLog:
    """Records log output. `infos` / `warnings` hold what the user would see."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    @staticmethod
    def _render(message, args) -> str:
        # The fleet mixes f-strings and lazy %-style args; support both.
        return str(message) % args if args else str(message)

    def info(self, message, *args) -> None:
        self.infos.append(self._render(message, args))

    def warning(self, message, *args) -> None:
        self.warnings.append(self._render(message, args))


class FakeActor:
    """Stands in for apify.Actor."""

    def __init__(self, prices: dict[str, str] | None = None, at_home: bool = True) -> None:
        self._at_home = at_home
        self._pricing = FakePricingInfo(prices)
        self.log = FakeLog()
        self.charges: list[tuple[str, int]] = []
        self.status_messages: list[str] = []
        self.pushed: list[dict] = []
        self.charge_limit_reached = False

    def is_at_home(self) -> bool:
        return self._at_home

    async def charge(self, event_name: str, count: int = 1):
        self.charges.append((event_name, count))
        return FakeChargeResult(self.charge_limit_reached)

    def get_charging_manager(self):
        return self

    def get_pricing_info(self):
        return self._pricing

    async def set_status_message(self, message: str, is_terminal: bool = False) -> None:
        self.status_messages.append(message)

    async def push_data(self, data) -> None:
        self.pushed.extend(data if isinstance(data, list) else [data])


class FakeDB:
    """Records every RPC so tests can assert on call counts, not just totals."""

    def __init__(self, start_total: str = "0", fail: bool = False) -> None:
        self.total = Decimal(start_total)
        self.fail = fail
        self.get_calls = 0
        self.increment_calls: list[Decimal] = []
        self.closed = False

    async def get_usage(self, user_id: str, actor_id: str) -> Decimal:
        self.get_calls += 1
        if self.fail:
            raise UsageDBError("ConnectError")
        return self.total

    async def increment_usage(self, user_id: str, actor_id: str, amount: Decimal) -> Decimal:
        self.increment_calls.append(amount)
        if self.fail:
            raise UsageDBError("ConnectError")
        self.total += amount
        return self.total

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def actor(monkeypatch):
    fake = FakeActor(prices={"item_returned": "0.01"})
    monkeypatch.setattr(guard_module, "Actor", fake)
    return fake


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(guard_module, "UsageDB", lambda url, key: fake)
    return fake


@pytest.fixture
def free_env(monkeypatch):
    """A configured free user on a configured Actor."""
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "0")
    monkeypatch.setenv("APIFY_USER_ID", "user-1")
    monkeypatch.setenv("APIFY_ACTOR_ID", "actor-1")
    monkeypatch.setenv("FREE_MAX", "0.05")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.delenv("FREE_TIER_FORCE", raising=False)
