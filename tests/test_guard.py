"""Behaviour tests for FreeTierGuard.

Grouped by the promise each one protects: paying users are untouched, the cap
actually stops a free user, and nothing about this library can break a run.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from apify_free_tier import FreeTierGuard


# ------------------------------------------------- promise 1: paid = untouched


async def test_paying_user_makes_no_database_calls(actor, db, free_env, monkeypatch):
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")

    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)
    await guard.close()

    assert guard.tracking is False
    assert db.get_calls == 0
    assert db.increment_calls == []
    assert actor.charges == [("item_returned", 1)]  # platform charge still happened
    assert actor.log.infos == []                      # and stayed silent about it


async def test_free_tier_force_overrides_paying_flag(actor, db, free_env, monkeypatch):
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")
    monkeypatch.setenv("FREE_TIER_FORCE", "1")

    guard = await FreeTierGuard.start()

    assert guard.tracking is True
    assert db.get_calls == 1


async def test_no_free_max_disables_tracking_silently(actor, db, free_env, monkeypatch):
    """Most of the fleet has no FREE_MAX. Installing the library must be a no-op there."""
    monkeypatch.delenv("FREE_MAX")

    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)

    assert guard.tracking is False
    assert db.get_calls == 0
    assert actor.log.warnings == []


async def test_off_platform_is_inert(actor, db, free_env):
    actor._at_home = False

    guard = await FreeTierGuard.start()
    stop = await guard.charge("item_returned", 1)

    assert guard.tracking is False
    assert stop is False
    assert actor.charges == []  # is_at_home gate, same as the fleet's _charge


# ------------------------------------------------------ promise 2: the cap bites


async def test_blocks_a_user_who_is_already_over(actor, db, free_env):
    db.total = Decimal("0.05")  # FREE_MAX is 0.05

    guard = await FreeTierGuard.start()

    assert guard.blocked is True
    assert guard.tracking is False
    assert len(actor.status_messages) == 1
    assert "full free monthly allowance" in actor.status_messages[0]
    assert "different free account" in actor.status_messages[0]
    assert db.closed is True


async def test_stops_mid_run_when_the_cap_is_crossed(actor, db, free_env):
    """0.05 cap, 0.01 per item: the 5th item must be the one that stops it."""
    guard = await FreeTierGuard.start()
    assert guard.blocked is False

    results = [await guard.charge("item_returned", 1) for _ in range(6)]

    assert results[:4] == [False, False, False, False]
    assert results[4] is True
    assert db.total == Decimal("0.05")
    assert guard.tracking is False
    assert any("full free monthly allowance" in m for m in actor.log.warnings)


async def test_exhausted_flag_lets_the_actor_keep_the_right_status_message(actor, db, free_env):
    """Actors set their own terminal status on the way out; this is how they
    know not to overwrite the guard's explanation."""
    guard = await FreeTierGuard.start()
    assert guard.exhausted is False

    for _ in range(5):
        await guard.charge("item_returned", 1)

    assert guard.exhausted is True


async def test_exhausted_is_true_when_blocked_at_start(actor, db, free_env):
    db.total = Decimal("0.05")

    guard = await FreeTierGuard.start()

    assert guard.exhausted is True


async def test_counts_multi_unit_charges(actor, db, free_env):
    guard = await FreeTierGuard.start()

    stop = await guard.charge("item_returned", 3)
    await guard.close()

    assert stop is False
    assert db.total == Decimal("0.03")


async def test_disclosure_is_logged_at_start(actor, db, free_env):
    db.total = Decimal("0.02")

    await FreeTierGuard.start()

    assert len(actor.log.infos) == 1
    assert "$0.02 of $0.05" in actor.log.infos[0]
    assert "Paid Apify accounts are never limited" in actor.log.infos[0]


async def test_pending_is_counted_while_a_flush_is_in_flight(actor, db, free_env):
    """The reason background flushing is safe: in-flight money still counts."""
    guard = await FreeTierGuard.start()

    # Four rapid charges; the background flush has not been awaited yet.
    for _ in range(4):
        await guard.charge("item_returned", 1)
    assert guard._known_total + guard._pending == Decimal("0.04")

    assert await guard.charge("item_returned", 1) is True


async def test_close_flushes_the_remainder(actor, db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)
    await guard.close()

    assert db.total == Decimal("0.01")
    assert guard._pending == Decimal("0")
    assert db.closed is True


async def test_platform_charge_limit_still_propagates(actor, db, free_env):
    """The guard must not swallow the platform's own stop signal."""
    actor.charge_limit_reached = True

    guard = await FreeTierGuard.start()

    assert await guard.charge("item_returned", 1) is True


# ----------------------------------------- promise 3: it cannot break your run


async def test_database_down_at_start_is_permissive(actor, db, free_env):
    db.fail = True

    guard = await FreeTierGuard.start()
    stop = await guard.charge("item_returned", 1)

    assert guard.blocked is False
    assert guard.tracking is False
    assert stop is False
    assert actor.charges == [("item_returned", 1)]
    assert any("continuing without it" in w for w in actor.log.warnings)
    assert db.get_calls == 2  # one retry


async def test_flush_failures_trip_the_circuit_breaker(actor, db, free_env):
    guard = await FreeTierGuard.start()
    db.fail = True

    for _ in range(4):
        await guard.charge("item_returned", 1)
        await asyncio.sleep(0)  # let the background flush run

    assert guard.tracking is False
    assert len(db.increment_calls) <= 3
    assert any("continuing without it" in w for w in actor.log.warnings)


async def test_missing_supabase_config_warns_and_continues(actor, db, free_env, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")

    guard = await FreeTierGuard.start()

    assert guard.tracking is False
    assert any("not configured" in w for w in actor.log.warnings)


async def test_old_sdk_goes_inert(actor, db, free_env, monkeypatch):
    monkeypatch.setattr(actor.__class__, "get_charging_manager", None, raising=False)
    delattr_target = type(actor)
    monkeypatch.delattr(delattr_target, "get_charging_manager", raising=False)

    guard = await FreeTierGuard.start()

    assert guard.tracking is False
    assert any("3.0 or newer" in w for w in actor.log.warnings)


async def test_non_ppe_actor_is_not_metered(actor, db, free_env):
    actor._pricing.is_pay_per_event = False

    guard = await FreeTierGuard.start()

    assert guard.tracking is False
    assert db.get_calls == 0


async def test_unpriced_event_warns_once_and_keeps_going(actor, db, free_env):
    guard = await FreeTierGuard.start()

    for _ in range(3):
        assert await guard.charge("mystery_event", 1) is False

    warnings = [w for w in actor.log.warnings if "mystery_event" in w]
    assert len(warnings) == 1
    assert guard.tracking is True


async def test_pricing_read_failure_is_not_fatal(actor, db, free_env):
    def boom():
        raise RuntimeError("platform fault")

    actor.get_pricing_info = boom

    guard = await FreeTierGuard.start()

    assert guard.tracking is False
    assert guard.blocked is False


async def test_bad_free_max_value_disables_tracking(actor, db, free_env, monkeypatch):
    monkeypatch.setenv("FREE_MAX", "not-a-number")

    guard = await FreeTierGuard.start()

    assert guard.tracking is False


@pytest.mark.parametrize("raw,expected", [("$0.50", "0.50"), (" 0.25 ", "0.25")])
async def test_free_max_tolerates_sloppy_input(actor, db, free_env, monkeypatch, raw, expected):
    monkeypatch.setenv("FREE_MAX", raw)

    guard = await FreeTierGuard.start()

    assert guard._free_max == Decimal(expected)
