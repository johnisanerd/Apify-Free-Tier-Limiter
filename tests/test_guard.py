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
    # ...and says so, so that "no message" always means "not installed".
    assert any("Paid Apify account" in m for m in actor.log.infos)


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


async def test_dormant_install_says_nothing_even_for_a_paying_user(actor, db, free_env, monkeypatch):
    """The paid-user announcement is for opted-in Actors only.

    Without this the library would narrate itself on every run of every Actor in
    the fleet the moment it is installed, before anyone has enabled a cap.
    """
    monkeypatch.delenv("FREE_MAX")
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")

    await FreeTierGuard.start()

    assert actor.log.infos == []
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


async def test_record_meters_without_charging(actor, db, free_env):
    """For Actors that must call Actor.charge themselves to get the billed count."""
    guard = await FreeTierGuard.start()

    stop = await guard.record("item_returned", 3)
    await guard.close()

    assert stop is False
    assert actor.charges == []                       # the guard did NOT charge
    assert db.increment_calls == [Decimal("0.03")]   # but it did meter


async def test_record_stops_at_the_cap(actor, db, free_env):
    guard = await FreeTierGuard.start()

    assert await guard.record("item_returned", 4) is False
    assert await guard.record("item_returned", 1) is True
    assert actor.charges == []


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
    assert any("report event prices" in w for w in actor.log.warnings)


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


# --------------------- promise 6: the cap tracks what a free user costs the owner
#
# The default fixture prices item_returned at $0.01 with a $0.05 cap. These use
# a $0.02 call cost, so one empty call is worth two rows of metering.

CALL_COST = Decimal("0.02")


async def test_an_empty_upstream_call_meters_its_full_cost(actor, db, free_env):
    """The vendor bills a call that returned nothing. Before 0.1.9 it metered $0."""
    guard = await FreeTierGuard.start()

    stop = await guard.record_cost(CALL_COST)
    await guard.close()

    assert stop is False
    assert db.total == CALL_COST
    assert actor.charges == []          # metering only, never a platform charge


async def test_a_thin_call_meters_its_cost_not_price_plus_cost(actor, db, free_env):
    guard = await FreeTierGuard.start()

    await guard.charge("item_returned", 1)       # $0.01 of rows
    await guard.record_cost(CALL_COST)           # tops the call up to $0.02
    await guard.close()

    assert db.total == Decimal("0.02")


async def test_a_call_whose_rows_cover_it_adds_nothing(actor, db, free_env):
    """Well-priced calls must not tighten anyone's existing allowance."""
    guard = await FreeTierGuard.start()

    await guard.charge("item_returned", 3)       # $0.03 >= $0.02
    await guard.record_cost(CALL_COST)
    await guard.close()

    assert db.total == Decimal("0.03")


async def test_the_shortfall_resets_per_call(actor, db, free_env):
    """Rows from an earlier call cannot pay for a later empty one."""
    guard = await FreeTierGuard.start()

    await guard.charge("item_returned", 3)       # call 1: $0.03, covered
    await guard.record_cost(CALL_COST)
    await guard.record_cost(Decimal("0.01"))     # call 2: empty, full cost
    await guard.close()

    assert db.total == Decimal("0.04")


async def test_recorded_rows_count_toward_the_call(actor, db, free_env):
    guard = await FreeTierGuard.start()

    await guard.record("item_returned", 1)
    await guard.record_cost(CALL_COST)
    await guard.close()

    assert db.total == Decimal("0.02")
    assert actor.charges == []


async def test_repeated_empty_calls_hit_the_cap(actor, db, free_env):
    guard = await FreeTierGuard.start()

    results = [await guard.record_cost(CALL_COST) for _ in range(4)]

    assert results == [False, False, True, False]   # $0.06 crosses $0.05; then inert
    assert guard.exhausted is True
    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    # Cost metering hands the user no results, so it must not claim it did.
    assert "returned no results" in notice[0]["message"]


async def test_record_cost_is_a_no_op_for_a_paying_user(actor, db, free_env, monkeypatch):
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")

    guard = await FreeTierGuard.start()
    stop = await guard.record_cost(CALL_COST)

    assert stop is False
    assert db.get_calls == 0
    assert db.increment_calls == []


async def test_record_cost_is_a_no_op_when_not_opted_in(actor, db, free_env, monkeypatch):
    monkeypatch.delenv("FREE_MAX")

    guard = await FreeTierGuard.start()

    assert await guard.record_cost(CALL_COST) is False
    assert db.increment_calls == []
    assert actor.log.warnings == []


async def test_record_cost_is_a_no_op_off_platform(actor, db, free_env):
    actor._at_home = False

    guard = await FreeTierGuard.start()

    assert await guard.record_cost(CALL_COST) is False
    assert db.increment_calls == []


@pytest.mark.parametrize("bad", [None, "-0.01", "NaN", "Infinity", "lots", -1])
async def test_an_unreadable_cost_warns_once_and_keeps_tracking(actor, db, free_env, bad):
    guard = await FreeTierGuard.start()

    assert await guard.record_cost(bad) is False
    assert await guard.record_cost(bad) is False

    warnings = [w for w in actor.log.warnings if "unreadable upstream cost" in w]
    assert len(warnings) == 1
    assert guard.tracking is True
    assert db.increment_calls == []


@pytest.mark.parametrize("raw", [0.02, "0.02", "$0.02", 1])
async def test_record_cost_accepts_numbers_and_strings(actor, db, free_env, raw):
    guard = await FreeTierGuard.start()

    await guard.record_cost(raw)
    await guard.close()

    assert db.total == min(Decimal(str(raw).lstrip("$")), Decimal("1"))


async def test_cost_in_flight_still_counts_toward_the_cap(actor, db, free_env):
    guard = await FreeTierGuard.start()

    await guard.record_cost(CALL_COST)
    await guard.record_cost(CALL_COST)
    assert guard._known_total + guard._pending == Decimal("0.04")

    assert await guard.charge("item_returned", 1) is True


async def test_a_top_up_does_not_inflate_the_delivered_count(actor, db, free_env):
    """A minimum-charge top-up bills events for rows that do not exist."""
    guard = await FreeTierGuard.start()

    await guard.charge("item_returned", 1)                 # one real row
    await guard.charge("item_returned", 4, delivered=0)    # top-up crosses the cap

    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    assert "returned 1 result(s)" in notice[0]["message"]


async def test_lens_geon_incident(actor, db, free_env, monkeypatch):
    """Regression for google-lens-api, September 2026.

    One free account sent ~1,831 exact_matches lookups at $0.00725 each. About
    two thirds came back empty and the rest returned one or two rows at the FREE
    price of $0.0004, so the guard metered $0.52 against a $1.00 cap and never
    stopped it: ~$13 of vendor cost. With record_cost the same traffic has to
    stop at the cap, within about $1.00 / $0.00725 = 138 lookups.
    """
    monkeypatch.setenv("FREE_MAX", "1.00")
    actor._pricing.per_event_prices = {"exact_match_returned": Decimal("0.0004")}
    guard = await FreeTierGuard.start()

    lookups = 0
    stopped = False
    for i in range(1831):
        lookups += 1
        rows = (0, 0, 2)[i % 3]
        if rows and await guard.charge("exact_match_returned", rows):
            stopped = True
            break
        if await guard.record_cost(Decimal("0.00725")):
            stopped = True
            break

    assert stopped is True
    assert lookups <= 138
    assert guard.exhausted is True


# ------------------------------------------- promise 5: the user is told why

async def test_the_reason_reaches_the_dataset_not_just_the_log(actor, db, free_env):
    """API and MCP callers only ever see the dataset. Without a row there, a
    capped run is indistinguishable from an Actor that silently does nothing."""
    guard = await FreeTierGuard.start()

    for _ in range(5):
        await guard.charge("item_returned", 1)

    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    assert "upgrade to a paid Apify account" in notice[0]["message"]
    assert notice[0]["free_allowance_usd"] == 0.05
    assert notice[0]["allowance_resets_on"].endswith("(UTC)")


async def test_blocked_at_start_still_explains_itself_in_the_dataset(actor, db, free_env):
    """The worst case: no results at all. The row is the only thing the caller gets."""
    db.total = Decimal("0.05")

    await FreeTierGuard.start()

    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    assert "no results" in notice[0]["message"]


async def test_notice_row_can_be_switched_off(actor, db, free_env, monkeypatch):
    """For an Actor whose output schema will not accept the extra row."""
    monkeypatch.setenv("FREE_TIER_NOTICE_ROW", "0")
    db.total = Decimal("0.05")

    await FreeTierGuard.start()

    assert actor.pushed == []


async def test_a_failed_notice_push_never_breaks_the_run(actor, db, free_env):
    async def boom(_data):
        raise RuntimeError("schema rejected the row")
    actor.push_data = boom
    db.total = Decimal("0.05")

    guard = await FreeTierGuard.start()

    assert guard.blocked is True                       # still stopped correctly
    assert any("Could not add the free-tier notice" in w for w in actor.log.warnings)


async def test_close_reasserts_the_reason_after_progress_messages(actor, db, free_env):
    """Actors post 'Returned 25 so far...' after each charge, which buries the
    guard's terminal status. The last write wins, so the guard writes last."""
    guard = await FreeTierGuard.start()

    for _ in range(5):
        await guard.charge("item_returned", 1)
    await actor.set_status_message("Returned 25 Actors so far...")   # the Actor clobbers it
    await guard.close()

    assert "full free monthly allowance" in actor.status_messages[-1]


# ------------------------------- a write in flight still counts (v0.1.9 fix)
#
# These use `slow_db`, whose writes park mid-flight until the test releases
# them. v0.1.8 took the amount out of `pending` before awaiting the write, so
# for the whole round trip the guard believed that money was never spent: with
# 0.03 parked it saw 0.00 of 0.05 used, and let a charge that reached the cap
# straight through.


async def test_a_write_in_flight_still_counts_against_the_allowance(actor, slow_db, free_env):
    """Cap 0.05 with 0.03 parked mid-write: 0.02 is left, not 0.05."""
    guard = await FreeTierGuard.start()

    assert await guard.charge("item_returned", 3) is False
    await slow_db.entered.wait()          # the background flush is now mid-write

    assert guard._in_flight == Decimal("0.03")
    assert guard._pending == Decimal("0")
    assert guard.remaining_usd == Decimal("0.02")
    assert guard.affordable("item_returned") == 2

    slow_db.release.set()
    await guard.close()

    assert guard._in_flight == Decimal("0")
    assert slow_db.total == Decimal("0.03")


async def test_a_charge_that_reaches_the_cap_mid_write_stops_the_run(actor, slow_db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 3)
    await slow_db.entered.wait()          # 0.03 parked in flight

    # 0.03 in flight + 0.02 now = the 0.05 cap. The stop drains the ledger
    # first, so let the parked write finish shortly after it starts waiting.
    asyncio.get_running_loop().call_later(0.01, slow_db.release.set)
    assert await guard.charge("item_returned", 2) is True

    assert guard.exhausted is True
    assert slow_db.total == Decimal("0.05")
    assert len([row for row in actor.pushed if row.get("free_tier_notice")]) == 1


async def test_a_failed_write_puts_the_amount_back(actor, slow_db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 3)
    await slow_db.entered.wait()

    slow_db.fail = True
    slow_db.release.set()
    await guard._flush_task

    assert guard._in_flight == Decimal("0")
    assert guard._pending == Decimal("0.03")         # queued for the next flush
    assert guard.remaining_usd == Decimal("0.02")    # still counted, never lost
    assert guard.tracking is True                    # one failure is not the breaker

    slow_db.fail = False
    await guard.close()
    assert slow_db.total == Decimal("0.03")          # the retry landed it


async def test_a_cancelled_write_puts_the_amount_back(actor, slow_db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 3)
    await slow_db.entered.wait()

    guard._flush_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await guard._flush_task

    assert guard._in_flight == Decimal("0")
    assert guard._pending == Decimal("0.03")
    assert guard.remaining_usd == Decimal("0.02")

    slow_db.release.set()
    await guard.close()
    assert slow_db.total == Decimal("0.03")


async def test_the_breaker_counts_a_failed_amount_exactly_once(actor, db, free_env):
    """The breaker deactivates mid-flush, while the failed amount is still in
    flight. It must be counted once then, and once after it is put back."""
    guard = await FreeTierGuard.start()
    db.fail = True
    seen_while_closing = []
    real_aclose = db.aclose

    async def aclose():
        seen_while_closing.append(guard._spent())
        await real_aclose()

    db.aclose = aclose

    for _ in range(3):
        await guard.charge("item_returned", 1)
        await asyncio.sleep(0)               # let the background flush fail

    assert guard.tracking is False           # the breaker tripped on the third failure
    assert seen_while_closing == [Decimal("0.03")]
    assert guard._in_flight == Decimal("0")
    assert guard._pending == Decimal("0.03")  # once, not twice


# ------------------------------ ask before buying: remaining_usd, affordable


def _paying(actor, db, monkeypatch):
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")


def _off_platform(actor, db, monkeypatch):
    actor._at_home = False


def _no_free_max(actor, db, monkeypatch):
    monkeypatch.delenv("FREE_MAX")


def _zero_free_max(actor, db, monkeypatch):
    monkeypatch.setenv("FREE_MAX", "0")


def _no_supabase(actor, db, monkeypatch):
    monkeypatch.delenv("SUPABASE_KEY")


def _no_run_identity(actor, db, monkeypatch):
    monkeypatch.delenv("APIFY_USER_ID")


def _old_sdk(actor, db, monkeypatch):
    monkeypatch.delattr(type(actor), "get_charging_manager", raising=False)


def _not_pay_per_event(actor, db, monkeypatch):
    actor._pricing.is_pay_per_event = False


def _database_down_at_start(actor, db, monkeypatch):
    db.fail = True


NO_FREE_LIMIT = pytest.mark.parametrize("setup", [
    _paying, _off_platform, _no_free_max, _zero_free_max, _no_supabase,
    _no_run_identity, _old_sdk, _not_pay_per_event, _database_down_at_start,
], ids=lambda f: f.__name__.strip("_"))


@NO_FREE_LIMIT
async def test_no_free_limit_means_none_not_zero(actor, db, free_env, monkeypatch, setup):
    """None tells the Actor the free tier does not limit this run, so go ahead.
    0 would wrongly tell it to stop a paying user."""
    setup(actor, db, monkeypatch)
    guard = await FreeTierGuard.start()
    warnings_before = list(actor.log.warnings)

    assert guard.tracking is False
    assert guard.remaining_usd is None
    assert guard.affordable("item_returned") is None
    assert guard.affordable("mystery_event") is None
    assert actor.log.warnings == warnings_before     # asking is silent when untracked


async def test_a_tripped_breaker_means_no_free_limit(actor, db, free_env):
    guard = await FreeTierGuard.start()
    assert guard.remaining_usd == Decimal("0.05")
    db.fail = True

    for _ in range(3):
        await guard.charge("item_returned", 1)
        await asyncio.sleep(0)

    assert guard.tracking is False
    assert guard.remaining_usd is None
    assert guard.affordable("item_returned") is None


async def test_blocked_at_start_leaves_nothing_affordable(actor, db, free_env):
    db.total = Decimal("0.05")

    guard = await FreeTierGuard.start()

    assert guard.blocked is True
    assert guard.remaining_usd == Decimal("0")
    assert guard.affordable("item_returned") == 0


async def test_exhausted_mid_run_leaves_nothing_affordable(actor, db, free_env):
    guard = await FreeTierGuard.start()
    for _ in range(5):
        await guard.charge("item_returned", 1)

    assert guard.exhausted is True
    assert guard.remaining_usd == Decimal("0")
    assert guard.affordable("item_returned") == 0

    await guard.close()
    assert guard.remaining_usd == Decimal("0")       # still 0 after close, not None


@pytest.mark.parametrize("free_max,spent,price,expected", [
    ("0.05", "0", "0.01", 5),
    ("0.05", "0.001", "0.01", 4),            # 0.049 left: floor, never round
    ("0.05", "0.045", "0.01", 0),            # tracking, not exhausted, can't pay for one
    ("1.00", "0", "0.0000121", 82644),       # the pilot's sub-cent price
])
async def test_affordable_is_the_floor_of_remaining_over_price(
    actor, db, free_env, monkeypatch, free_max, spent, price, expected,
):
    monkeypatch.setenv("FREE_MAX", free_max)
    db.total = Decimal(spent)
    actor._pricing.per_event_prices = {"item_returned": Decimal(price)}

    guard = await FreeTierGuard.start()

    assert guard.tracking is True
    assert guard.remaining_usd == Decimal(free_max) - Decimal(spent)
    assert guard.affordable("item_returned") == expected


async def test_remaining_counts_charges_not_yet_written(actor, db, free_env):
    guard = await FreeTierGuard.start()

    await guard.charge("item_returned", 2)           # the flush has not even started

    assert guard._pending == Decimal("0.02")
    assert guard.remaining_usd == Decimal("0.03")
    assert guard.affordable("item_returned") == 3


async def test_remaining_never_goes_below_zero(actor, db, free_env):
    """The ledger total can jump past the cap when another run by the same user
    flushes first. Remaining stops at 0, and affordable() says stop."""
    db.total = Decimal("0.02")
    guard = await FreeTierGuard.start()
    db.total = Decimal("0.08")                       # another run spent 0.06 meanwhile

    await guard.charge("item_returned", 1)
    await asyncio.sleep(0)                           # the flush returns the real total

    assert guard._known_total == Decimal("0.09")
    assert guard.exhausted is False
    assert guard.remaining_usd == Decimal("0")
    assert guard.affordable("item_returned") == 0


async def test_affordable_is_none_for_an_unpriced_event(actor, db, free_env):
    guard = await FreeTierGuard.start()

    assert guard.affordable("mystery_event") is None
    assert guard.affordable("mystery_event") is None
    await guard.charge("mystery_event", 1)

    warnings = [w for w in actor.log.warnings if "mystery_event" in w]
    assert len(warnings) == 1                        # one warning per event, shared with charge()
    assert guard.tracking is True


@pytest.mark.parametrize("free_max,calls_bought,rows_delivered,notice_used", [
    ("1.00", 5, 100, 1.00),    # every call fully deliverable; charge() stops at the cap
    ("0.95", 5, 95, 0.95),     # 15 of the 5th call's 20 deliverable: still worth buying
    ("0.85", 4, 80, 0.80),     # only 5 deliverable: exhaust() instead of buying the 5th
])
async def test_asking_first_never_buys_a_call_the_user_cannot_use(
    actor, db, free_env, monkeypatch, free_max, calls_bought, rows_delivered, notice_used,
):
    """The README pattern end to end: each upstream call returns d=20 rows and is
    worth buying only if the user can receive r=10 of them, at $0.01 a row."""
    monkeypatch.setenv("FREE_MAX", free_max)
    guard = await FreeTierGuard.start()
    d, r, calls, delivered = 20, 10, 0, 0

    stop = False
    while not stop:
        n = guard.affordable("item_returned")
        if n is not None and n < min(d, r):
            await guard.exhaust()
            break
        calls += 1                                  # buy the upstream call
        for _ in range(d):
            delivered += 1                          # push the row, then charge it
            if await guard.charge("item_returned", 1):
                stop = True
                break
    await guard.close()

    assert calls == calls_bought
    assert delivered == rows_delivered
    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    assert notice[0]["used_this_month_usd"] == notice_used
    assert guard.exhausted is True


async def test_affordable_is_none_for_a_zero_priced_event(actor, db, free_env):
    actor._pricing.per_event_prices = {"item_returned": Decimal("0.01"), "free_row": Decimal("0")}

    guard = await FreeTierGuard.start()

    assert guard.affordable("free_row") is None
    assert guard.affordable("item_returned") == 5
    assert actor.log.warnings == []


# ------------------------------------------------ exhaust(): the explicit stop


async def test_exhaust_stops_the_run_exactly_like_a_crossed_cap(actor, db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 2)

    await guard.exhaust()

    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    assert "returned 2 result(s)" in notice[0]["message"]
    assert notice[0]["used_this_month_usd"] == 0.02
    assert len(actor.status_messages) == 1
    assert "full free monthly allowance" in actor.status_messages[0]
    assert any("full free monthly allowance" in w for w in actor.log.warnings)
    assert guard.exhausted is True
    assert guard.tracking is False
    assert guard.remaining_usd == Decimal("0")
    assert guard.affordable("item_returned") == 0
    assert db.total == Decimal("0.02")               # the ledger was settled first
    assert db.closed is True


async def test_exhaust_is_idempotent(actor, db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)

    await guard.exhaust()
    calls_after_first = list(db.increment_calls)
    await guard.exhaust()

    assert len([row for row in actor.pushed if row.get("free_tier_notice")]) == 1
    assert len(actor.status_messages) == 1
    assert db.increment_calls == calls_after_first


async def test_concurrent_exhaust_calls_push_one_notice_row(actor, slow_db, free_env):
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)
    await slow_db.entered.wait()                     # the first stop will park in its drain

    asyncio.get_running_loop().call_later(0.01, slow_db.release.set)
    await asyncio.gather(guard.exhaust(), guard.exhaust())

    assert len([row for row in actor.pushed if row.get("free_tier_notice")]) == 1
    assert len(actor.status_messages) == 1


async def test_concurrent_charges_that_cross_the_cap_push_one_notice_row(actor, slow_db, free_env):
    """Actors that charge from concurrent workers used to get one notice row per
    worker that crossed the cap while the first stop was still settling up."""
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)
    await slow_db.entered.wait()

    asyncio.get_running_loop().call_later(0.01, slow_db.release.set)
    results = await asyncio.gather(
        guard.charge("item_returned", 4), guard.charge("item_returned", 4),
    )

    assert results == [True, True]
    assert len([row for row in actor.pushed if row.get("free_tier_notice")]) == 1
    assert len(actor.status_messages) == 1
    assert slow_db.total == Decimal("0.09")          # both workers' charges reached the ledger


@NO_FREE_LIMIT
async def test_exhaust_is_a_no_op_when_no_free_limit_applies(actor, db, free_env, monkeypatch, setup):
    setup(actor, db, monkeypatch)
    guard = await FreeTierGuard.start()
    get_calls, infos, warnings = db.get_calls, list(actor.log.infos), list(actor.log.warnings)

    await guard.exhaust()

    assert db.get_calls == get_calls
    assert db.increment_calls == []
    assert actor.pushed == []
    assert actor.status_messages == []
    assert actor.log.infos == infos
    assert actor.log.warnings == warnings
    assert guard.exhausted is False


async def test_exhaust_makes_no_database_call_for_a_paying_user(actor, db, free_env, monkeypatch):
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")

    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)
    await guard.exhaust()
    await guard.close()

    assert db.get_calls == 0
    assert db.increment_calls == []
    assert actor.pushed == []
    assert actor.status_messages == []


async def test_exhaust_after_being_blocked_does_not_announce_twice(actor, db, free_env):
    db.total = Decimal("0.05")
    guard = await FreeTierGuard.start()

    await guard.exhaust()

    assert len([row for row in actor.pushed if row.get("free_tier_notice")]) == 1
    assert len(actor.status_messages) == 1


async def test_nothing_is_metered_after_exhaust(actor, db, free_env):
    guard = await FreeTierGuard.start()
    await guard.record("item_returned", 1)
    await guard.exhaust()

    # Same as after a natural stop: `charge()` still performs the platform
    # charge and returns the platform's own flag. `guard.exhausted` is the
    # signal that stays set.
    assert await guard.record("item_returned", 3) is False
    assert await guard.charge("item_returned", 2) is False

    assert actor.charges == [("item_returned", 2)]
    assert guard._charged_rows == 1
    assert guard._pending == Decimal("0")
    assert guard._in_flight == Decimal("0")
    assert db.increment_calls == [Decimal("0.01")]   # only the drain inside exhaust()
    assert guard.remaining_usd == Decimal("0")


async def test_exhaust_respects_the_notice_row_switch(actor, db, free_env, monkeypatch):
    monkeypatch.setenv("FREE_TIER_NOTICE_ROW", "0")
    guard = await FreeTierGuard.start()

    await guard.exhaust()

    assert actor.pushed == []
    assert len(actor.status_messages) == 1
    assert guard.exhausted is True


async def test_close_after_exhaust_reasserts_the_reason(actor, db, free_env):
    guard = await FreeTierGuard.start()
    await guard.exhaust()
    await actor.set_status_message("Returned 25 places so far...")   # the Actor clobbers it

    await guard.close()

    assert "full free monthly allowance" in actor.status_messages[-1]


async def test_exhausted_wins_over_a_breaker_that_trips_while_settling_up(actor, db, free_env):
    """The run was ended by the allowance, so it reports 0 left, not None, even
    though the database also failed during the final drain."""
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 1)
    db.fail = True

    await guard.exhaust()

    assert any("continuing without it" in w for w in actor.log.warnings)   # breaker tripped
    assert guard.exhausted is True
    assert guard.remaining_usd == Decimal("0")
    notice = [row for row in actor.pushed if row.get("free_tier_notice")]
    assert len(notice) == 1
    assert notice[0]["used_this_month_usd"] == 0.01   # unwritten spend still reported


async def test_a_surprise_error_while_settling_up_still_ends_the_run(actor, db, free_env):
    """Anything a flush raises besides UsageDBError used to escape the stop path,
    skipping the explanation. The user must still be told why the run ended."""
    guard = await FreeTierGuard.start()

    async def boom(*_args):
        raise RuntimeError("client already closed")

    db.increment_usage = boom
    await guard.charge("item_returned", 1)

    await guard.exhaust()                            # must not raise

    assert guard.exhausted is True
    assert guard.tracking is False
    assert len([row for row in actor.pushed if row.get("free_tier_notice")]) == 1


async def test_record_cost_counts_a_write_already_in_flight(actor, slow_db, free_env):
    """Cost metering reads the same `_spent()` as charges (#12): with $0.03 parked
    mid-write, a $0.02 empty call reaches the $0.05 cap instead of looking free."""
    guard = await FreeTierGuard.start()
    await guard.charge("item_returned", 3)
    await guard.record_cost(Decimal("0.01"))      # covered by the rows: adds nothing
    await slow_db.entered.wait()                  # 0.03 parked in flight

    assert guard.remaining_usd == Decimal("0.02")
    asyncio.get_running_loop().call_later(0.01, slow_db.release.set)
    assert await guard.record_cost(Decimal("0.02")) is True

    assert guard.exhausted is True
    assert guard.remaining_usd == Decimal("0")
    assert slow_db.total == Decimal("0.05")
