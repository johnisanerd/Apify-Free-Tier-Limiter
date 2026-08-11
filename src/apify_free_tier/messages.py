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


def notice_row(free_max: Decimal, spent: Decimal, resets_on: str, rows_returned: int | None) -> dict:
    """The explanation as a dataset row, for people who never read the log.

    Most consumers reach an Actor through the API, an MCP client, or an
    integration, and only ever see the dataset. To them a capped run looks like
    an Actor that returned nothing and said nothing - indistinguishable from
    broken. This row is the difference between "it stopped and told me why" and
    a one-star review.

    Deliberately verbose: it is the only place the full story is guaranteed to
    reach the user.
    """
    got = (
        f"This run returned {rows_returned} result(s) before stopping."
        if rows_returned else
        "This run returned no results because the allowance was already used up."
    )
    return {
        # Both spellings: the fleet is split between result_type and resultType,
        # and this row should be recognisable whichever one a consumer filters on.
        "result_type": "free_tier_limit_reached",
        "resultType": "free_tier_limit_reached",
        "message": (
            f"Free monthly allowance reached for this Actor. {got} "
            f"Free Apify accounts get {money(free_max)} of usage on this Actor per calendar "
            f"month, and this account has now used {money(spent)}. Nothing is wrong with the "
            "Actor or your input. "
            "To continue: upgrade to a paid Apify account, which is never limited by this cap "
            "and lets you run the Actor as much as you like; or wait for the allowance to reset; "
            f"or run it from a different free account. The allowance resets on {resets_on}."
        ),
        "free_allowance_usd": float(free_max),
        "used_this_month_usd": float(spent),
        "allowance_resets_on": resets_on,
        "how_to_continue": "Upgrade to a paid Apify account (never limited), or wait for the monthly reset.",
        "is_error": False,
    }


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
