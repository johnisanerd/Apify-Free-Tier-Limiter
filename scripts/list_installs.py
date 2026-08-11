#!/usr/bin/env python3
"""List every Actor that has the free-tier cap enabled.

Scans the account for Actors with a FREE_MAX variable and reports how each one
is configured, so the rollout list never has to be maintained by hand. Also
flags the two misconfigurations that make an install look fine but behave
wrongly: a secret FREE_MAX (redacted out of the user-facing message) and test
flags left switched on (FREE_TIER_FORCE meters paying customers).

Usage:
    python3 scripts/list_installs.py
    python3 scripts/list_installs.py --markdown     # table for ROLLOUT.md

Reads the Apify token from APIFY_TOKEN, or from ~/.apify/auth.json.
"""
from __future__ import annotations

import argparse
import json
import os
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", action="store_true", help="Emit a Markdown table")
    args = ap.parse_args()

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
