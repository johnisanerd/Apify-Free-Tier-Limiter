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

## Enabled (14 of 103 Actors, as of 2026-08-17)

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
| `johnvc/yandex-...-per-result` | `enUmNny2eNO4pE269` | $2.50 | v0.1.7 | **not metering** |
| `johnvc/google-autocomplete-api` | `VVMGjb2KwyOPsXcwU` | $1.00 | v0.1.7 | OK |
| `johnvc/google-hotels-search-scraper` | `ahpk7S3a62kOzKdE9` | $1.00 | v0.1.7 | OK |
| `johnvc/apple-app-store-reviews-api` | `k3dKElhh0XK52g619` | $1.00 | v0.1.7 | OK |
| `johnvc/Google-AI-Overview-API` | `XqEZodkkqvqAtiSkV` | $1.00 | v0.1.7 | OK |

Each was verified on-platform on both paths: a paying account logs the "no limit
applies" line and writes nothing, and a forced-free run writes a ledger row whose amount
matches the tier-resolved price.

## What each one taught us

Eleven installs, and no two Actors have had the same charge shape until the eleventh.
Check the shape before you start.

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
