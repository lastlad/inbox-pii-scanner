"""Async token bucket for Gmail API throttling.

The Gmail per-user quota is ~250 quota units/sec. Each ``messages.get`` and
``attachments.get`` costs 5 units, so the plan caps us at 20 calls/sec to
stay well under. We model that as a leaky bucket with capacity == rate so
short 1-second bursts are allowed but the steady-state rate stays bounded.

Sync orchestration lives in async land; the actual Gmail HTTP calls run
inside ``asyncio.to_thread``. The bucket is the only shared coordination
point — every worker awaits ``acquire`` before each Gmail call.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(rate)
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until at least one token is available, then consume it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(wait)
