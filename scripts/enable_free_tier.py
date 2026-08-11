#!/usr/bin/env python3
"""Turn the free-tier cap on for an Actor: set the variables, then rebuild.

The rebuild is the point. Apify captures environment variables into the build
image, so a variable set through the API or the Console does not reach a run
until the Actor is rebuilt - and nothing tells you, the guard just sits there
doing nothing. Doing both here means that cannot be forgotten.

Usage:
    python3 scripts/enable_free_tier.py <ACTOR_ID> --free-max 0.05 \
        --supabase-url https://xxxx.supabase.co --supabase-key sb_publishable_xxx

    python3 scripts/enable_free_tier.py <ACTOR_ID> --show
    python3 scripts/enable_free_tier.py <ACTOR_ID> --free-max 0.25   # retune, rebuild
    python3 scripts/enable_free_tier.py <ACTOR_ID> --disable         # remove the cap

Reads the Apify token from APIFY_TOKEN, or from ~/.apify/auth.json (apify login).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.apify.com/v2"
MANAGED = ("SUPABASE_URL", "SUPABASE_KEY", "FREE_MAX", "FREE_TIER_FORCE", "FREE_TIER_DEBUG")


def token() -> str:
    if os.getenv("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    try:
        with open(os.path.expanduser("~/.apify/auth.json")) as fh:
            return json.load(fh)["token"]
    except (OSError, KeyError):
        sys.exit("No Apify token. Set APIFY_TOKEN or run `apify login`.")


def call(path: str, body: dict | None = None, method: str = "GET") -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        raw = urllib.request.urlopen(req).read().decode()
        return json.loads(raw)["data"] if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode()[:200]}


def latest_version(actor_id: str) -> str:
    actor = call(f"/acts/{actor_id}")
    if "_error" in actor:
        sys.exit(f"Cannot read Actor {actor_id}: {actor['_body']}")
    versions = actor.get("versions") or []
    if not versions:
        sys.exit(f"Actor {actor_id} has no versions.")
    print(f"Actor: {actor['username']}/{actor['name']}")
    return versions[0]["versionNumber"]


def put_env(actor_id: str, ver: str, name: str, value: str, secret: bool) -> None:
    """Create or replace one variable. PUT 404s when it does not exist yet."""
    body = {"name": name, "value": value, "isSecret": secret}
    res = call(f"/acts/{actor_id}/versions/{ver}/env-vars/{name}", body, "PUT")
    if res.get("_error") == 404:
        res = call(f"/acts/{actor_id}/versions/{ver}/env-vars", body, "POST")
    if "_error" in res:
        sys.exit(f"Failed to set {name}: {res['_body']}")
    print(f"  {name:16} = {'<secret>' if secret else value}")


def del_env(actor_id: str, ver: str, name: str) -> None:
    if "_error" not in call(f"/acts/{actor_id}/versions/{ver}/env-vars/{name}", method="DELETE"):
        print(f"  removed {name}")


def show(actor_id: str, ver: str) -> None:
    for var in call(f"/acts/{actor_id}/versions/{ver}").get("envVars") or []:
        if var["name"] in MANAGED:
            shown = "<secret>" if var.get("isSecret") else var.get("value")
            print(f"  {var['name']:16} = {shown}   secret={bool(var.get('isSecret'))}")


def rebuild(actor_id: str, ver: str, wait: bool) -> None:
    build = call(f"/acts/{actor_id}/builds?version={ver}&tag=latest&useCache=true", {}, "POST")
    if "_error" in build:
        sys.exit(f"Build failed to start: {build['_body']}")
    number = build.get("buildNumber")
    print(f"\nRebuilding ({number}) - variables do not take effect until this finishes.")
    if not wait:
        return
    for _ in range(120):
        status = call(f"/actor-builds/{build['id']}").get("status")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            print(f"Build {number}: {status}")
            if status != "SUCCEEDED":
                sys.exit(1)
            return
        time.sleep(5)
    print("Still building; check the Console.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("actor_id")
    ap.add_argument("--free-max", help="Dollars per free user per calendar month, e.g. 0.05")
    ap.add_argument("--supabase-url")
    ap.add_argument("--supabase-key")
    ap.add_argument("--force", action="store_true",
                    help="Set FREE_TIER_FORCE=1 to test the free path from a paid account")
    ap.add_argument("--debug", action="store_true", help="Set FREE_TIER_DEBUG=1")
    ap.add_argument("--clear-test-flags", action="store_true",
                    help="Remove FREE_TIER_FORCE and FREE_TIER_DEBUG")
    ap.add_argument("--disable", action="store_true",
                    help="Remove every variable, turning the cap off")
    ap.add_argument("--show", action="store_true", help="Print current values and exit")
    ap.add_argument("--no-wait", action="store_true", help="Do not wait for the build")
    args = ap.parse_args()

    ver = latest_version(args.actor_id)

    if args.show:
        show(args.actor_id, ver)
        return

    if args.disable:
        for name in MANAGED:
            del_env(args.actor_id, ver, name)
        rebuild(args.actor_id, ver, not args.no_wait)
        return

    print("Setting variables:")
    if args.supabase_url:
        put_env(args.actor_id, ver, "SUPABASE_URL", args.supabase_url, False)
    if args.supabase_key:
        put_env(args.actor_id, ver, "SUPABASE_KEY", args.supabase_key, True)
    if args.free_max:
        # Never secret: Apify redacts secret values in logs, and this number is
        # quoted in the message the user reads.
        put_env(args.actor_id, ver, "FREE_MAX", args.free_max, False)
    if args.force:
        put_env(args.actor_id, ver, "FREE_TIER_FORCE", "1", False)
    if args.debug:
        put_env(args.actor_id, ver, "FREE_TIER_DEBUG", "1", False)
    if args.clear_test_flags:
        for name in ("FREE_TIER_FORCE", "FREE_TIER_DEBUG"):
            del_env(args.actor_id, ver, name)

    rebuild(args.actor_id, ver, not args.no_wait)

    print("\nNow run the Actor once. On a paying account you should see:")
    print('  "Paid Apify account detected - no free-tier limit applies to this run."')
    print("That line is how you know it is installed.")


if __name__ == "__main__":
    main()
