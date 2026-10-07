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

Actors that buy upstream work per call can ask first: `guard.affordable(event)`
is how many more results the free allowance pays for (None when no free limit
applies), and `await guard.exhaust()` stops the run when that is too few.

Paying Apify users are never limited, never counted, and never slowed down.
"""

from .db import UsageDBError
from .guard import FreeTierGuard

__all__ = ["FreeTierGuard", "UsageDBError"]
__version__ = "0.1.9"
