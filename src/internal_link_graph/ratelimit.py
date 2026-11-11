"""Per-host politeness delay shared between crawler threads."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class HostRateLimiter:
    """Guarantees a minimum interval between requests to the same host.

    Each call to :meth:`wait` reserves the next free slot for the host and
    sleeps until it arrives, so concurrent workers queue up fairly instead of
    all firing at once.  ``clock`` and ``sleep`` are injectable for tests.
    """

    def __init__(
        self,
        delay: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if delay < 0:
            raise ValueError("delay must be >= 0")
        self.delay = delay
        self._clock = clock
        self._sleep = sleep
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()
        self.total_waited = 0.0

    def reserve(self, host: str, delay: float | None = None) -> float:
        """Reserve a slot for ``host`` and return how long the caller must wait."""
        interval = self.delay if delay is None else max(delay, 0.0)
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + interval
            wait_for = slot - now
            self.total_waited += wait_for
        return wait_for

    def wait(self, host: str, delay: float | None = None) -> float:
        """Block until the host may be contacted again; returns the time slept."""
        wait_for = self.reserve(host, delay)
        if wait_for > 0:
            self._sleep(wait_for)
        return wait_for
