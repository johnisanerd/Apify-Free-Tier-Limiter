"""Monthly free-tier usage cap for Apify Actors.

    from apify_free_tier import FreeTierGuard

    async with Actor:
        guard = await FreeTierGuard.start()
        if guard.blocked:
            return
        try:
            ...
            if await guard.charge("item_returned", 1):
                break
        finally:
            await guard.close()

Paying Apify users are never limited, never counted, and never slowed down.
"""

from .db import UsageDBError
from .guard import FreeTierGuard

__all__ = ["FreeTierGuard", "UsageDBError"]
__version__ = "0.1.4"
