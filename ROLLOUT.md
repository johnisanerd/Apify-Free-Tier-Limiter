# Rollout status

Which Actors have the free-tier cap enabled. Snapshot below; regenerate any time with:

```bash
python3 scripts/list_installs.py --markdown
```

A tracking spreadsheet lands at `~/Desktop/Apify/free-tier-limiter-rollout.csv`:

```bash
python3 scripts/export_rollout_csv.py --usage-json /tmp/usage.json
```

Add an `INTEGRATIONS` entry to that script on every new install — it carries the facts
no API knows (repo, charge event, which file the Dockerfile installs from). Drop
`--usage-json` to skip the usage columns; build the file from the aggregate query under
[Live usage](#live-usage), since the anon key cannot read aggregates.

That scan is the source of truth — it reads the live Actor configuration rather than
this file, and it exits non-zero if it finds a secret `FREE_MAX`, missing Supabase
variables, or a test flag left switched on.

## Enabled (21 of 104 Actors, as of 2026-08-24)

| Actor | Actor ID | FREE_MAX | Library | Status |
| --- | --- | --- | --- | --- |
| `johnvc/store-actor-intelligence-api` | `WzsyD0afch5fKHGn5` | $1.00 | v0.1.7 | OK |
| `johnvc/google-images-api` | `bvAQMqCbp6wE53JzK` | $1.00 | v0.1.7 | OK |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | $1.00 | v0.1.7 | OK |
| `johnvc/google-maps-places-api` | `WQbrHYgrJV5fP6b09` | $1.00 | v0.1.7 | OK |
| `johnvc/google-news-lite-api` | `Sl7mQJeH9MvLhgGYy` | $1.00 | v0.1.7 | OK |
| `johnvc/google-shopping-lite-api` | `YrCMNywfEbYqWpgdF` | $1.00 | v0.1.7 | OK |
| `johnvc/google-scholar-lite-api` | `ChRMxpDtEqlJHZDga` | $1.00 | v0.1.7 | OK |
| `johnvc/Scrape-Yandex` | `y7gc70pJD81ubH2I9` | $2.50 | v0.1.7 | OK |
| `johnvc/yandex-reverse-image-search` | `FdyxaCtHdVcA1FBDm` | $2.50 | v0.1.7 | OK |
| `johnvc/yandex-...-per-result` | `enUmNny2eNO4pE269` | $2.50 | v0.1.7 | OK |
| `johnvc/google-autocomplete-api` | `VVMGjb2KwyOPsXcwU` | $1.00 | v0.1.7 | OK |
| `johnvc/google-hotels-search-scraper` | `ahpk7S3a62kOzKdE9` | $1.00 | v0.1.7 | OK |
| `johnvc/apple-app-store-reviews-api` | `k3dKElhh0XK52g619` | $1.00 | v0.1.7 | OK |
| `johnvc/Google-AI-Overview-API` | `XqEZodkkqvqAtiSkV` | $1.00 | v0.1.7 | OK |
| `johnvc/google-events-api-...` | `DfdUgh7nBLKe78irv` | $1.00 | v0.1.7 | OK |
| `johnvc/google-scholar-api` | `m22qEjpnfxa4H1ijE` | $1.00 | v0.1.7 | OK |
| `johnvc/google-local-api` | `bZ3PtlNaHVObbbR4O` | $1.00 | v0.1.7 | OK |
| `johnvc/Baidu-Search-Scraper` | `hDVd9ZQQHglV5LZ1A` | $1.00 | v0.1.7 | OK |
| `johnvc/google-shopping-api-...` | `U02ytMsu6ynITFJHX` | $1.00 | v0.1.7 | OK |
| `johnvc/us-congress-financial-...` | `xxCgm38ifv9HcLl9z` | $1.00 | v0.1.7 | OK |
| `johnvc/jazzhr-jobs-api` | `Kv1kG2WbLlEvSe4Yc` | $1.00 | v0.1.7 | OK |

Each was verified on-platform on both paths: a paying account logs the "no limit
applies" line and writes nothing, and a forced-free run writes a ledger row whose amount
matches the tier-resolved price.

## What each one taught us

Twenty-one installs, and the charge shape has differed more often than it has repeated.
Check the shape before you start — the two questions that decide the whole integration
are in [Choosing the next Actors](#choosing-the-next-actors).

**`store-actor-intelligence-api`** (pilot) — the common fleet shape: a local
`_charge(event, count) -> bool` helper, batch charging after `push_data`. One-line swap.

**`google-images-api`** — same shape, per item. The trap was the **Dockerfile**: it
installs from `requirements.txt`, so editing `pyproject.toml` and running `uv lock`
changed nothing the image saw. The build succeeded on a cached layer with no library in
it, and the Actor logged nothing.

**`YoutubeTranscripts`** — no loop to break. Every video launches at once via
`asyncio.gather` and queues on a semaphore, so stopping needs an `asyncio.Event` checked
*after* a task acquires the semaphore. Two versions both tagged `latest`; **0.0** builds.

**`google-maps-places-api`**, **`google-scholar-lite-api`** — inline `Actor.charge` with
no helper, per item. Straightforward.

**`google-news-lite-api`** — its `_charge` returned `None` and deliberately never stopped
the run, on the reasoning that the platform's charge limit should not truncate output.
The free-tier cap is a different question, so the article loop now breaks and a flag
carries that out of the enclosing term loop.

**`google-shopping-lite-api`** — charges *before* storing and slices the batch to the
billed count. `charge()` returns a bool, so adopting it would have meant either billing
twice or losing that count. This is why `record()` exists: the Actor keeps its own
`_charge` call and meters the billed number. `allowance_spent` is kept separate from the
existing `charge_limit_hit`, whose message ("storing more would be unbilled") is the
wrong explanation for a free user who simply used up the month.

**`Scrape-Yandex`**, **`yandex-...-per-result`** — on apify SDK **2.7.3**, which disproved
the library's `>=3.0` floor: both call `get_charging_manager().get_pricing_info()` and get
real prices there. v0.1.5 lowered the bound to `>=2.7`; the runtime capability probe, not
a version number, decides whether the guard can meter. Both verify `charged_count`
themselves, so they keep their own `Actor.charge` calls and meter with `record()` at three
sites each, including the once-per-run setup fee.

**`google-autocomplete-api`** — the first install with no surprises at all: identical
inline `Actor.charge` shape to google-images, `requirements.txt` Dockerfile, done in one
pass. $1.00 buys about 500 suggestions a month.

**`yandex-reverse-image-search`** — expensive per run. One search returned 146 results,
$1.83 of metered spend, so $2.50 is about **one run per free user per month**. Worth a
second look at that number.

**`google-hotels-search-scraper`** — twelfth install, two firsts: no pyproject or
lockfile anywhere (requirements.txt is the source of truth, so the httpx chain is
pinned there by hand), and four run modes (search, autocomplete, photos, reviews) that
all funnel through one `_charge` helper — so the guard is routed through that single
seam with a module-level handle instead of patching ten call sites.

**`apple-app-store-reviews-api`** — the first Actor living on version **0.1** rather than
0.0 (the enable script's versions[0] convention still held). Verifies `charged_count` on
both charges, so it keeps its own `Actor.charge` calls and meters with `record()`; the
allowance stop reuses the existing `budget_exhausted` short-circuit.

**`Google-AI-Overview-API`** — news-lite shape (`_charge` returned `None`, never stopped)
plus a multi-count charge (`retrievals_used`). Routed through the guard via a
module-level handle; a row already billed is still pushed before the stop takes effect.

**`google-events-api`** — verifies `charged_count` on all three of its events, so it
meters with `record()`. Its `requirements.txt` is a `uv export` of the repo-root
pyproject — regenerate it, don't hand-edit. GitHub webhook is null (one of the known
11), so a push alone deploys nothing; the enable script's rebuild is the deploy.

**`google-scholar-api`** — the first Actor that already had its own free-tier policy
(a per-*run* upstream-call budget in `tier_policy.py`). The guard layers the
per-*month* allowance on top; its exhausted check mirrors the existing budget break at
the `__charge__` marker, so items a free user was already billed for still get stored.

**`google-local-api`** — `page_processed` was fire-and-forget (`Actor.charge` result
ignored), which makes `guard.charge()` a straight swap at that site; `setup` verifies
`charged_count` and meters with `record()`. Second Actor on version 0.1.

## The pre-charge family (Baidu, google-shopping, us-congress)

These three share a shape the first seventeen never had: they charge **setup plus the
entire expected batch up front**, before any upstream call, then do the work. There is
no per-item charge site to hook, and more importantly no useful place to stop — by the
time the guard could act, the user has already paid for the whole run.

So enforcement moves to the door. `guard.blocked` at `start()` turns away a free user
who is already over the allowance, before a single cent is charged; the pre-charges are
metered with `record()` and the return value is **deliberately ignored**, so a run that
gets past the door always finishes and delivers what it billed for. The allowance stop
lands on that user's *next* run.

The tradeoff is a slightly larger overshoot bound: one full run rather than one charge.
Worst case is small — Baidu $0.012 + 3 x $0.02, congress $0.001 + N x $0.0019 — and
this is the honest direction to err, since the alternative bills a user for pages they
never receive.

Per-Actor notes: **shopping** only meters `setup` when `charged_count == 1` (it
deliberately continues unbilled in dev mode) and meters the pages actually billed
rather than the number requested. **congress** keeps its source in
`ApifyCongressFinancialData/`, not a directory matching the repo name, and is on the
null-webhook list.

**Gotcha found here:** `chargedEventCounts` on the run object returned by
`POST /runs?waitForFinish=` can still read all zeros for a few seconds after the run
succeeds. Re-fetch `GET /v2/actor-runs/<id>` (or read the log's total) before
concluding that nothing was charged.

## `jazzhr-jobs-api` — a cap that was installed and doing nothing

The most useful failure so far, because everything looked right. The repo had a complete,
correct integration (guard threaded through `_run_jobs_mode`, three per-mode charge
events, `exhausted` checked before the terminal status, `close()` in `finally`), and the
Actor had `FREE_MAX=1.00` and `SUPABASE_URL` set. **`SUPABASE_KEY` was never added.**

With no key the guard takes the permissive path by design: one "tracking unavailable"
line, then it goes inert. Meanwhile the *paying* path still prints "Paid Apify account
detected" — because that check runs before the Supabase check — so every owner run
looked perfectly healthy, and `health_check.py` reported "No problems found" every hour
for as long as it sat there.

Two fixes came out of it:

- **`health_check.py` now fails on a missing `SUPABASE_URL`/`SUPABASE_KEY`.** It had only
  ever checked for a secret `FREE_MAX` and leftover test flags; `list_installs.py` caught
  the missing vars but is not what the monitoring loop runs. A cap with no database
  behind it is now a reported problem, not a silent one.
- **Verify the free path, not just the paid one.** The paid line proves the library is in
  the image; only a forced-free run with a ledger row proves the cap actually meters.

Whenever code arrives from somewhere other than the enable script — a parallel session, a
hand edit — assume the config half is missing until the ledger says otherwise.

## Resolved 2026-08-18: per-result now meters

For its first week installed, `yandex-...-per-result` charged `setup` and
`page_processed` while its pricing config defined neither — the platform dropped the
charges as unknown events and `per_event_prices` couldn't price them, so the cap
protected nothing. A pricing migration scheduled 2026-08-03 went live
2026-08-17 19:33 UTC and defined both events (tiered; $0.08/$0.10 at FREE). Within a
day the guard metered its first two free users and capped one at $2.56. Lesson: a
charge event only works if the *live* pricing config defines it — check
`per_event_prices` at install time, and remember a fix may already be sitting in a
scheduled `pricingInfos` entry rather than needing a new one.

Note: the same migration also kept `apify-default-dataset-item` at $0.015, so free
*and* paying users now pay per result row on top of per page — unlike its twin
`Scrape-Yandex` ($0.00001). Flagged to John; an instant decrease can fix it whenever
he chooses.

## Monitoring

```bash
python3 scripts/health_check.py --hours 3
```

Checks every capped Actor for a secret `FREE_MAX`, a test flag left on, guard warnings in
recent runs, failed runs, and Actors where the guard never spoke. Exits non-zero when
something needs attention, so it can drive a cron.

## Choosing the next Actors

Priority is the loss leaders and anything cheap enough to be worth bulk-running for
free. Before starting, check the two things that differ per Actor:

```bash
grep -E "COPY (requirements.txt|pyproject.toml)" Dockerfile   # which file to update
grep -n "_charge\|Actor.charge" src/main.py                   # the charge seam
```

Then follow [INSTALL.md](INSTALL.md).
