#!/usr/bin/env python3
"""Find - and optionally delete - Actor builds that predate the free-tier cap.

Why this exists
---------------
Environment variables are baked into the build image at build time. A build made
before `FREE_MAX` and the Supabase variables were set therefore carries no cap,
and the guard inside it goes dormant and says nothing. That is true even when the
library code is already in the image, which makes these builds impossible to spot
from the code alone.

Apify lets a caller pin a build when starting a run ("you can choose what build to
run by selecting a tag or number in the run options"), so a stale build is a live
bypass, not dead weight. Verified on 2026-08-30: running linkedin-company-api with
`?build=0.0.52` - the build that shipped hours before its variables were set -
succeeded, charged normally, and logged zero guard lines.

Retention does not solve it. Apify deletes builds only when they are "not tagged
and have not been used for over 90 days", and use resets that clock, so a build
somebody is actively pinning never expires.

What counts as stale
--------------------
A build is stale when all of these hold:

  * it SUCCEEDED (unfinished builds cannot be deleted anyway),
  * it carries no build tag - the tagged/default build is what the Actor serves
    and the API refuses to delete it,
  * it finished on or before the day the cap was installed, per the
    `installed_utc` field in export_rollout_csv.py's INTEGRATIONS table.

The day boundary is deliberately inclusive. Same-day builds finished *after* the
enable script are safe, but a strictly-earlier comparison misses the ones finished
*before* it - which is how the first pass at this list missed the very build that
was proven exploitable. Deleting a superseded same-day build costs nothing;
missing a live bypass does not.

Builds made after the cap was installed are NOT stale. They inherit the variables
from the Actor's configuration, so the daily README-bump rebuilds are all capped.
That is why this is a one-time cleanup per Actor rather than a recurring job.

After a library upgrade: --before-build
---------------------------------------
Moving an Actor to a newer library (v0.1.9 adds the check-before-you-buy
`affordable()` / `exhaust()`) leaves the same kind of bypass: every older build
still runs the old library, and a caller can pin one. Dates do not identify those
builds, so `--before-build 0.0.N` (one Actor at a time, via `--actor`) replaces
the date rule: an untagged SUCCEEDED build is stale when its number is below
0.0.N. The tagged build is still never touched.

Build numbers are compared as integers, part by part. As strings 0.0.100 sorts
before 0.0.11, so a string comparison would delete a newer build and keep an
older one. Every build of an older version (0.0.x against a 0.1.y cutoff) counts
as earlier, and a build number that does not parse is kept.

Usage
-----
    python3 scripts/prune_stale_builds.py                     # dry run, writes the CSV
    python3 scripts/prune_stale_builds.py --actor <id|slug>   # dry run, one Actor
    python3 scripts/prune_stale_builds.py --actor <id> --delete
    python3 scripts/prune_stale_builds.py --all-actors --delete
    python3 scripts/prune_stale_builds.py --actor <id> --before-build 0.0.120            # dry run
    python3 scripts/prune_stale_builds.py --actor <id> --before-build 0.0.120 --delete

Deleting is opt-in and scoped: `--delete` needs either `--actor` or the explicit
`--all-actors`, so a fleet-wide purge can never happen by forgetting a flag.

Reads the Apify token from APIFY_TOKEN, ~/.apify/auth.json, or the fleet .env.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from list_installs import token  # noqa: E402 - shares the token fallback chain

API = "https://api.apify.com/v2"
DEFAULT_OUT = Path.home() / "Desktop" / "Apify" / "free-tier-stale-builds.csv"
BUILD_NUMBER = re.compile(r"\d+\.\d+\.\d+")


def build_number_key(number: str) -> tuple[int, ...]:
    """'0.0.105' -> (0, 0, 105), so build numbers compare as numbers, not text."""
    return tuple(int(part) for part in number.strip().split("."))


def is_before_build(number: str | None, cutoff: str) -> bool:
    """True when build `number` is older than `cutoff`, compared as integer tuples.

    Pure, so it is unit-tested. A number that does not parse is never "before"
    anything: when in doubt, keep the build.
    """
    try:
        return build_number_key(number) < build_number_key(cutoff)
    except (AttributeError, TypeError, ValueError):
        return False


def _build_number_arg(value: str) -> str:
    """argparse type for --before-build: exactly MAJOR.MINOR.BUILD, digits only."""
    value = value.strip()
    if not BUILD_NUMBER.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"expected a build number like 0.0.120, got {value!r}"
        )
    return value


def _integrations() -> dict:
    """Install dates live in the CSV exporter's INTEGRATIONS table."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_rollout_csv.py")
    spec = importlib.util.spec_from_file_location("_exp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.INTEGRATIONS


def call(path: str, method: str = "GET", tries: int = 6):
    """One API call, retrying the throttling and transient codes.

    A fleet-wide scan makes a few hundred calls, and Apify rate-limits well
    before that, so backoff is required rather than nice to have.
    """
    for attempt in range(tries):
        req = urllib.request.Request(
            f"{API}{path}", method=method,
            headers={"Authorization": f"Bearer {token()}"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                # DELETE answers 204 with no body; a chunked GET reports
                # length None, so status is the only reliable signal here.
                if resp.status == 204:
                    return None
                body = resp.read()
                return json.loads(body).get("data") if body else None
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def capped_actors(only: str | None) -> list[dict]:
    """Every Actor with FREE_MAX set, across all of its versions.

    Checking every version matters: YoutubeTranscripts carried the cap on
    version 0.0 while 0.5 was the one tagged latest, so reading versions[0]
    alone reports the wrong answer.
    """
    actors, offset = [], 0
    while True:
        page = call(f"/acts?my=1&limit=100&offset={offset}")
        actors += page["items"]
        offset += 100
        if offset >= page["total"]:
            break

    found = []
    for actor in actors:
        if only and only not in (actor["id"], actor["name"], f"{actor['username']}/{actor['name']}"):
            continue
        detail = call(f"/acts/{actor['id']}")
        versions = detail.get("versions") or []
        if not any("FREE_MAX" in {e["name"] for e in (v.get("envVars") or [])} for v in versions):
            continue
        found.append(detail)
    return found


def select_stale(
    builds: list[dict], tagged: set, installed: str | None, before_build: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Split one Actor's builds into (stale builds, tagged build numbers kept). Pure.

    Only a SUCCEEDED, finished, untagged build can be stale. Which of those are
    is decided by exactly one rule: numbered below `before_build` when it is
    given, otherwise finished on or before the `installed` date.
    """
    stale, kept = [], []
    for build in builds:
        finished = (build.get("finishedAt") or "")[:10]
        if build.get("status") != "SUCCEEDED" or not finished:
            continue
        if build["id"] in tagged:
            kept.append(build["buildNumber"])
            continue
        if before_build is not None:
            if is_before_build(build.get("buildNumber"), before_build):
                stale.append(build)
        elif installed and finished <= installed:   # inclusive: see the module docstring
            stale.append(build)
    return stale, kept


def stale_builds(
    detail: dict, installed: str | None, before_build: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Return (stale builds, build numbers kept) for one Actor."""
    tagged = {t.get("buildId") for t in (detail.get("taggedBuilds") or {}).values()}
    builds = call(f"/acts/{detail['id']}/builds?desc=1&limit=1000")["items"]
    return select_stale(builds, tagged, installed, before_build)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actor", help="Limit to one Actor (id, name, or username/name)")
    ap.add_argument("--all-actors", action="store_true",
                    help="Required to delete across the whole fleet")
    ap.add_argument("--delete", action="store_true",
                    help="Actually delete. Needs --actor or --all-actors")
    ap.add_argument("--before-build", type=_build_number_arg, metavar="0.0.N",
                    help="Needs --actor. Select untagged builds numbered below 0.0.N "
                         "instead of those finished by the cap's install date "
                         "(after a library upgrade). Dry run unless --delete")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if args.before_build and not args.actor:
        sys.exit("--before-build needs --actor: a build number only means something "
                 "within one Actor.")
    if args.delete and not (args.actor or args.all_actors):
        sys.exit("Refusing to delete without --actor or --all-actors.")

    integrations = _integrations()
    rows, total_stale, no_date = [], 0, []

    for detail in capped_actors(args.actor):
        name = f"{detail['username']}/{detail['name']}"
        meta = integrations.get(detail["id"], {})
        installed = meta.get("installed_utc")
        if not installed and not args.before_build:
            # Without an install date there is no way to tell a pre-cap build
            # from a post-cap one, and guessing would delete live builds.
            no_date.append(name)
            continue

        stale, kept = stale_builds(detail, installed, args.before_build)
        if args.before_build:
            old_tagged = [n for n in kept if is_before_build(n, args.before_build)]
            if old_tagged:
                # Kept regardless, but it means the Actor still serves a build
                # from before the cutoff: the upgrade is not live yet.
                print(f"\n{name}: tagged build {', '.join(old_tagged)} is below "
                      f"{args.before_build} and is kept; the default build predates "
                      "the cutoff.", file=sys.stderr)
        total_stale += len(stale)
        stats = detail.get("stats", {})
        rows.append({
            "actor": name,
            "actor_id": detail["id"],
            "public": bool(detail.get("isPublic")),
            "cap_installed_utc": installed or "",
            "users_30d": stats.get("totalUsers30Days") or 0,
            "runs_30d": (stats.get("publicActorRunStats30Days") or {}).get("TOTAL") or 0,
            "keep_builds": " ".join(kept),
            "stale_count": len(stale),
            "stale_oldest": min((b["finishedAt"][:10] for b in stale), default=""),
            "stale_newest": max((b["finishedAt"][:10] for b in stale), default=""),
            "stale_build_numbers": " ".join(b["buildNumber"] for b in stale),
            "stale_build_ids": " ".join(b["id"] for b in stale),
        })

        if args.delete and stale:
            print(f"\n{name}: deleting {len(stale)} stale build(s)")
            done = failed = 0
            for build in stale:
                try:
                    call(f"/actor-builds/{build['id']}", method="DELETE")
                    done += 1
                except urllib.error.HTTPError as exc:
                    failed += 1
                    print(f"  ! {build['buildNumber']}: HTTP {exc.code} {exc.reason}")
            print(f"  deleted {done}, failed {failed}")

    rows.sort(key=lambda r: (-r["stale_count"], -r["users_30d"]))

    print(f"\n{'actor':44} {'pub':5} {'users30':>7} {'installed':>11} {'STALE':>6} {'oldest':>11}")
    for r in rows:
        print(f"{r['actor'][:44]:44} {str(r['public'])[:5]:5} {r['users_30d']:>7} "
              f"{r['cap_installed_utc']:>11} {r['stale_count']:>6} {r['stale_oldest']:>11}")

    if no_date:
        print(f"\nNo install date on record, skipped ({len(no_date)}): {', '.join(no_date)}",
              file=sys.stderr)
    if args.actor and not rows and not no_date:
        print(f"\nNo Actor with FREE_MAX matched --actor {args.actor}.", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["actor"])
        writer.writeheader()
        writer.writerows(rows)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    verb = "deleted" if args.delete else "stale (dry run)"
    rule = (f"untagged, numbered below {args.before_build}" if args.before_build
            else "untagged, finished on or before the cap install date")
    print(f"\n{len(rows)} Actor(s), {total_stale} build(s) {verb}   ({stamp})")
    print(f"Stale rule: {rule}")
    print(f"Wrote {args.out}")
    if not args.delete and total_stale:
        if args.before_build:
            print("\nNothing was deleted. Re-run the same command with --delete to purge them.")
        else:
            print("\nNothing was deleted. Re-run with --actor <id> --delete to purge one Actor.")


if __name__ == "__main__":
    main()
