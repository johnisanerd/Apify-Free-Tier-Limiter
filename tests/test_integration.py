"""Integration tests against a real Supabase project.

Skipped unless SUPABASE_URL and SUPABASE_KEY are set, so the normal unit run
stays offline and fast:

    SUPABASE_URL=... SUPABASE_KEY=... uv run pytest tests/test_integration.py

These exist to prove the things a mock cannot: that the anon key really is
confined to the two RPCs, that the server rejects a negative amount, and that
concurrent increments do not lose writes.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import httpx
import pytest

from apify_free_tier.db import UsageDB, UsageDBError

URL = os.getenv("SUPABASE_URL")
KEY = os.getenv("SUPABASE_KEY")

pytestmark = pytest.mark.skipif(
    not (URL and KEY), reason="SUPABASE_URL / SUPABASE_KEY not set"
)


@pytest.fixture
def db():
    return UsageDB(URL, KEY)


@pytest.fixture
def ids():
    """A unique user each run, so tests never collide with real ledger rows."""
    return f"test-{uuid.uuid4()}", "test-actor"


async def test_unknown_user_starts_at_zero(db, ids):
    assert await db.get_usage(*ids) == Decimal("0")


async def test_increments_accumulate(db, ids):
    assert await db.increment_usage(*ids, Decimal("0.01")) == Decimal("0.010000")
    assert await db.increment_usage(*ids, Decimal("0.02")) == Decimal("0.030000")
    assert await db.get_usage(*ids) == Decimal("0.030000")


async def test_sub_cent_amounts_keep_their_precision(db, ids):
    await db.increment_usage(*ids, Decimal("0.00025"))
    assert await db.get_usage(*ids) == Decimal("0.000250")


async def test_negative_amount_is_rejected(db, ids):
    """Otherwise anyone holding the key could clear their own tab."""
    await db.increment_usage(*ids, Decimal("0.05"))
    with pytest.raises(UsageDBError):
        await db.increment_usage(*ids, Decimal("-0.05"))
    assert await db.get_usage(*ids) == Decimal("0.050000")


async def test_absurd_amount_is_rejected(db, ids):
    with pytest.raises(UsageDBError):
        await db.increment_usage(*ids, Decimal("1000"))


async def test_concurrent_increments_do_not_lose_writes(db, ids):
    """The race the counter design exists to close."""
    await asyncio.gather(*(db.increment_usage(*ids, Decimal("0.01")) for _ in range(20)))
    assert await db.get_usage(*ids) == Decimal("0.200000")


async def test_key_cannot_read_the_table_directly(ids):
    """RLS is on with no policies, and direct grants are revoked."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{URL.rstrip('/')}/rest/v1/free_tier_usage",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
            params={"select": "*"},
        )
    assert response.status_code >= 400 or response.json() == []


async def test_key_cannot_write_the_table_directly(ids):
    user_id, actor_id = ids
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{URL.rstrip('/')}/rest/v1/free_tier_usage",
            headers={
                "apikey": KEY,
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
            },
            json={"user_id": user_id, "actor_id": actor_id, "period": "2026-08"},
        )
    assert response.status_code >= 400
