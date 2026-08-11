# Rollout status

Which Actors have the free-tier cap enabled. Snapshot below; regenerate any time with:

```bash
python3 scripts/list_installs.py --markdown
```

That scan is the source of truth — it reads the live Actor configuration rather than
this file, and it exits non-zero if it finds a secret `FREE_MAX`, missing Supabase
variables, or a test flag left switched on.

## Enabled (3 of 102 Actors, as of 2026-08-11)

| Actor | Actor ID | FREE_MAX | Status |
| --- | --- | --- | --- |
| `johnvc/store-actor-intelligence-api` | `WzsyD0afch5fKHGn5` | $1.00 | OK |
| `johnvc/google-images-api` | `bvAQMqCbp6wE53JzK` | $1.00 | OK |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | $1.00 | OK |

Each was verified on-platform on both paths: a paying account logs the "no limit
applies" line and writes nothing, and a forced-free run writes a ledger row whose amount
matches the tier-resolved price.

## What each one taught us

Every install so far has hit a different integration shape, which is worth knowing
before picking the next one.

**`store-actor-intelligence-api`** (pilot) — the common fleet shape: a local
`_charge(event, count) -> bool` helper, batch charging after `push_data`. The swap is
one line. Because it charges per *batch*, a free user can overshoot the cap by up to a
whole batch.

**`google-images-api`** — same charge shape, per item. The trap was the **Dockerfile**:
it installs from `requirements.txt`, so editing `pyproject.toml` and running `uv lock`
changed nothing the image saw. The build succeeded, reused a cached layer with no
library in it, and the Actor logged nothing at runtime. Fix: regenerate with
`uv export --no-hashes --format requirements-txt > requirements.txt`.

**`YoutubeTranscripts`** — no loop to break. Every video is launched at once with
`asyncio.gather` and queues on a semaphore, so stopping needs a shared `asyncio.Event`
checked *after* a task acquires the semaphore. Checking at the top of the worker looks
right and never fires: `gather` starts every coroutine immediately, so all of them would
check before anything was charged. It also has two versions (0.0 and 0.5) both tagged
`latest`; **0.0** is the one that builds.

## Live usage

Real external free accounts, current month:

| Actor | Free users | Total metered | Heaviest single user | Charges |
| --- | --- | --- | --- | --- |
| `google-images-api` | 6 | $0.4995 | **$0.4693** | 4,857 |
| `store-actor-intelligence-api` | 2 | $0.0009 | $0.0005 | 6 |
| `YoutubeTranscripts` | 4 | $0.0001 | $0.0001 | 11 |

Query it directly:

```sql
select actor_id, count(*) as free_users, round(sum(spent_usd), 6) as total_spent,
       round(max(spent_usd), 6) as heaviest_user, sum(charge_count) as charges
from free_tier_usage
where period = to_char(now() at time zone 'utc', 'YYYY-MM')
group by actor_id order by total_spent desc;
```

One `google-images-api` user reached roughly **47% of a $1 monthly allowance within an
hour** of the cap going live — the case this exists for. Worth watching before choosing
`FREE_MAX` elsewhere.

## Choosing the next Actors

Priority is the loss leaders and anything cheap enough to be worth bulk-running for
free. Before starting, check the two things that differ per Actor:

```bash
grep -E "COPY (requirements.txt|pyproject.toml)" Dockerfile   # which file to update
grep -n "_charge\|Actor.charge" src/main.py                   # the charge seam
```

Then follow [INSTALL.md](INSTALL.md).
