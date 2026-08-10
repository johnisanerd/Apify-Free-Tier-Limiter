# Apify Free-Tier Limiter — Design

> A shared, public Python library that every Actor in the fleet pulls in via Docker.
> It caps how much compute a **free Apify user** can consume on a **loss-leader / free
> Actor**, protecting the owner's wallet, while leaving **paying users unthrottled**.
>
> Origin: handwritten notebook "Free Users: August Solution, 2026" (pages 34–35).
> The raw notes are preserved at the bottom under [Original notes](#original-notes).

## Goal (confirmed)

Protect the owner's wallet on free/loss-leader Actors — **not** throttle paying users.
When a free user burns through the per-Actor subsidy we've allotted them for the
month, the Actor shuts itself down gracefully with an upgrade message.

**Why do it at all** (the upside that justifies the cost):
- Drives paid conversions / recommendations.
- Builds users, exposure, SEO.
- Loss leaders for paying customers.

**The cost it controls:** up to ~$5/mo/user of subsidized compute on free Actors.

## Resolved decisions

| # | Decision | Choice |
| --- | --- | --- |
| 1 | Distribution | **Public** library, pulled in at Docker build. No secrets in code; config via env vars. |
| 2 | Overspend race | **Atomic increment** via a Supabase SQL RPC (check-and-add in one call). Accept a few cents of slippage. |
| 3 | DB unreachable | **Permissive, documented.** Log once and continue untracked; circuit-break after 3 consecutive flush failures. _(Supersedes the earlier fail-closed call: an outage on our side should not break someone else's run, and the exposure is bounded to free users on loss-leader Actors.)_ |
| 4 | User identity | **Native Apify env vars** (see research below). Confirmed the run sees the *caller's* ID, not the owner's. |
| 5 | Free vs paid check | Read `APIFY_USER_IS_PAYING`. No DB call, no off-platform table for v1. |
| 6 | Monthly window | **Calendar month** via a `period` key (e.g. `"2026-08"`). Auto-resets, no cron. |
| 7 | Enforcement cadence | Every charge round, not just at startup (a single run can otherwise overshoot). |
| 8 | Hot path | **Never blocks.** Charges accumulate locally; at most one background flush in flight; enforcement reads `known_total + pending`. Per-item Actors charge hundreds of times per run, so a synchronous round trip per charge would add minutes. |
| 9 | Distribution | Pinned **release-tag tarball** via `[tool.uv.sources]` — reproducible, and needs no `git` binary in the Actor image. |
| 10 | SDK floor | apify **≥3.0** (`get_charging_manager` + `get_pricing_info` landed in 3.0.0). Enforced at install *and* probed at runtime; too old → inert, never a crash. |
| 11 | Pilot | ApifyApifyScraper. |

## Research findings (verified)

From the [Apify env-vars docs](https://docs.apify.com/platform/actors/development/programming-interface/environment-variables):

- **`APIFY_USER_ID`** — *"ID of the user who started the Actor. May differ from the
  Actor owner."* → keys on the caller. ✅
- **`APIFY_USER_IS_PAYING`** — *"If it is `1`, it means that the user who started the
  Actor is a paying user."* → the free/paid gate is a single env read.
- **`APIFY_ACTOR_ID`** — the running Actor's ID.

**To verify empirically before fleet-wide trust:** that `APIFY_USER_IS_PAYING` is
reliably populated on *public* runs (cheap to confirm on a live run).

**Nuance:** "paying" = paying *Apify* user, not necessarily a paying *our-product*
customer. Fine for v1. A v2 Supabase allowlist could exempt our own
email-signup/bonus customers too.

## Variables gathered at start

| Variable | Source |
| --- | --- |
| Actor ID | `APIFY_ACTOR_ID` (native) |
| User ID | `APIFY_USER_ID` (native — the caller) |
| Free vs paid | `APIFY_USER_IS_PAYING` (native) |
| `FREE_MAX` (dollars) | custom env var, set per Actor |
| `SUPABASE_URL` / `SUPABASE_KEY` | per-Actor secret env vars |

## Function design

A tiny class so each Actor integrates in ~3 lines:

```python
from apify_free_tier import FreeTierGuard

guard = FreeTierGuard.from_env()   # reads the vars above
await guard.start()                # gate: if free AND already >= FREE_MAX -> log + graceful exit

# ...inside the charge/delivery loop, each time we charge:
await guard.charge(amount)         # atomic increment; if now >= FREE_MAX -> log + graceful shutdown
```

Underlying pieces (maps to the notes' Initiate / Check / Increment):

1. **`is_free_user()`** — `APIFY_USER_IS_PAYING != "1"`. No DB. If paid, the guard is a no-op.
2. **`get_usage(user, actor, period)`** — Supabase read of this month's consumption.
3. **`increment_usage(amount)`** — atomic RPC; returns the new total.
4. **orchestrator** — `start()` / `charge()` / `shutdown()` tie them together and enforce every round.

## Supabase schema (draft)

One row per user × actor × month:

```
usage (
  user_id   text,
  actor_id  text,
  period    text,        -- "2026-08"
  spent_usd numeric,     -- accumulated subsidized spend
  updated_at timestamptz,
  primary key (user_id, actor_id, period)
)
```

Atomic increment via an RPC (`increment_usage(user_id, actor_id, period, amount)`)
that upserts and returns the new `spent_usd` in a single call.

## Open questions

_(to fill in as we discuss)_

1. Where does `charge(amount)` get the dollar amount — mirror the exact `Actor.charge()`
   PPE amount, or a compute estimate? (Needs a source of truth so the cap means real dollars.)
2. Which Supabase key/policy — locked-down RLS + RPC-only, since the key rides in each
   Actor's env?
3. Async vs sync API (Apify Python SDK is async)?

---

## Original notes

### Page 34 — trade-off + solution

**(+)** 1. Drive Paid Recs. 2. Can help build users, exposure, SEO. 3. Loss leaders for paying customers.
**(−)** 1. Cost up to $5/mo/user.

**Solution:** Use an external DB to monitor, log, and limit monthly consumption. A free
user can be limited from using their full five dollars — cutting them off at $1 or 50¢.
**Bonus:** capture them off-platform via a bonus / e-mail signup. If a bot swarm uses our
actor, per-user caps force the swarm to use 5–10× more users, inflating our user stats.

**Implementation:** Supabase DB tracks user↔actor usage. Actor has an env var
`max_free_use` (`Free_MAX`), set per actor. A Python script in a separate repo checks if
it's a free actor and whether free use is surpassed, incrementing the DB with costs —
checks and increments every cost round.

### Page 35 — deploy + functions

**Deploy/maintain:** Standard; separate private GitHub repo; add to an Actor repo via a
read-only GitHub token. _(Superseded by decision #1: public library, no token.)_

**Initiate Fxn** (first thing an Actor fires up): get variables (Actor, `Free_MAX`, User
Name); connect to Supabase, get usage for username × Actor; quit gracefully if maxed, log
a message. **Check Fxn:** checks if we've gone beyond the total allowable amount.
**Increment Fxn:** adds some amount charged to the DB.
