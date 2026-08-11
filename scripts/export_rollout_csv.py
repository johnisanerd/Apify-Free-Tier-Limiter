#!/usr/bin/env python3
"""Export the free-tier rollout to a CSV that tracks every migrated Actor.

Which Actors are migrated is read live from Apify (an Actor counts as migrated
when it has a FREE_MAX variable), so the CSV cannot drift from reality the way a
hand-kept list does. Per-Actor integration facts that no API knows - the repo,
the charge event, which file its Dockerfile installs from - come from INTEGRATIONS
below and want one new entry per install.

Usage-this-month columns need aggregate reads that the anon key deliberately
cannot do, so they are optional: pass a JSON file produced by the query in
ROLLOUT.md, or leave them blank.

    python3 scripts/export_rollout_csv.py
    python3 scripts/export_rollout_csv.py --usage-json /tmp/usage.json
    python3 scripts/export_rollout_csv.py --out ~/somewhere/else.csv

Reads the Apify token from APIFY_TOKEN, or from ~/.apify/auth.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.apify.com/v2"
DEFAULT_OUT = Path.home() / "Desktop" / "Apify" / "free-tier-limiter-rollout.csv"

# One entry per migrated Actor. Everything here is a fact about the integration
# that the Apify API cannot tell us.
INTEGRATIONS = {
    "WzsyD0afch5fKHGn5": {
        "github_repo": "johnisanerd/ApifyApifyScraper",
        "local_path": "~/Github/ApifyApifyScraper/ApifyApifyScraper",
        "installed_utc": "2026-08-10",
        "charge_event": "actor_returned",
        "charge_granularity": "per batch",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Pilot. Standard fleet _charge helper; batch charging means overshoot can be a whole batch.",
    },
    "bvAQMqCbp6wE53JzK": {
        "github_repo": "johnisanerd/ApifyGoogleImages",
        "local_path": "~/Github/ApifyGoogleImages/ApifyGoogleImages",
        "installed_utc": "2026-08-11",
        "charge_event": "image_scraped",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Dockerfile installs from requirements.txt, so uv lock alone was not enough; had to regenerate via uv export.",
    },
    "zPumutvB61fpEsglh": {
        "github_repo": "johnisanerd/ApifyYoutubeTranscripts",
        "local_path": "~/Github/ApifyYoutubeTranscripts/ApifyYoutubeTranscript",
        "installed_utc": "2026-08-11",
        "charge_event": "videoprocessed",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Fully parallel via asyncio.gather; stopping needs an Event checked after the semaphore. Versions 0.0 and 0.5 both tagged latest; 0.0 builds.",
    },
    "WQbrHYgrJV5fP6b09": {
        "github_repo": "johnisanerd/ApifyGoogleMaps",
        "local_path": "~/Github/ApifyGoogleMaps/ApifyGoogleMaps",
        "installed_utc": "2026-08-11",
        "charge_event": "place",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Inline Actor.charge, same shape as google-images.",
    },
    "Sl7mQJeH9MvLhgGYy": {
        "github_repo": "johnisanerd/ApifyGoogleNewsLite",
        "local_path": "~/Github/ApifyGoogleNewsLite/ApifyGoogleNewsLite",
        "installed_utc": "2026-08-11",
        "charge_event": "article_processed",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Its _charge returned None and never stopped the run; the cap does stop, and a flag carries that out of the nested term loop.",
    },
    "YrCMNywfEbYqWpgdF": {
        "github_repo": "johnisanerd/ApifyGoogleShoppingLite",
        "local_path": "~/Github/ApifyGoogleShoppingLite/ApifyGoogleShoppingLite",
        "installed_utc": "2026-08-11",
        "charge_event": "product",
        "charge_granularity": "per batch",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Charges before storing and slices to the billed count, so it keeps its own _charge and meters with guard.record(). First user of record().",
    },
    "ChRMxpDtEqlJHZDga": {
        "github_repo": "johnisanerd/ApifyGoogleScholarLite",
        "local_path": "~/Github/ApifyGoogleScholarLite/ApifyGoogleScholarLite",
        "installed_utc": "2026-08-11",
        "charge_event": "paper",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Inline Actor.charge. maxResultsPerSearch has a schema minimum of 10.",
    },
    "y7gc70pJD81ubH2I9": {
        "github_repo": "johnisanerd/ApifyYandex",
        "local_path": "~/Github/ApifyYandex/ApifyYandex",
        "installed_utc": "2026-08-11",
        "charge_event": "setup + page_processed",
        "charge_granularity": "per page",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Runs on apify SDK 2.7.3. Verifies charged_count itself, so it keeps its own Actor.charge calls and meters with guard.record() at three sites; the once-per-run setup fee is metered too.",
    },
    "FdyxaCtHdVcA1FBDm": {
        "github_repo": "johnisanerd/ApifyYandexReverseImage",
        "local_path": "~/Github/ApifyYandexReverseImage/ApifyYandexReverseImage",
        "installed_utc": "2026-08-11",
        "charge_event": "result_returned",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Expensive per run: one search returned 146 results = $1.83, so $2.50 is roughly a single run per month.",
    },
    "enUmNny2eNO4pE269": {
        "github_repo": "johnisanerd/ApifyYandexPayPerResult",
        "local_path": "~/Github/ApifyYandexPayPerResult/ApifyYandex",
        "installed_utc": "2026-08-11",
        "charge_event": "setup + page_processed",
        "charge_granularity": "per page",
        "dockerfile_installs_from": "uv.lock",
        "notes": "NOT METERING: the Actor charges setup and page_processed but its pricing config defines neither, so the platform drops those charges and the guard cannot price them. Fix the pricing config and this starts working.",
    },
}

COLUMNS = [
    "actor", "actor_id", "actor_url", "github_repo", "free_max_usd", "config_status",
    "library_version", "charge_event", "charge_granularity", "dockerfile_installs_from",
    "installed_utc", "latest_build", "free_users_this_month", "metered_usd_this_month",
    "capped_out_this_month", "charges_this_month", "notes",
]


def token() -> str:
    if os.getenv("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    try:
        return json.load(open(os.path.expanduser("~/.apify/auth.json")))["token"]
    except (OSError, KeyError):
        sys.exit("No Apify token. Set APIFY_TOKEN or run `apify login`.")


def call(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {token()}"})
    try:
        return json.load(urllib.request.urlopen(req))["data"]
    except urllib.error.HTTPError as exc:
        sys.exit(f"API error {exc.code} on {path}")


def pinned_version(local_path: str) -> str:
    """Which library release the Actor pins, read from its pyproject."""
    path = Path(os.path.expanduser(local_path)) / "pyproject.toml"
    if not path.exists():
        return ""
    match = re.search(r"Apify-Free-Tier-Limiter/archive/refs/tags/(v[\d.]+)\.tar\.gz", path.read_text())
    return match.group(1) if match else ""


def config_status(env: dict) -> str:
    problems = [
        *(["FREE_MAX is secret"] if env["FREE_MAX"].get("isSecret") else []),
        *(["SUPABASE_URL missing"] if "SUPABASE_URL" not in env else []),
        *(["SUPABASE_KEY missing"] if "SUPABASE_KEY" not in env else []),
        *([f"{n} still set" for n in ("FREE_TIER_FORCE", "FREE_TIER_DEBUG") if n in env]),
    ]
    return "OK" if not problems else "; ".join(problems)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--usage-json", type=Path, help="Usage aggregates keyed by actor_id")
    args = ap.parse_args()

    usage = {}
    if args.usage_json:
        raw = json.loads(args.usage_json.read_text())
        usage = {r["actor_id"]: r for r in (raw if isinstance(raw, list) else raw.get("usage", []))}

    actors = call("/acts?limit=1000&desc=1")["items"]
    print(f"Scanning {len(actors)} Actors...", file=sys.stderr)

    rows = []
    for actor in actors:
        detail = call(f"/acts/{actor['id']}")
        for version in detail.get("versions", []):
            env = {e["name"]: e for e in (version.get("envVars") or [])}
            if "FREE_MAX" not in env:
                continue
            meta = INTEGRATIONS.get(actor["id"], {})
            use = usage.get(actor["id"], {})
            builds = call(f"/acts/{actor['id']}/builds?desc=1&limit=1")["items"]
            rows.append({
                "actor": f"{actor['username']}/{actor['name']}",
                "actor_id": actor["id"],
                "actor_url": f"https://apify.com/{actor['username']}/{actor['name']}",
                "github_repo": meta.get("github_repo", ""),
                "free_max_usd": env["FREE_MAX"].get("value", ""),
                "config_status": config_status(env),
                "library_version": pinned_version(meta["local_path"]) if meta.get("local_path") else "",
                "charge_event": meta.get("charge_event", ""),
                "charge_granularity": meta.get("charge_granularity", ""),
                "dockerfile_installs_from": meta.get("dockerfile_installs_from", ""),
                "installed_utc": meta.get("installed_utc", ""),
                "latest_build": builds[0].get("buildNumber", "") if builds else "",
                "free_users_this_month": use.get("free_users", ""),
                "metered_usd_this_month": use.get("total_metered_usd", ""),
                "capped_out_this_month": use.get("capped_out", ""),
                "charges_this_month": use.get("charges", ""),
                "notes": meta.get("notes", ""),
            })

    rows.sort(key=lambda r: (r["installed_utc"], r["actor"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Wrote {len(rows)} Actor(s) to {args.out}   ({stamp})")
    missing = [r["actor"] for r in rows if not r["github_repo"]]
    if missing:
        print(f"No INTEGRATIONS entry yet for: {', '.join(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
