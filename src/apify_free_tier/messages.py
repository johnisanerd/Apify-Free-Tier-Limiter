"""Every user-visible string the limiter emits, in one place.

These run in the public logs of every Actor in the fleet, so the wording is a
product decision, not an implementation detail. Keeping them here is the point
of the library: one standard voice across every Actor, changed in one commit.
"""

from __future__ import annotations

from decimal import Decimal


def money(value: Decimal | float | int) -> str:
    """Format a dollar amount without lying about small numbers.

    Per-event prices in this fleet go down to $0.00025, so a blanket two-decimal
    format would render a real allowance as "$0.00". Show more precision only
    when the amount is genuinely sub-cent.
    """
    amount = Decimal(str(value))
    if amount >= Decimal("0.01") or amount == 0:
        return f"${amount:.2f}"
    return f"${amount:.6f}".rstrip("0")


def disclosure(spent: Decimal, free_max: Decimal) -> str:
    """Told at the start of every tracked run, so usage tracking is never a surprise."""
    return (
        f"Free usage this month: {money(spent)} of {money(free_max)} for this Actor. "
        "Paid Apify accounts are never limited."
    )


def exhausted(free_max: Decimal) -> str:
    """Told when a free user hits the cap. Encouraging, with two ways forward."""
    return (
        f"You've used this Actor's full free monthly allowance ({money(free_max)}). "
        "We're glad it's been useful! To keep going, upgrade to a paid Apify account "
        "(paid users are never limited), or run it again from a different free account. "
        "Your free allowance resets on the 1st (UTC). Thanks for using this Actor!"
    )


def tracking_unavailable(reason: str) -> str:
    """The permissive path. An outage on our side must not break someone's run."""
    return (
        f"Free-tier usage tracking unavailable ({reason}); continuing without it. "
        "This run is not being counted against the free allowance."
    )


def sdk_too_old(found: str) -> str:
    return (
        f"Free-tier usage tracking needs the Apify SDK 3.0 or newer to read event "
        f"prices (found {found}); continuing without it."
    )


def paid_user() -> str:
    """Logged on every paid run of an opted-in Actor.

    Doubles as the install's proof of life: silence now means the library is not
    running, instead of being indistinguishable from a working paid-user skip.
    """
    return (
        "Paid Apify account detected - no free-tier limit applies to this run. "
        "Nothing is counted or restricted."
    )


def no_prices() -> str:
    """FREE_MAX was set, so somebody expected tracking. Never fail this silently."""
    return (
        "Free-tier usage tracking is configured, but this run reports no "
        "pay-per-event prices, so there is nothing to meter. This is expected "
        "when the Actor's owner starts the run, since owner runs are not charged."
    )


def unpriced_event(event_name: str, known: list[str]) -> str:
    return (
        f"No readable price for the '{event_name}' event "
        f"(known events: {', '.join(known) or 'none'}); "
        "that event is not counted against the free allowance."
    )
