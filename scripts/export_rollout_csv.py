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
        "installed_utc": "2026-08-31",
        "charge_event": "videoprocessed",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Fully parallel via asyncio.gather; stopping needs an Event checked after the semaphore. Versions 0.0 and 0.5 both tagged latest; 0.0 builds. CAP LOST AND RESTORED 2026-08-31: version 0.0 (which carried the cap) was removed and v0.5 became the only version, having never had the variables - the Actor ran uncapped until re-enabled on build 0.5.21. installed_utc is the RE-install date on purpose, because every v0.5 build before it lacks FREE_MAX and is prunable.",
    },
    "WQbrHYgrJV5fP6b09": {
        "github_repo": "johnisanerd/ApifyGoogleMaps",
        "local_path": "~/Github/ApifyGoogleMaps/ApifyGoogleMaps",
        "installed_utc": "2026-08-11",
        "charge_event": "place",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Inline Actor.charge, same shape as google-images. Cap halved $2.00 -> $1.00 on 2026-08-13, which put 6 existing users over 50% overnight.",
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
    "VVMGjb2KwyOPsXcwU": {
        "github_repo": "johnisanerd/ApifyGoogleAutocomplete",
        "local_path": "~/Github/ApifyGoogleAutocomplete/ApifyGoogleAutocomplete",
        "installed_utc": "2026-08-13",
        "charge_event": "suggestion_returned",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Cleanest install so far: same inline Actor.charge shape as google-images, no surprises. $1.00 buys about 500 suggestions a month.",
    },
    "ahpk7S3a62kOzKdE9": {
        "github_repo": "johnisanerd/ApifyGoogleHotels",
        "local_path": "~/Github/ApifyGoogleHotels/ApifyGoogleHotels",
        "installed_utc": "2026-08-17",
        "charge_event": "setup + page_processed + 3 per-item events",
        "charge_granularity": "per page/item",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "No pyproject/uv.lock at all - requirements.txt is the source of truth, httpx chain pinned by hand. All four modes funnel through one _charge helper, so the guard is routed via a module-level handle instead of ten call sites.",
    },
    "k3dKElhh0XK52g619": {
        "github_repo": "johnisanerd/ApifyAppStoreReviews",
        "local_path": "~/Github/ApifyAppStoreReviews/ApifyAppStoreReviews",
        "installed_utc": "2026-08-17",
        "charge_event": "setup + review",
        "charge_granularity": "per item",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Lives on version 0.1, not 0.0. Verifies charged_count on both charges, so it keeps its own Actor.charge calls and meters with guard.record(); the allowance stop reuses the existing budget_exhausted short-circuit.",
    },
    "XqEZodkkqvqAtiSkV": {
        "github_repo": "johnisanerd/ApifyGoogleAIOverview",
        "local_path": "~/Github/ApifyGoogleAIOverview/ApifyGoogleAIOverview",
        "installed_utc": "2026-08-17",
        "charge_event": "setup + overview-retrieval",
        "charge_granularity": "per retrieval (multi-count)",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "news-lite shape: _charge returned None and never stopped. Now routed through the guard via a module-level handle and the query loop stops; a row already billed is still pushed before stopping.",
    },
    "DfdUgh7nBLKe78irv": {
        "github_repo": "johnisanerd/ApifyGoogleEvents",
        "local_path": "~/Github/ApifyGoogleEvents/ApifyGoogleEvents",
        "installed_utc": "2026-08-20",
        "charge_event": "setup + page_processed (+ event_returned from 2026-09-01)",
        "charge_granularity": "per page (single-page vertical)",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Verifies charged_count on all three events, so it meters with guard.record(). requirements.txt is exported from the repo-root pyproject (uv export), not hand-pinned. Null GitHub webhook - the enable script's rebuild is what deploys.",
    },
    "m22qEjpnfxa4H1ijE": {
        "github_repo": "johnisanerd/ApifyGoogleScholar",
        "local_path": "~/Github/ApifyGoogleScholar/ApifyGoogleScholar",
        "installed_utc": "2026-08-20",
        "charge_event": "setup + query_executed",
        "charge_granularity": "per upstream call",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Already had its own per-RUN free-tier policy (tier_policy.py); the guard adds the per-MONTH layer on top. Meters with guard.record(); the exhausted check mirrors the existing per-run budget break at the __charge__ marker so items already billed still get stored.",
    },
    "pKIcPdH1zYxQBowJa": {
        "github_repo": "johnisanerd/ApifyLinkedInJobSearch",
        "local_path": "~/Github/ApifyLinkedInJobSearch/ApifyLinkedInJobSearch",
        "installed_utc": "2026-08-31",
        "charge_event": "per-row base event + a resolution surcharge event",
        "charge_granularity": "per row, surcharge stacks on the same row",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Found by the new list_installs.py --find-unconfigured sweep, not by anyone noticing: guard committed and deployed, variables never set. Still private with 5 runs, so nothing leaked. Meters with record() because it checks charged_count itself, and combines the platform limit and the allowance into one stop signal. SDK 4, v0.1.8, version 0.1. Priced and metering as of 2026-08-31 ($0.0005/job-listing, so $1.00 is ~2,000 listings). installed_utc is the 2026-08-31 RE-install, not the 2026-08-30 first install: SUPABASE_KEY was removed from the Actor afterwards and build 0.1.7 shipped without it, so everything up to 0.1.8 must be treated as uncapped and prunable.",
    },
    "UhJGmp1YJmNidr7h1": {
        "github_repo": "johnisanerd/ApifyLinkedInCompany",
        "local_path": "~/Github/ApifyLinkedInCompany/ApifyLinkedInCompany",
        "installed_utc": "2026-08-29",
        "charge_event": "company-scraped + company-searched",
        "charge_granularity": "per company; the search event fires only when resolving a name",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Code arrived pre-integrated and already deployed (build 0.0.52) but the env vars were never set, so it ran uncapped with real public traffic. Only Actor so far with a try/except import fallback to a local _NullGuard for dev; on-platform the guard's own log line is what proves the real library loaded rather than the shim. On SDK 3 yet pinned to v0.1.8, which works fine. Measured $0.00495/company, so $1.00 is about 200 companies a month.",
    },
    "P9ArUDJDSgHTmYzDp": {
        "github_repo": "johnisanerd/ApifyLinkedInPosts",
        "local_path": "~/Github/ApifyLinkedInPosts/ApifyLinkedInPosts",
        "installed_utc": "2026-09-03",
        "charge_event": "post-scraped",
        "charge_granularity": "per post",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Sibling of profile/jobs: single _charge helper feeding a nested _emit; guard routed through _charge via a module handle, started after input+token+budget checks (before the client), final status skipped when exhausted. SDK 3.4, v0.1.8.",
    },
    "IBDtFcC5lRLf0vXoH": {
        "github_repo": "johnisanerd/ApifyLinkedInProfile",
        "local_path": "~/Github/ApifyLinkedInProfile/ApifyLinkedInProfile",
        "installed_utc": "2026-09-03",
        "charge_event": "profile-scraped",
        "charge_granularity": "per profile",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Same shape as posts/jobs. Verified paid (charged profile-scraped:1, guard spoke) and free-path guard disclosure; a free-path ledger row was not captured because the upstream profile fetch is slow and TIMED-OUT on the test URLs - real free traffic (16 users/30d) will produce it. SDK 3.4, v0.1.8.",
    },
    "8gL3E4qLSkxxDyeDl": {
        "github_repo": "johnisanerd/ApifyLinkedInJobs",
        "local_path": "~/Github/ApifyLinkedInJobs/ApifyLinkedInJobs",
        "installed_utc": "2026-09-03",
        "charge_event": "job-scraped",
        "charge_granularity": "per job",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Sibling of posts/profile. Free-path metered $0.01188 for 3 jobs. SDK 3.4, v0.1.8.",
    },
    "La2BRZMUbyhY5gKNG": {
        "github_repo": "johnisanerd/ApifyLinkedInLearning",
        "local_path": "~/Github/ApifyLinkedInLearning/ApifyLinkedInLearning",
        "installed_utc": "2026-09-03",
        "charge_event": "course-found + course-detail",
        "charge_granularity": "per course row",
        "dockerfile_installs_from": "uv.lock",
        "notes": "SDK 4 (needs v0.1.8). Work lives in _run() called from main()'s try/finally; guard routed through the existing _charge (4 sites), started in _run before the fetcher, closed in main's finally. Free-path metered $0.0003 for 3 course-found.",
    },
    "K57owi8nOaCWbnGQM": {
        "github_repo": "johnisanerd/ApifyLinkedInPeople",
        "local_path": "~/Github/ApifyLinkedInPeople/ApifyLinkedInPeople",
        "installed_utc": "2026-08-29",
        "charge_event": "person-found + person-enriched",
        "charge_granularity": "per profile, enrichment stacks on the same profile",
        "dockerfile_installs_from": "uv.lock",
        "notes": "First install on SDK 4 that needed the code written (Greenhouse/Ashby arrived pre-integrated), so it is the reference for the v0.1.8 pin: Actor.charge takes count as keyword-only there. Original single-_charge-helper shape, routed via a module-level handle. Capped pre-launch while private. Measured $0.000297/profile found, so $1.00 is ~3,300 profiles a month, or ~1,100 with enrichment on.",
    },
    "H9ZkYEGh5gSvAVSXT": {
        "github_repo": "johnisanerd/ApifyAshby",
        "local_path": "~/Github/ApifyAshby/ApifyAshby",
        "installed_utc": "2026-08-26",
        "charge_event": "job-result + description add-ons + company-row + url-index-row + run-report",
        "charge_granularity": "per row, add-ons stack per row",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Greenhouse-pattern clone (same _charge_row add-on stacking), pinned to library v0.1.8 because it runs Apify SDK 4. Capped pre-launch while still private with 0 runs. DORMANT until the Actor is priced: with no PAY_PER_EVENT pricing the guard reads no prices and logs the no-prices line instead of metering. It picks prices up at runtime with no rebuild once pricing is set - re-verify with a forced-free run then.",
    },
    "X3nud8oqPjzaV92oQ": {
        "github_repo": "johnisanerd/ApifyGreenhouse",
        "local_path": "~/Github/ApifyGreenhouse/ApifyGreenhouse",
        "installed_utc": "2026-08-25",
        "charge_event": "job-result + 3 description add-ons + job-questions + company-row + url-index-row + run-report",
        "charge_granularity": "per row, add-ons stack per row",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Capped BEFORE launch - still private with zero public runs, so no free user ever ran it uncapped. Eight charge events, the most in the fleet; add-ons stack on top of the base row event via _charge_row(). Pinned to library v0.1.8 (rest of the fleet is on v0.1.7) because it runs Apify SDK 4, where Actor.charge takes count as keyword-only. Measured cost ~$0.00147/job with markdown on, so $1.00 is roughly 680 jobs a month.",
    },
    "j4OJsjSUT8rK1REX6": {
        "github_repo": "johnisanerd/ApifyNaver",
        "local_path": "~/Github/ApifyNaver/ApifyNaver",
        "installed_utc": "2026-08-25",
        "charge_event": "actor_start + result_scraped",
        "charge_granularity": "per result",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Original fleet shape - a single _charge(event, count) -> bool helper wrapping one Actor.charge, no charged_count check - so the guard routes through that helper via a module-level handle and both call sites are covered by one change. The done-summary status message is skipped when guard.exhausted so it cannot clobber the allowance explanation.",
    },
    "fKI5Ckh8aKOioEU1U": {
        "github_repo": "johnisanerd/ApifyNaverAIOverview",
        "local_path": "~/Github/ApifyNaverAIOverview/ApifyNaverAIOverview",
        "installed_utc": "2026-08-25",
        "charge_event": "ai_overview_queried",
        "charge_granularity": "per query",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Same single-helper shape; its existing limit_reached stop logic works unchanged once _charge routes through the guard. Its _finish_due_to_budget message ('raise your run budget') is wrong advice for a spent monthly allowance, so it is suppressed when guard.exhausted. Found by ranking candidates on RUNS not users: 10 free users but 1,321 runs/30d.",
    },
    "Kv1kG2WbLlEvSe4Yc": {
        "github_repo": "johnisanerd/ApifyJazzHR",
        "local_path": "~/Github/ApifyJazzHR/ApifyJazzHR",
        "installed_utc": "2026-08-24",
        "charge_event": "job-detail + company-row + url-index-row",
        "charge_granularity": "per row, one event per output mode",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Code landed ahead of the config: the repo had the full guard integration and FREE_MAX + SUPABASE_URL were set, but SUPABASE_KEY was never added, so the guard went permissive and metered nothing while looking healthy. Three output modes each bill their own event (they cost very differently); guard is threaded into _run_jobs_mode as a parameter. Per-row charging makes the overshoot bound one row per concurrent worker, the tightest shape so far. Third Actor on version 0.1.",
    },
    "hDVd9ZQQHglV5LZ1A": {
        "github_repo": "johnisanerd/ApifyBaidu",
        "local_path": "~/Github/ApifyBaidu/ApifyBaidu",
        "installed_utc": "2026-08-24",
        "charge_event": "setup + page_processed",
        "charge_granularity": "per page, pre-charged up front",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "First of the pre-charge-up-front family: setup and ALL pages are charged before any work. The cap is enforced at the door via guard.blocked; both charges are metered with record() but the run is never stopped mid-flight, because stopping would strand a run the user already paid for. Overshoot is bounded by one run.",
    },
    "U02ytMsu6ynITFJHX": {
        "github_repo": "johnisanerd/ApifyGoogleShopping",
        "local_path": "~/Github/ApifyGoogleShopping/ApifyGoogleShopping",
        "installed_utc": "2026-08-24",
        "charge_event": "setup + page_processed",
        "charge_granularity": "per page, pre-charged up front",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Pre-charge family. Only meters setup when charged_count==1, since the Actor deliberately continues unbilled in dev mode. Meters the pages actually billed (charged_count), not the number requested. Repo also carries an unused requirements.txt; the Dockerfile exports from the lock at build time.",
    },
    "xxCgm38ifv9HcLl9z": {
        "github_repo": "johnisanerd/ApifyCongressFinancialDisclosures",
        "local_path": "~/Github/ApifyCongressFinancialDisclosures/ApifyCongressFinancialData",
        "installed_utc": "2026-08-24",
        "charge_event": "setup + transaction_processed",
        "charge_granularity": "per transaction, pre-charged up front",
        "dockerfile_installs_from": "uv.lock",
        "notes": "Pre-charge family. Actor dir name (ApifyCongressFinancialData) differs from the repo name. Self-scraped via Supabase, so free-user cost is compute rather than upstream cash. Null GitHub webhook - the enable script's rebuild is the deploy.",
    },
    "bZ3PtlNaHVObbbR4O": {
        "github_repo": "johnisanerd/ApifyGoogleLocal",
        "local_path": "~/Github/ApifyGoogleLocal/ApifyGoogleLocal",
        "installed_utc": "2026-08-20",
        "charge_event": "setup + page_processed",
        "charge_granularity": "per page",
        "dockerfile_installs_from": "requirements.txt",
        "notes": "Lives on version 0.1. page_processed was fire-and-forget (result ignored), so guard.charge() is a straight swap there; setup verifies charged_count and meters with record(). serper.dev upstream (SEARCH_API_KEY).",
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
