# Adding the free-tier cap to an Actor

Five steps, about ten minutes. Step 4 is the one that silently wastes an afternoon if
you skip it.

> **Your Supabase values.** The ledger URL and key are not in this public repo. Get them
> from an Actor that already has the cap (Console → Settings → Environment variables), or
> from the Supabase project `apify-free-tier`. Referred to below as `<SUPABASE_URL>` and
> `<SUPABASE_KEY>`.

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
apify-free-tier = { url = "https://github.com/johnisanerd/Apify-Free-Tier-Limiter/archive/refs/tags/v0.1.3.tar.gz" }
```

Then re-lock, or the Docker build will not see it (the image installs from `uv.lock`,
not from `pyproject.toml`):

```bash
uv lock
```

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

## When it does not work

| What you see | Cause |
| --- | --- |
| No free-tier line at all | Not rebuilt after setting env vars, or `uv lock` not re-run |
| `allowance ($*********)` | `FREE_MAX` is marked secret |
| "no pay-per-event prices" | The Actor is not on PPE pricing, or the owner started the run |
| "tracking unavailable (...)" | Supabase unreachable or misconfigured; the run continues untracked on purpose |
| Paying customers being capped | `FREE_TIER_FORCE` left set |

`FREE_TIER_DEBUG=1` prints every variable the guard can see plus the resolved price map,
and never prints a secret value. It answers most of the above in one run.
