#!/usr/bin/env python3
"""List every Actor that has the free-tier cap enabled.

Scans the account for Actors with a FREE_MAX variable and reports how each one
is configured, so the rollout list never has to be maintained by hand. Also
flags the two misconfigurations that make an install look fine but behave
wrongly: a secret FREE_MAX (redacted out of the user-facing message) and test
flags left switched on (FREE_TIER_FORCE meters paying customers).

There is a third failure this cannot see by scanning FREE_MAX alone, because such an
Actor has no FREE_MAX to find: the guard code is committed and deployed, but the
variables were never set, so the Actor runs uncapped while the repo looks done.
That has happened twice (jazzhr-jobs-api, linkedin-company-api, the latter with real
public traffic). `--find-unconfigured` catches it by matching each Actor's git repo
against its local checkout and reporting any that import the library with no FREE_MAX.

Usage:
    python3 scripts/list_installs.py
    python3 scripts/list_installs.py --markdown            # table for ROLLOUT.md
    python3 scripts/list_installs.py --find-unconfigured   # guard in code, never configured

Reads the Apify token from APIFY_TOKEN, or from ~/.apify/auth.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.apify.com/v2"


def token() -> str:
    if os.getenv("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    try:
        with open(os.path.expanduser("~/.apify/auth.json")) as fh:
            return json.load(fh)["token"]
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


def scan() -> list[dict]:
    actors = call("/acts?limit=1000&desc=1")["items"]
    print(f"Scanning {len(actors)} Actors...", file=sys.stderr)
    installs = []
    for actor in actors:
        for version in call(f"/acts/{actor['id']}").get("versions", []):
            env = {e["name"]: e for e in (version.get("envVars") or [])}
            if "FREE_MAX" not in env:
                continue
            installs.append({
                "actor": f"{actor['username']}/{actor['name']}",
                "id": actor["id"],
                "version": version["versionNumber"],
                "free_max": env["FREE_MAX"].get("value"),
                "problems": [
                    *(["FREE_MAX is secret (message will show $*********)"]
                      if env["FREE_MAX"].get("isSecret") else []),
                    *(["SUPABASE_URL missing"] if "SUPABASE_URL" not in env else []),
                    *(["SUPABASE_KEY missing"] if "SUPABASE_KEY" not in env else []),
                    *([f"{n} still set" for n in ("FREE_TIER_FORCE", "FREE_TIER_DEBUG") if n in env]),
                ],
            })
    return installs


def find_unconfigured() -> list[dict]:
    """Actors whose repo imports the library but which have no FREE_MAX set.

    The code half of the install can land without the config half - a build
    deploys the guard, nobody sets the variables, and the Actor serves free
    users uncapped while every log line looks healthy. Matches each Actor's
    gitRepoUrl to a local checkout under ~/Github and greps it.
    """
    found = []
    for actor in call("/acts?limit=1000&desc=1")["items"]:
        detail = call(f"/acts/{actor['id']}")
        versions = detail.get("versions") or []
        if not versions:
            continue
        env = {e["name"] for e in (versions[0].get("envVars") or [])}
        if "FREE_MAX" in env:
            continue
        match = re.search(r"[:/]([^/:]+)/([^/.]+)\.git", versions[0].get("gitRepoUrl") or "")
        if not match:
            continue
        path = os.path.expanduser(f"~/Github/{match.group(2)}")
        if not os.path.isdir(path):
            continue
        hit = subprocess.run(
            ["grep", "-rl", "apify_free_tier", path, "--include=*.py"],
            capture_output=True, text=True,
        ).stdout.strip()
        if hit:
            stats = detail.get("stats", {})
            found.append({
                "actor": f"{actor['username']}/{actor['name']}",
                "id": actor["id"],
                "repo": match.group(2),
                "public": detail.get("isPublic"),
                "users30": stats.get("totalUsers30Days"),
            })
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", action="store_true", help="Emit a Markdown table")
    ap.add_argument("--find-unconfigured", action="store_true",
                    help="Report Actors whose code has the guard but no FREE_MAX set")
    args = ap.parse_args()

    if args.find_unconfigured:
        rows = find_unconfigured()
        for r in rows:
            flag = "PUBLIC" if r["public"] else "private"
            print(f"{r['actor']:45} {r['id']}  {flag}  users30={r['users30']}  repo={r['repo']}")
        if rows:
            print(f"\n{len(rows)} Actor(s) import the library but have no FREE_MAX set - "
                  f"they run uncapped.", file=sys.stderr)
            sys.exit(1)
        print("No Actor has the guard in code without the variables set.", file=sys.stderr)
        return

    installs = scan()

    if args.markdown:
        print("| Actor | Actor ID | FREE_MAX | Status |")
        print("| --- | --- | --- | --- |")
        for i in installs:
            status = "OK" if not i["problems"] else "; ".join(i["problems"])
            print(f"| `{i['actor']}` | `{i['id']}` | ${i['free_max']} | {status} |")
    else:
        for i in installs:
            print(f"{i['actor']:45} v{i['version']:5} FREE_MAX=${i['free_max']}")
            for problem in i["problems"]:
                print(f"    ! {problem}")

    print(f"\n{len(installs)} Actor(s) with the cap enabled.", file=sys.stderr)
    if any(i["problems"] for i in installs):
        sys.exit(1)


if __name__ == "__main__":
    main()
