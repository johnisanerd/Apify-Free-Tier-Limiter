#!/usr/bin/env python3
"""Health check across every Actor that has the free-tier cap enabled.

Answers the question "is the cap misbehaving anywhere?" in one command. Three
things it looks for, in order of how much they matter:

1. Config that looks fine but is not - a secret FREE_MAX (redacted out of the
   user-facing message) or a test flag left on (FREE_TIER_FORCE meters paying
   customers).
2. Guard warnings in recent runs: tracking unavailable, an event with no
   readable price, the circuit breaker, or an unhandled traceback.
3. Whether the guard is silent on an opted-in Actor, which means it is not
   running at all.

Owner runs are the only ones the API exposes, so external usage is judged from
the ledger instead. Pass --supabase-url/--supabase-key (service role) to include
ledger stats; without them the run-log checks still work.

    python3 scripts/health_check.py
    python3 scripts/health_check.py --hours 3

Exit code 1 if anything needs attention, so it can drive a cron or a loop.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.apify.com/v2"

# Guard messages that mean something is wrong, and what each one implies.
WARNING_SIGNS = {
    "Free-tier usage tracking unavailable": "Supabase unreachable; runs continued untracked",
    "No readable price": "event charged that the pricing config does not define",
    "no longer counting this run": "circuit breaker tripped after repeated flush failures",
    "ModuleNotFoundError": "the library is not in the image",
    "Traceback": "unhandled exception",
}
GUARD_ALIVE = ("Paid Apify account detected", "Free usage this month",
               "full free monthly allowance", "Free-tier usage tracking is configured")


def token() -> str:
    if os.getenv("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    try:
        return json.load(open(os.path.expanduser("~/.apify/auth.json")))["token"]
    except (OSError, KeyError):
        pass
    # The Apify CLI keeps its token in the OS keychain when auth.json says
    # secretsBackend: keyring, so fall back to the fleet-tooling .env.
    try:
        for line in open(os.path.expanduser("~/Github/ApifyUpdate/.env")):
            if line.startswith("APIFY_TOKEN=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    sys.exit("No Apify token. Set APIFY_TOKEN or run `apify login`.")


def call(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {token()}"})
    try:
        return json.load(urllib.request.urlopen(req))["data"]
    except urllib.error.HTTPError as exc:
        sys.exit(f"API error {exc.code} on {path}")


def fetch_log(run_id: str) -> str:
    req = urllib.request.Request(f"{API}/logs/{run_id}", headers={"Authorization": f"Bearer {token()}"})
    try:
        return urllib.request.urlopen(req).read().decode()
    except Exception:  # noqa: BLE001 - a missing log is not a health problem
        return ""


def ledger(url: str, key: str) -> list[dict]:
    """Current-month totals per Actor. Needs a key that can read the table."""
    req = urllib.request.Request(
        f"{url}/rest/v1/free_tier_usage?select=actor_id,user_id,spent_usd,charge_count"
        f"&period=eq.{datetime.now(timezone.utc):%Y-%m}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        return json.load(urllib.request.urlopen(req))
    except Exception as exc:  # noqa: BLE001
        print(f"  (ledger unavailable: {type(exc).__name__})", file=sys.stderr)
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3, help="How far back to read runs")
    ap.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    ap.add_argument("--supabase-key", default=os.getenv("SUPABASE_SERVICE_KEY"))
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    problems: list[str] = []

    capped = []
    for actor in call("/acts?limit=1000&desc=1")["items"]:
        for version in call(f"/acts/{actor['id']}").get("versions", []):
            env = {e["name"]: e for e in (version.get("envVars") or [])}
            if "FREE_MAX" in env:
                capped.append((f"{actor['username']}/{actor['name']}", actor["id"], env))
                break

    print(f"Free-tier health check - {stamp}")
    print(f"{len(capped)} Actor(s) with the cap enabled, runs in the last {args.hours:g}h\n")

    for name, actor_id, env in capped:
        notes = []
        if env["FREE_MAX"].get("isSecret"):
            problems.append(f"{name}: FREE_MAX is secret, so the user-facing message shows $*********")
        for flag in ("FREE_TIER_FORCE", "FREE_TIER_DEBUG"):
            if flag in env:
                problems.append(f"{name}: {flag} still set"
                                + (" - PAYING CUSTOMERS ARE BEING METERED" if flag == "FREE_TIER_FORCE" else ""))

        runs = [r for r in call(f"/acts/{actor_id}/runs?desc=1&limit=25")["items"]
                if (r.get("startedAt") or "") >= since]
        failed = [r for r in runs if r["status"] not in ("SUCCEEDED", "RUNNING", "READY")]
        seen_guard = False
        for run in runs:
            log = fetch_log(run["id"])
            if any(sign in log for sign in GUARD_ALIVE):
                seen_guard = True
            for sign, meaning in WARNING_SIGNS.items():
                if sign in log:
                    problems.append(f"{name}: {meaning} (run {run['id']})")
        if failed:
            problems.append(f"{name}: {len(failed)} run(s) not successful: "
                            + ", ".join(f"{r['id']}={r['status']}" for r in failed[:3]))
        if runs and not seen_guard:
            problems.append(f"{name}: {len(runs)} run(s) but the guard never spoke - is it installed?")

        state = f"{len(runs)} run(s)" if runs else "no owner runs"
        print(f"  {name:52} ${env['FREE_MAX'].get('value','?'):>5}  {state}"
              + ("  " + "; ".join(notes) if notes else ""))

    if args.supabase_url and args.supabase_key:
        rows = ledger(args.supabase_url, args.supabase_key)
        if rows:
            by_actor: dict[str, list[dict]] = {}
            for row in rows:
                by_actor.setdefault(row["actor_id"], []).append(row)
            print("\nLedger, current month:")
            for actor_id, rs in sorted(by_actor.items(), key=lambda kv: -sum(float(r["spent_usd"]) for r in kv[1])):
                total = sum(float(r["spent_usd"]) for r in rs)
                print(f"  {actor_id}  users={len(rs):3}  metered=${total:8.4f}")

    print()
    if problems:
        print(f"{len(problems)} thing(s) need attention:")
        for p in problems:
            print(f"  ! {p}")
        sys.exit(1)
    print("No problems found.")


if __name__ == "__main__":
    main()
