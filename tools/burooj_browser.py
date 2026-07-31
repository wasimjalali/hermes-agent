"""Shared browser-automation helpers for the Burooj Build and Design tools.

``preview``, ``a11y_check`` and ``visual_diff`` all drive Playwright over the
same dev server, and all three had the same three bugs. They live here once.

**Why not ``asyncio.get_event_loop()``.** That call is deprecated, and on 3.12+
it raises when no loop is set on the current thread. The old pattern also ran
the coroutine on a borrowed loop when one happened to be running, then wrapped
the thread-pool path in ``with ThreadPoolExecutor(...)``, whose ``__exit__``
calls ``shutdown(wait=True)``. A timeout therefore blocked forever on the very
worker it had just given up on, instead of returning.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Coroutine, TypeVar

logger = logging.getLogger("hermes.burooj_browser")

T = TypeVar("T")

# Message used when Playwright is absent. Callers treat this specific string as
# a legitimate skip rather than a failure, so keep it stable.
PLAYWRIGHT_MISSING = (
    "playwright not installed. Run: pip install playwright && "
    "python -m playwright install chromium"
)

# Next.js dev keeps an HMR websocket open, so "networkidle" never fires and
# every navigation burns its full timeout before continuing. "load" is the
# right signal for a dev server.
_WAIT_UNTIL = "load"

# Per-navigation timeout.
_NAV_TIMEOUT_MS = 20000

# Overall budget for one capture pass across all routes.
_CAPTURE_TIMEOUT = 300


def run_async(coro: "Coroutine[Any, Any, T]", timeout: int = _CAPTURE_TIMEOUT) -> T:
    """Run *coro* to completion from sync code, whatever the caller's context.

    Always uses a dedicated thread with its own event loop, so it is safe
    whether or not the caller already has a running loop. The executor is shut
    down without waiting on timeout, so a hung capture surfaces as an exception
    instead of blocking the turn.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(asyncio.run, coro)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"Browser capture did not finish within {timeout}s"
            ) from exc
    finally:
        # wait=False: never block the caller on a worker that already overran.
        pool.shutdown(wait=False, cancel_futures=True)


async def new_page(browser: Any, width: int = 1280, height: int = 800) -> Any:
    """Create a fresh context and page at the given viewport.

    A fresh context per route is what keeps console listeners from leaking
    across routes: handlers registered on a shared page stayed registered, so
    a later route's console output kept appending to an earlier route's
    already-returned error list.
    """
    context = await browser.new_context(viewport={"width": width, "height": height})
    return await context.new_page()
