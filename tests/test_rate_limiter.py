"""TokenBucket pacing tests.

Use a high rate (50/sec) so tests stay fast (<200ms) but still exercise the
rate-limited path. The bucket starts full, so the first ``capacity`` calls
must be (close to) instantaneous; the next call has to wait for refill.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from inboxaudit.gmail.rate_limiter import TokenBucket


@pytest.mark.asyncio
async def test_initial_burst_is_immediate():
    bucket = TokenBucket(rate=50)
    start = time.monotonic()
    for _ in range(50):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # 50 tokens prefilled; whole burst should be near-instant.
    assert elapsed < 0.05, f"initial burst took {elapsed:.3f}s, expected <50ms"


@pytest.mark.asyncio
async def test_steady_state_paces_to_rate():
    rate = 50
    bucket = TokenBucket(rate=rate)
    # Burn the initial burst so subsequent acquires hit the refill path.
    for _ in range(rate):
        await bucket.acquire()
    # Now ask for `rate` more tokens — these must be paced.
    start = time.monotonic()
    for _ in range(rate):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # ~1 second expected; allow a generous lower bound for scheduling jitter.
    assert elapsed >= 0.7, f"steady-state was {elapsed:.3f}s, expected ≥0.7s"


@pytest.mark.asyncio
async def test_concurrent_workers_respect_global_rate():
    """Multiple coroutines pulling from one bucket get a combined rate
    bounded by the bucket — not N×rate."""
    rate = 30
    bucket = TokenBucket(rate=rate)
    # Burn the prefill so we measure steady-state.
    for _ in range(rate):
        await bucket.acquire()

    target_per_worker = 15
    n_workers = 4

    async def worker():
        for _ in range(target_per_worker):
            await bucket.acquire()

    start = time.monotonic()
    await asyncio.gather(*[worker() for _ in range(n_workers)])
    elapsed = time.monotonic() - start
    expected = (target_per_worker * n_workers) / rate  # 60/30 = 2.0 s
    # Should land near `expected`; allow 30% headroom either side.
    assert expected * 0.7 <= elapsed <= expected * 1.5, (
        f"elapsed={elapsed:.3f}s, expected≈{expected:.2f}s"
    )


def test_invalid_rate_rejected():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
    with pytest.raises(ValueError):
        TokenBucket(rate=-1)
