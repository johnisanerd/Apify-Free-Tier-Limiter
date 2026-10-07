"""One version number, everywhere a person or an Actor reads it.

The v0.1.8 tag shipped with `__version__` still saying 0.1.7 and README and
INSTALL.md still pinning the v0.1.7 tarball, so anyone following the install
steps got the previous release. These fail the build instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import apify_free_tier

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"Apify-Free-Tier-Limiter/archive/refs/tags/v(\d+\.\d+\.\d+)\.tar\.gz")


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_package_version_matches_pyproject():
    assert apify_free_tier.__version__ == _pyproject_version()


@pytest.mark.parametrize("doc", ["README.md", "INSTALL.md"])
def test_install_pins_point_at_this_release(doc):
    pins = PIN.findall((ROOT / doc).read_text())

    assert pins, f"no release-tarball pin found in {doc}"
    assert set(pins) == {_pyproject_version()}
