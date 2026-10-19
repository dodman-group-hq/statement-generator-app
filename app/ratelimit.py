"""
A small in-process sliding-window rate limiter.

Good enough for this app because it runs as a single worker (see the
Dockerfile): the counters live in process memory, so with several workers each
would keep its own and the effective limit would be multiplied. If you ever
scale out, move this to Redis along with the job state.
"""
import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Records an event for `key` and returns True if it's within the limit.
        Rejected attempts are NOT recorded, so a blocked caller recovers as soon
        as their oldest allowed event ages out of the window."""
        now = time.monotonic() if now is None else now
        with self._lock:
            events = self._events[key]
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.max_events:
                return False
            events.append(now)
            if len(self._events) > 10_000:  # keep memory bounded under a spray of distinct keys
                self._purge(now)
            return True

    def _purge(self, now: float) -> None:
        for k in [k for k, ev in self._events.items() if not ev or now - ev[-1] >= self.window]:
            del self._events[k]