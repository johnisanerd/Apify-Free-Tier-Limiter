"""The free-tier guard: a drop-in replacement for the fleet's local `_charge`.

Shape of the thing
------------------
`charge(event_name, count)` keeps the exact contract every Actor in this fleet
already relies on: it performs the platform charge, never raises, and returns
True when the caller should stop pushing and charging. On top of that it counts
what a *free* user has spent this month and stops them at FREE_MAX.

Three properties matter more than the feature itself:

1. Paying users are untouched. Not throttled, not counted, not slowed down by a
   single network call. The guard turns itself off before it does anything.
2. The hot path never blocks. Per-item Actors in this fleet charge hundreds of
   times per run; a synchronous round trip per charge would add minutes. Charges
   accumulate locally and flush in the background, with at most one flush in
   flight. Enforcement reads `known_total + pending`, so it is correct even
   while a flush is outstanding.
3. It fails permissive. Every failure mode - no config, old SDK, database down,
   unreadable price - logs once and continues untracked. Our own infrastructure
   having a bad day must never break someone else's run.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version

from apify import Actor

from . import messages
from .db import UsageDB, UsageDBError

# After this many consecutive flush failures the guard stops trying. Without it
# a dead database would mean one doomed request per charge for the whole run.
_MAX_CONSECUTIVE_FAILURES = 3

# Matches the per-call ceiling enforced in increment_usage(). Anything larger is
# split across flushes rather than being rejected outright.
_MAX_FLUSH_AMOUNT = Decimal("10")

_ZERO = Decimal("0")


class FreeTierGuard:
    """Created by `FreeTierGuard.start()`. Do not instantiate directly."""

    def __init__(self) -> None:
        self._active = False          # tracking on? False = inert passthrough
        self._blocked = False         # already over the cap before any work began
        self._exhausted = False       # the cap ended this run, at start or mid-run
        self._free_max = _ZERO
        self._known_total = _ZERO     # last authoritative total from the database
        self._pending = _ZERO         # charged locally, not yet flushed
        self._prices: dict[str, Decimal] = {}
        self._db: UsageDB | None = None
        self._user_id = ""
        self._actor_id = ""
        self._flush_task: asyncio.Task[None] | None = None
        self._failures = 0
        self._warned_events: set[str] = set()

    # ------------------------------------------------------------------ start

    @classmethod
    async def start(cls) -> FreeTierGuard:
        """Set the guard up. Makes at most one awaited database call. Never raises."""
        guard = cls()
        try:
            await guard._configure()
        except Exception as exc:  # noqa: BLE001 - permissive by design
            guard._active = False
            Actor.log.warning(messages.tracking_unavailable(type(exc).__name__))
        return guard

    @property
    def blocked(self) -> bool:
        """True when this free user was already over the cap at run start.

        The caller should return immediately. The reason has already been logged
        and set as the run's terminal status message.
        """
        return self._blocked

    @property
    def exhausted(self) -> bool:
        """True once the cap has ended this run, whether at start or mid-run.

        Actors in this fleet set their own terminal status message on the way
        out, which would overwrite the guard's. Check this before doing that:

            if guard.exhausted:
                pass                      # the guard already said why
            else:
                await Actor.set_status_message(f"Done. {n} rows.", is_terminal=True)
        """
        return self._exhausted

    @property
    def tracking(self) -> bool:
        """True when this run is actually being counted. Useful in tests and logs."""
        return self._active

    async def _configure(self) -> None:
        if os.getenv("FREE_TIER_DEBUG") == "1":
            _log_environment()

        # Off-platform (local dev, CI) there is no user and nothing to protect.
        if not Actor.is_at_home():
            return

        # The whole point: paying users are never limited. FREE_TIER_FORCE lets
        # us exercise the free path from a paid account during verification.
        forced = os.getenv("FREE_TIER_FORCE") == "1"
        if os.getenv("APIFY_USER_IS_PAYING") == "1" and not forced:
            return

        # No FREE_MAX means this Actor has not opted in. That is the normal state
        # for most of the fleet, so it is silent: the library is safe to install
        # everywhere and enable per Actor.
        free_max = _decimal_or_none(os.getenv("FREE_MAX"))
        if free_max is None or free_max <= 0:
            return
        self._free_max = free_max

        url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
        if not url or not key:
            Actor.log.warning(messages.tracking_unavailable("not configured"))
            return

        if not _sdk_supports_pricing():
            Actor.log.warning(messages.sdk_too_old(_sdk_version()))
            return

        self._user_id = os.getenv("APIFY_USER_ID", "")
        self._actor_id = os.getenv("APIFY_ACTOR_ID", "")
        if not self._user_id or not self._actor_id:
            Actor.log.warning(messages.tracking_unavailable("run identity unavailable"))
            return

        # Read every event price once. Doing this per charge would mean a
        # platform call inside the hot loop for a number that cannot change
        # mid-run.
        self._prices = _read_event_prices()
        if not self._prices:
            # Nothing to meter: either the Actor is not pay-per-event, or the
            # platform reported no prices for this run (which is what happens
            # when the Actor's own owner starts it - owner runs are not
            # charged). Say so: FREE_MAX being set means someone expected
            # tracking, and a silent no-op here is impossible to debug.
            Actor.log.info(messages.no_prices())
            return

        self._db = UsageDB(url, key)
        spent = await self._get_usage_with_retry()
        if spent is None:
            await self._deactivate()
            return

        self._known_total = spent
        Actor.log.info(messages.disclosure(spent, self._free_max))

        if spent >= self._free_max:
            self._blocked = True
            await self._announce_exhausted()
            await self._deactivate()
            return

        self._active = True

    async def _get_usage_with_retry(self) -> Decimal | None:
        """One retry: startup is the only place we can afford to wait."""
        assert self._db is not None
        for attempt in (1, 2):
            try:
                return await self._db.get_usage(self._user_id, self._actor_id)
            except UsageDBError as exc:
                if attempt == 2:
                    Actor.log.warning(messages.tracking_unavailable(str(exc)))
                    return None
        return None

    # ----------------------------------------------------------------- charge

    async def charge(self, event_name: str, count: int = 1) -> bool:
        """Charge an event. Returns True when the caller should stop.

        Same contract as the local `_charge` helper this replaces: the
        `is_at_home()` gate lives in here, and it never raises.
        """
        limit_reached = await self._platform_charge(event_name, count)

        if not self._active or count <= 0:
            return limit_reached

        price = self._prices.get(event_name)
        if price is None:
            if event_name not in self._warned_events:
                self._warned_events.add(event_name)
                Actor.log.warning(messages.unpriced_event(event_name, sorted(self._prices)))
            return limit_reached

        self._pending += price * count
        self._schedule_flush()

        # `known_total + pending` is the whole reason the background flush is
        # safe: the in-flight amount is still counted here.
        if self._known_total + self._pending >= self._free_max:
            await self._stop()
            return True

        return limit_reached

    async def _platform_charge(self, event_name: str, count: int) -> bool:
        if not Actor.is_at_home() or count <= 0:
            return False
        try:
            result = await Actor.charge(event_name, count)
            return bool(getattr(result, "event_charge_limit_reached", False))
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning(f"Failed to charge '{event_name}' x{count}: {exc}")
            return False

    # ------------------------------------------------------------------ flush

    def _schedule_flush(self) -> None:
        """At most one flush in flight. Extra charges ride along in `_pending`."""
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        if self._db is None or self._pending <= 0:
            return

        amount = min(self._pending, _MAX_FLUSH_AMOUNT)
        self._pending -= amount
        try:
            self._known_total = await self._db.increment_usage(
                self._user_id, self._actor_id, amount
            )
            self._failures = 0
        except UsageDBError as exc:
            # Put it back so the next flush retries it, rather than losing
            # money we already let the user spend.
            self._pending += amount
            self._failures += 1
            if self._failures >= _MAX_CONSECUTIVE_FAILURES:
                Actor.log.warning(messages.tracking_unavailable(str(exc)))
                await self._deactivate()

    async def _drain(self) -> None:
        """Await the in-flight flush, then push whatever is left. Bounded.

        The loop exists because a flush sends at most _MAX_FLUSH_AMOUNT; the
        bound stops a persistently failing database from spinning here.
        """
        if self._flush_task is not None and not self._flush_task.done():
            try:
                # shield: a timeout here must not cancel a write already in progress.
                await asyncio.wait_for(asyncio.shield(self._flush_task), timeout=5.0)
            except Exception:  # noqa: BLE001 - includes TimeoutError
                pass
        for _ in range(3):
            if not self._active or self._pending <= 0:
                return
            await self._flush()

    # ------------------------------------------------------------------- stop

    async def _stop(self) -> None:
        """The cap was reached mid-run. Settle up, tell the user, go quiet."""
        await self._drain()
        await self._announce_exhausted()
        await self._deactivate()

    async def _announce_exhausted(self) -> None:
        self._exhausted = True
        message = messages.exhausted(self._free_max)
        Actor.log.warning(message)
        try:
            await Actor.set_status_message(message, is_terminal=True)
        except Exception:  # noqa: BLE001 - a status message is never worth failing over
            pass

    async def _deactivate(self) -> None:
        self._active = False
        if self._db is not None:
            await self._db.aclose()
            self._db = None

    async def close(self) -> None:
        """Final flush. Call from the Actor's `finally`. Never raises."""
        try:
            await self._drain()
        except Exception:  # noqa: BLE001
            pass
        await self._deactivate()


# --------------------------------------------------------------------- helpers


def _log_environment() -> None:
    """FREE_TIER_DEBUG=1. Reports what the guard can see, never a secret value.

    Rolling this out across a fleet means diagnosing "why is it inert?" over and
    over, and the answer is almost always a missing variable or an empty price
    map. Presence and shape are enough to tell which.
    """
    seen = {
        name: ("set" if os.getenv(name) else "MISSING")
        for name in ("APIFY_USER_ID", "APIFY_ACTOR_ID", "APIFY_USER_IS_PAYING",
                     "FREE_MAX", "SUPABASE_URL", "SUPABASE_KEY")
    }
    seen["APIFY_USER_IS_PAYING"] = os.getenv("APIFY_USER_IS_PAYING") or "MISSING"
    seen["FREE_MAX"] = os.getenv("FREE_MAX") or "MISSING"  # not a secret in practice
    Actor.log.info(f"[free-tier debug] env: {seen}")
    Actor.log.info(f"[free-tier debug] is_at_home={Actor.is_at_home()} sdk={_sdk_version()}")
    try:
        info = Actor.get_charging_manager().get_pricing_info()
        Actor.log.info(
            f"[free-tier debug] is_pay_per_event="
            f"{getattr(info, 'is_pay_per_event', None)} "
            f"per_event_prices={getattr(info, 'per_event_prices', None)}"
        )
    except Exception as exc:  # noqa: BLE001
        Actor.log.info(f"[free-tier debug] pricing read failed: {type(exc).__name__}: {exc}")


def _decimal_or_none(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    try:
        return Decimal(raw.strip().lstrip("$"))
    except (InvalidOperation, AttributeError):
        return None


def _sdk_version() -> str:
    try:
        return version("apify")
    except PackageNotFoundError:
        return "unknown"


def _sdk_supports_pricing() -> bool:
    """Checked every run, not just at install: the SDK is the Actor's dependency."""
    return hasattr(Actor, "charge") and hasattr(Actor, "get_charging_manager")


def _read_event_prices() -> dict[str, Decimal]:
    """Per-event prices for this run, already resolved to the payer's tier.

    Returns an empty map when the Actor is not on pay-per-event pricing, or when
    the platform will not tell us. Unlike the Actors' own preflight check, an
    unreadable price here is not fatal: the run is still perfectly valid, we
    just cannot meter it.
    """
    try:
        info = Actor.get_charging_manager().get_pricing_info()
    except Exception:  # noqa: BLE001
        return {}

    if not getattr(info, "is_pay_per_event", False):
        return {}

    prices = getattr(info, "per_event_prices", None) or {}
    return {name: Decimal(str(price)) for name, price in prices.items() if price is not None}
