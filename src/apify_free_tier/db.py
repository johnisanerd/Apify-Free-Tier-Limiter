"""Thin PostgREST client for the two usage RPCs.

Deliberately raw httpx rather than supabase-py: the Actors in this fleet run
under a hard 128 MB memory cap and already declare httpx as a dependency, so
this adds no new install weight and no new import cost.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx


class UsageDBError(RuntimeError):
    """Any failure talking to the usage ledger. Always caught by the guard."""


class UsageDB:
    """Calls `get_usage` and `increment_usage`. Never retries on its own."""

    def __init__(self, url: str, key: str, timeout: float = 2.5) -> None:
        self._endpoint = url.rstrip("/") + "/rest/v1/rpc"
        self._client = httpx.AsyncClient(
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(connect=2.0, read=timeout, write=2.0, pool=2.0),
        )

    async def _rpc(self, function: str, params: dict[str, object]) -> Decimal:
        try:
            response = await self._client.post(f"{self._endpoint}/{function}", json=params)
        except httpx.HTTPError as exc:
            # Type name only. The full exception can carry the project URL, and
            # this string lands in a public Actor log.
            raise UsageDBError(type(exc).__name__) from None

        if response.status_code >= 400:
            raise UsageDBError(f"HTTP {response.status_code}")

        try:
            return Decimal(str(response.json()))
        except (ValueError, InvalidOperation) as exc:
            raise UsageDBError(f"bad response ({type(exc).__name__})") from None

    async def get_usage(self, user_id: str, actor_id: str) -> Decimal:
        """Dollars this user has already spent on this Actor this month."""
        return await self._rpc("get_usage", {"p_user_id": user_id, "p_actor_id": actor_id})

    async def increment_usage(self, user_id: str, actor_id: str, amount: Decimal) -> Decimal:
        """Atomically add `amount` and return the new authoritative total."""
        return await self._rpc(
            "increment_usage",
            {"p_user_id": user_id, "p_actor_id": actor_id, "p_amount": str(amount)},
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 - closing must never break a run
            pass
