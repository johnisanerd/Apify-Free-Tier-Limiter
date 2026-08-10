# Apify Free-Tier Limiter

A monthly usage cap for free users of [Apify](https://apify.com/?fpr=9n7kx3) Actors.

Free and loss-leader Actors are worth running: they drive conversions, exposure, and
search traffic. What they cannot do is cost an unbounded amount. This library caps how
much a **free** Apify user can consume from a given Actor in a calendar month, and stops
them with a friendly message when they hit it.

**Paying Apify users are never limited, never counted, and never slowed down.** The guard
checks `APIFY_USER_IS_PAYING` and turns itself off before it does anything else.

## How it works

A single Supabase counter row per (user, actor, month). One read at run start, atomic
increments in the background as the run charges. The counter is the enforcement
authority; there is no event log on the hot path.

```
run starts ──> paying user?  ──yes──> inert, zero overhead
                    │no
                    ▼
             read usage (1 RPC) ──over FREE_MAX?──yes──> log + stop gracefully
                    │no
                    ▼
             charge loop: count locally, flush in background,
                          stop the moment known + pending >= FREE_MAX
```

## Install

Actors in this fleet install from a pinned release tag, so a build is always
reproducible. Add to the Actor's `pyproject.toml`:

```toml
dependencies = [
    "apify-free-tier",
]

[tool.uv.sources]
apify-free-tier = { url = "https://github.com/johnisanerd/Apify-Free-Tier-Limiter/archive/refs/tags/v0.1.0.tar.gz" }
```

Then re-lock so the Docker build picks it up:

```bash
uv lock
```

The tarball form is deliberate: it needs no `git` binary inside the Actor image.

## Use

`charge()` is a drop-in for the `_charge(event_name, count) -> bool` helper these Actors
already use. Same contract: it performs the platform charge, never raises, and returns
`True` when the caller should stop.

```python
from apify_free_tier import FreeTierGuard

async def main() -> None:
    async with Actor:
        guard = await FreeTierGuard.start()
        if guard.blocked:
            return          # already logged and set the run's terminal status
        try:
            for row in rows:
                await Actor.push_data(row)
                if await guard.charge("item_returned", 1):
                    break
        finally:
            await guard.close()   # final flush
```

## Configuration

Set these on the Actor (Console → Settings → Environment variables). Mark the Supabase
values as secret.

| Variable | Required | Meaning |
| --- | --- | --- |
| `FREE_MAX` | to enable | Dollars a free user may spend on this Actor per calendar month, e.g. `0.50`. **Absent means tracking is off** — the library is safe to install fleet-wide and enable per Actor. |
| `SUPABASE_URL` | with `FREE_MAX` | Usage-ledger project URL. |
| `SUPABASE_KEY` | with `FREE_MAX` | Publishable/anon key. It can only execute the two RPCs. |
| `FREE_TIER_FORCE` | no | `1` treats every caller as free. For verifying the free path from a paid account. Remove afterwards. |

`APIFY_USER_ID`, `APIFY_ACTOR_ID`, and `APIFY_USER_IS_PAYING` are supplied by the
platform. `APIFY_USER_ID` is the user who *started* the run, not the Actor's owner.

Amounts mirror the Actor's real pay-per-event prices, read at run start from
`ChargingManager.get_pricing_info()` and already resolved to the payer's tier. Nothing is
hardcoded.

## Failure behaviour: permissive, on purpose

Every failure path logs once and lets the run continue **untracked**:

- Supabase unreachable or slow (2.5s timeout, one retry at start)
- three consecutive flush failures (circuit breaker; stops retrying for the rest of the run)
- `FREE_MAX` unset or unparseable, Supabase not configured
- Apify SDK older than 3.0, or an Actor that is not on pay-per-event pricing
- an event with no readable price (warned once, that event is not counted)

The alternative — refusing to run when our own database is down — punishes users for our
outage. The exposure is small and bounded: a free user, during an outage, on a
loss-leader Actor.

Known limitation: enforcement is per Apify account, so someone can reset their allowance
by using a different free account. That is understood and priced in; it also means a bot
swarm has to burn 5–10× more accounts to keep going.

## What the user sees

```
INFO  Free usage this month: $0.02 of $0.50 for this Actor. Paid Apify accounts are never limited.
WARN  You've used this Actor's full free monthly allowance ($0.50). We're glad it's been
      useful! To keep going, upgrade to a paid Apify account (paid users are never
      limited), or run it again from a different free account. Your free allowance resets
      on the 1st (UTC). Thanks for using this Actor!
```

## Database

`migrations/0001_free_tier_usage.sql` creates the table and the two RPCs. The key that
ships in each Actor must be assumed public, so the table has RLS enabled with **zero
policies** and direct grants revoked. The only reachable surface is
`get_usage(user, actor)` and `increment_usage(user, actor, amount)`, both
`SECURITY DEFINER`, both rejecting negative amounts and amounts above a per-call ceiling.
The month key is computed server-side in UTC, so the monthly reset needs no cron.

## Tests

```bash
uv sync --extra dev && uv run pytest
```

## Roadmap

- Allowlist so our own email-signup and bonus users are exempt too
- Append-only `usage_events` table for forensics, off the hot path
- Edge Function validating the run against the Apify API, closing the griefing hole where
  an extracted key can inflate someone else's counter
- PyPI release with provenance

## License

MIT
