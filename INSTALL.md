# Adding the free-tier cap to an Actor

Five steps, about ten minutes. Do the **step 0 pre-flight first** — it takes seconds and
catches the one class of break the guard can cause (a Supabase env-var collision). Step 4
is the one that silently wastes an afternoon if you skip it.

> **Your Supabase values.** The ledger URL and key are not in this public repo. Get them
> from an Actor that already has the cap (Console → Settings → Environment variables), or
> from the Supabase project `apify-free-tier`. Referred to below as `<SUPABASE_URL>` and
> `<SUPABASE_KEY>`.

---

## 0. Pre-flight: does the Actor use `SUPABASE_URL` / `SUPABASE_KEY` itself?

The guard reaches its ledger through the **`SUPABASE_URL`** and **`SUPABASE_KEY`** env vars
(hardcoded in `guard.py` / `db.py`). If the Actor's *own* code reads either of those two names
for a *different* database, provisioning the ledger creds (step 4) overwrites the Actor's data
connection and breaks it — silently, because it only fails on-platform after a rebuild.

This bit `SECInvestmentAdvisorContacts`: its data layer read `SUPABASE_URL`/`SUPABASE_KEY` for
its own SEC-advisors DB, so the daily run went from SUCCEEDED to `Failed to connect. Check
configuration.` the moment the capped build shipped. Every run before it had passed — a clean
"the cap did this" signal.

Grep the Actor's source **before touching anything**:

```bash
grep -rnE "SUPABASE_URL|SUPABASE_KEY|create_client|create_engine|psycopg|DATABASE_URL" <actor>/src/
```

- **No hits, or only comments** → no collision. Continue to step 1. (This is most Actors — 83 of
  87 in the fleet audit.)
- **Hits only on `SUPABASE_USER` / `SUPABASE_PASSWORD` / `SUPABASE_HOST` / `SUPABASE_PORT` /
  `SUPABASE_DBNAME`** (a direct Postgres DSN, usually feeding `create_engine`) → **safe.** The
  collision is *only* the two names `SUPABASE_URL` and `SUPABASE_KEY`, not the whole `SUPABASE_`
  namespace — those DSN vars coexist with the guard's vars. Continue.
- **The Actor reads `SUPABASE_URL` or `SUPABASE_KEY` for its own database** → **STOP. Do not set
  the ledger vars yet.** Rename the *Actor's* data-layer vars first (the guard's names are fixed
  across 80+ Actors, so the Actor is the side that moves):

  1. In the Actor's DB code, read a prefixed name with a local-dev fallback:
     `os.getenv("XXX_SUPABASE_URL") or os.getenv("SUPABASE_URL")` (and `..._KEY`), where `XXX` is
     the Actor (e.g. `SEC_`).
  2. Set the prefixed vars (`XXX_SUPABASE_URL` / `XXX_SUPABASE_KEY`) on the Actor from its own
     project's creds; leave `SUPABASE_URL` / `SUPABASE_KEY` for the guard.
  3. Then do steps 1–6. Verify **both** afterward: the Actor's own data query succeeds, **and** a
     forced-free run logs `Free usage this month …` (a wrong guard key shows as
     `tracking unavailable (HTTP 401)`).

Skipping this check is a latent break: it surfaces only when the Actor is next rebuilt or the
pre-cap builds are pruned.

---

## 1. Add the dependency

In the Actor's `pyproject.toml`:

```toml
dependencies = [
    "apify>=3.4,<4",
    "apify-free-tier",        # <- add
    ...
]

[tool.uv.sources]
apify-free-tier = { url = "https://github.com/johnisanerd/Apify-Free-Tier-Limiter/archive/refs/tags/v0.1.7.tar.gz" }
```

Then re-lock:

```bash
uv lock
```

### Check which file your Dockerfile installs from

**This is where an install silently does nothing.** The fleet has two Dockerfile styles,
and `pyproject.toml` alone reaches neither of them.

```bash
grep -E "COPY (requirements.txt|pyproject.toml)" Dockerfile
```

- **`COPY requirements.txt`** (older Actors, plain `pip install -r requirements.txt`) —
  `uv lock` is not enough. Regenerate the snapshot the image actually installs:

  ```bash
  uv export --no-hashes --format requirements-txt > requirements.txt
  ```

  Check the diff: it should add the `apify-free-tier @ https://...` line and change no
  existing pin.

- **`COPY pyproject.toml uv.lock`** (newer Actors, `uv export | uv pip install`) —
  `uv lock` is all you need.

Get this wrong and the build still succeeds, reusing a cached layer without the library,
and the Actor logs nothing at all at runtime.

## 2. Wire it into `main.py`

Import it next to the Apify import:

```python
from apify_free_tier import FreeTierGuard  # noqa: E402
```

Start it after input validation, before any scraping:

```python
guard = await FreeTierGuard.start()
if guard.blocked:
    return          # already logged and set the run's terminal status
```

Replace the local `_charge(...)` call. Same signature, same "True means stop" contract:

```python
# before
if Actor.is_at_home() and await _charge("item_returned", len(rows)):
    stats["limit_reached"] = True

# after
if await guard.charge("item_returned", len(rows)):
    stats["limit_reached"] = True
```

Settle the ledger on the way out, including on early exits:

```python
try:
    ...                       # the scraping / charging loop
finally:
    await guard.close()
```

Finally, stop the Actor's own sign-off from overwriting the guard's explanation:

```python
if guard.exhausted:
    Actor.log.info(f"Returned {count} rows before the free allowance ran out.")
elif stats["limit_reached"]:
    ...                       # existing platform charge-limit branch
else:
    await Actor.set_status_message(f"Done. Returned {count} rows.", is_terminal=True)
```

Leave the old `_charge` helper defined; nothing else has to change.

## 3. Commit and push

```bash
git add -A && git commit -m "Add free-tier usage cap for free users" && git push
```

If the Actor auto-builds from GitHub, this triggers a build. Confirm it succeeded before
moving on.

## 4. Set the environment variables, then REBUILD

Apify captures env vars **into the build image**. A variable added after the last build
does not exist as far as the running Actor is concerned, and nothing warns you — the
guard just sits there inert.

The script does both, in the right order:

```bash
python3 scripts/enable_free_tier.py <ACTOR_ID> --free-max 0.05 \
    --supabase-url <SUPABASE_URL> --supabase-key <SUPABASE_KEY>
```

Or by hand in Console → Settings → Environment variables:

| Variable | Value | Secret? |
| --- | --- | --- |
| `SUPABASE_URL` | `<SUPABASE_URL>` | no |
| `SUPABASE_KEY` | `<SUPABASE_KEY>` | **yes** |
| `FREE_MAX` | dollars per free user per month, e.g. `0.05` | **no** — see below |

`FREE_MAX` must **not** be secret. Apify redacts secret values in logs, which turns the
message the user reads into `allowance ($*********)`.

Then **rebuild** (Console → Builds → Build, or push any commit).

### Choosing FREE_MAX

Take the Actor's real per-event price and decide how many free results are a fair
sample. The pilot bills `$0.0000121` per row, and `$0.05` buys about 4,100 rows a month.
For an Actor at `$0.015` a page, `$0.05` is only three pages — probably too tight to
show the Actor off, so go higher.

Remember the tier-resolved price is usually **above** the list price in `actor.json`.
Run once with `FREE_TIER_DEBUG=1` to see the real numbers.

## 5. Verify

Run the Actor once. On your own (paying) account you should see:

```
INFO  Paid Apify account detected - no free-tier limit applies to this run.
```

That line is the proof it is installed. If it is missing, the guard is not running —
almost always a skipped rebuild (step 4) or a missing `uv lock` (step 1).

To exercise the free path from your paid account, add `FREE_TIER_FORCE=1`, rebuild, and
run. You should see the disclosure line, and a row should appear in the ledger:

```sql
select * from free_tier_usage where actor_id = '<ACTOR_ID>';
```

**Remove `FREE_TIER_FORCE` and rebuild when you are done**, or every paying customer
gets metered and capped.

---

## 6. Purge the pre-cap builds — the install is not finished without this

Environment variables are baked into the build image at build time, so **every build
made before step 4 has no `FREE_MAX` in it**. The guard inside those images finds no
allowance, goes dormant, and says nothing — even though the library is right there.

That matters because Apify lets whoever starts a run pin a build by number
(`?build=0.0.52`), so each pre-cap build is a working bypass of the cap you just
installed. Proven on `linkedin-company-api`: pinning the build from hours before its
variables were set ran fine, charged normally, and logged zero guard lines. Apify only
expires an untagged build after 90 days *unused*, and a run resets that clock, so a
build somebody is pinning never expires on its own.

```bash
python3 scripts/prune_stale_builds.py --actor <ACTOR_ID>            # preview
python3 scripts/prune_stale_builds.py --actor <ACTOR_ID> --delete   # purge
```

It keeps the tagged `latest` build and everything built after the cap landed, and
deletes only the pre-cap window. This is one-time per Actor — later builds inherit the
variables, so the daily rebuilds are all capped.

---

## When it does not work

| What you see | Cause |
| --- | --- |
| No free-tier line at all | Not rebuilt after setting env vars, or the dependency never reached the image — check step 1's Dockerfile question |
| Build log says `CACHED` where the install should be | The dependency is not in the file the Dockerfile installs from (usually `requirements.txt` was not regenerated) |
| `allowance ($*********)` | `FREE_MAX` is marked secret |
| "no pay-per-event prices" | The Actor is not on PPE pricing, or the owner started the run |
| "tracking unavailable (...)" | Supabase unreachable or misconfigured; the run continues untracked on purpose |
| `tracking unavailable (HTTP 401)` specifically | `SUPABASE_KEY` does not match `SUPABASE_URL`'s project — often an Actor's own key left in the guard's var (see step 0 collision) |
| The Actor's **own** run now fails (`Failed to connect`, its DB errors) after the cap shipped | The Actor reads `SUPABASE_URL`/`SUPABASE_KEY` for its own database; the ledger creds clobbered it. This is the step 0 collision — rename the Actor's vars |
| Paying customers being capped | `FREE_TIER_FORCE` left set |

`FREE_TIER_DEBUG=1` prints every variable the guard can see plus the resolved price map,
and never prints a secret value. It answers most of the above in one run.
