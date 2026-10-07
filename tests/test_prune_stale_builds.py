"""scripts/prune_stale_builds.py: the --before-build rule, offline.

Deleting builds cannot be undone, so the selection is a pure function and the
comparison it rests on is pinned down here. The API is replaced by fakes.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_stale_builds.py"
_spec = importlib.util.spec_from_file_location("prune_stale_builds", SCRIPT)
prune = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune)


def _build(build_id, number, finished="2026-10-01T12:00:00.000Z", status="SUCCEEDED"):
    return {"id": build_id, "buildNumber": number, "status": status, "finishedAt": finished}


# Newest first, the way the API returns them. Cap installed 2026-08-11.
BUILDS = [
    _build("t", "0.0.130"),                                     # tagged latest
    _build("a", "0.0.125", finished="2026-10-05T00:00:00Z"),
    _build("b", "0.0.120", finished="2026-10-04T00:00:00Z"),    # the cutoff itself
    _build("c", "0.0.119", finished="2026-10-03T00:00:00Z"),
    _build("d", "0.0.99", finished="2026-08-01T00:00:00Z"),
    _build("e", "0.0.100", finished="2026-08-02T00:00:00Z"),
    _build("f", "0.0.118", status="FAILED"),
    _build("g", "0.0.117", finished=None, status="RUNNING"),
]


# ------------------------------------------------------- the comparison itself


def test_build_numbers_become_integer_tuples():
    assert prune.build_number_key("0.0.105") == (0, 0, 105)
    assert prune.build_number_key(" 0.1.7 ") == (0, 1, 7)


@pytest.mark.parametrize("number,cutoff,expected", [
    ("0.0.9", "0.0.10", True),        # as text, '0.0.9' sorts after '0.0.10'
    ("0.0.100", "0.0.11", False),     # as text, '0.0.100' sorts before '0.0.11'
    ("0.0.10", "0.0.10", False),      # the cutoff build itself is kept
    ("0.0.11", "0.0.10", False),
    ("0.0.119", "0.0.120", True),
    ("0.0.52", "0.1.0", True),        # every build of an older version is earlier
    ("0.5.3", "0.0.200", False),      # a newer version, whatever its build part
    ("1.0.0", "0.9.999", False),
])
def test_is_before_build_compares_as_integers(number, cutoff, expected):
    assert prune.is_before_build(number, cutoff) is expected


def test_text_order_would_delete_a_newer_build():
    """Why the helper exists: compared as text, 0.0.100 looks older than 0.0.11."""
    assert "0.0.100" < "0.0.11"
    assert prune.is_before_build("0.0.100", "0.0.11") is False


@pytest.mark.parametrize("number", ["latest", "", None, "0.0.x", "0..1"])
def test_an_unparseable_build_number_is_kept(number):
    assert prune.is_before_build(number, "0.0.10") is False


@pytest.mark.parametrize("value", ["0.0.120", " 0.1.7 "])
def test_before_build_accepts_a_build_number(value):
    assert prune._build_number_arg(value) == value.strip()


@pytest.mark.parametrize("value", ["0.0", "v0.0.1", "0.0.x", "0.0.1.2", "", "latest"])
def test_before_build_rejects_anything_else(value):
    with pytest.raises(argparse.ArgumentTypeError):
        prune._build_number_arg(value)


# -------------------------------------------------------------- the selection


def test_before_build_replaces_the_date_rule():
    stale, kept = prune.select_stale(BUILDS, {"t"}, installed="2026-08-11", before_build="0.0.120")

    # 0.0.119 finished long after the install date and is still selected; the
    # date rule alone would have picked only 0.0.99 and 0.0.100.
    assert [b["buildNumber"] for b in stale] == ["0.0.119", "0.0.99", "0.0.100"]
    assert kept == ["0.0.130"]


def test_the_date_rule_is_unchanged_without_before_build():
    stale, kept = prune.select_stale(BUILDS, {"t"}, installed="2026-08-01")

    assert [b["buildNumber"] for b in stale] == ["0.0.99"]     # inclusive day boundary
    assert kept == ["0.0.130"]


def test_a_tagged_build_below_the_cutoff_is_never_selected():
    stale, kept = prune.select_stale([_build("t", "0.0.50")], {"t"}, None, "0.0.120")

    assert stale == []
    assert kept == ["0.0.50"]


# ------------------------------------------------------------------- the CLI


def test_before_build_needs_an_actor(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prune_stale_builds.py", "--before-build", "0.0.120"])
    monkeypatch.setattr(prune, "capped_actors", lambda only: pytest.fail("reached the API"))

    with pytest.raises(SystemExit) as exc:
        prune.main()

    assert "--actor" in str(exc.value)


def test_before_build_is_a_dry_run_until_delete(monkeypatch, tmp_path):
    detail = {
        "id": "A1", "username": "johnvc", "name": "google-images-api", "isPublic": True,
        "stats": {}, "taggedBuilds": {"latest": {"buildId": "t"}},
    }
    calls = []

    def fake_call(path, method="GET", tries=6):
        calls.append((method, path))
        return {"items": BUILDS} if method == "GET" else None

    monkeypatch.setattr(prune, "capped_actors", lambda only: [detail])
    monkeypatch.setattr(prune, "call", fake_call)
    monkeypatch.setattr(prune, "_integrations", lambda: {})    # no install date on record
    out = tmp_path / "stale.csv"
    argv = ["prune_stale_builds.py", "--actor", "A1", "--before-build", "0.0.120", "--out", str(out)]

    monkeypatch.setattr(sys, "argv", argv)
    prune.main()

    assert [method for method, _ in calls] == ["GET"]           # nothing deleted
    assert "0.0.119 0.0.99 0.0.100" in out.read_text()          # and no date was needed

    calls.clear()
    monkeypatch.setattr(sys, "argv", [*argv, "--delete"])
    prune.main()

    assert [path for method, path in calls if method == "DELETE"] == [
        "/actor-builds/c", "/actor-builds/d", "/actor-builds/e",
    ]
