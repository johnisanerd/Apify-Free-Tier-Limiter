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

## Enabled (11 of 103 Actors, as of 2026-08-13)

| Actor | Actor ID | FREE_MAX | Library | Status |
| --- | --- | --- | --- | --- |
| `johnvc/store-actor-intelligence-api` | `WzsyD0afch5fKHGn5` | $1.00 | v0.1.3 | OK |
| `johnvc/google-images-api` | `bvAQMqCbp6wE53JzK` | $1.00 | v0.1.3 | OK |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | $1.00 | v0.1.3 | OK |
| `johnvc/google-maps-places-api` | `WQbrHYgrJV5fP6b09` | $1.00 | v0.1.4 | OK |
| `johnvc/google-news-lite-api` | `Sl7mQJeH9MvLhgGYy` | $1.00 | v0.1.4 | OK |
| `johnvc/google-shopping-lite-api` | `YrCMNywfEbYqWpgdF` | $1.00 | v0.1.4 | OK |
| `johnvc/google-scholar-lite-api` | `ChRMxpDtEqlJHZDga` | $1.00 | v0.1.4 | OK |
| `johnvc/Scrape-Yandex` | `y7gc70pJD81ubH2I9` | $2.50 | v0.1.5 | OK |
| `johnvc/yandex-reverse-image-search` | `FdyxaCtHdVcA1FBDm` | $2.50 | v0.1.5 | OK |
| `johnvc/yandex-...-per-result` | `enUmNny2eNO4pE269` | $2.50 | v0.1.5 | **not metering** |
| `johnvc/google-autocomplete-api` | `VVMGjb2KwyOPsXcwU` | $1.00 | v0.1.7 | OK |

Each was verified on-platform on both paths: a paying account logs the "no limit
applies" line and writes nothing, and a forced-free run writes a ledger row whose amount
matches the tier-resolved price.

## What each one taught us

Seven installs, seven different charge shapes. Check the shape before you start.

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

**`yandex-reverse-image-search`** — expensive per run. One search returned 146 results,
$1.83 of metered spend, so $2.50 is about **one run per free user per month**. Worth a
second look at that number.

## Known problem: per-result is not metering

`yandex-...-per-result` charges `setup` and `page_processed`, but its pricing config
defines neither — the platform logs `Attempting to charge for an unknown event 'setup'`
and drops the charge, and `per_event_prices` returns only
`{apify-default-dataset-item, startup}`. So those charges earn nothing **and** the guard
cannot price them. The cap is installed but protecting nothing until the pricing config
is fixed. Its twin `Scrape-Yandex` resolves the same two events correctly, so compare
their `.actor/actor.json` pricing blocks.

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
