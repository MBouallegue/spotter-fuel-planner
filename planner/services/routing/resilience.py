"""Keep a failing provider from slowing every request, and keep our calls to OSRM within
its one-request-per-second policy.

State lives in Django's cache, so every thread of a process shares it (and every process,
once the cache is Redis). Times are wall-clock seconds, which processes can compare.
"""

import math
import threading
import time

from django.core.cache import cache

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    """Skip a provider that keeps failing, then try it again after a pause.

    closed:    calls go through, and failures in a row are counted.
    open:      after ``threshold`` failures in a row, calls are skipped for ``reset_seconds``.
    half-open: once that time is up, one trial call goes through. Success closes the
               breaker; failure opens it again for another ``reset_seconds``.

    Only the provider's own trouble counts as a failure (network errors, timeouts, 5xx,
    429, malformed bodies), never a real answer such as "no road between these points".
    """

    def __init__(self, name: str, threshold: int, reset_seconds: int):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._failures = f"cb:{name}:failures"
        self._open_until = f"cb:{name}:open_until"
        self._trial = f"cb:{name}:trial"

    def state(self) -> str:
        open_until = cache.get(self._open_until)
        if open_until is None:
            return CLOSED
        return OPEN if time.time() < open_until else HALF_OPEN

    def allow_request(self) -> bool:
        state = self.state()
        if state == CLOSED:
            return True
        # cache.add only succeeds for the first caller, so exactly one trial goes through.
        # It expires on its own in case that request dies without reporting back.
        return state == HALF_OPEN and cache.add(self._trial, True, timeout=self.reset_seconds)

    def record_success(self) -> None:
        cache.delete_many([self._failures, self._open_until, self._trial])

    def record_failure(self) -> None:
        cache.add(self._failures, 0, timeout=None)
        # A failed trial finds the count still at or above the threshold, so it re-opens.
        if cache.incr(self._failures) >= self.threshold:
            cache.set(self._open_until, time.time() + self.reset_seconds, timeout=None)
            cache.delete(self._trial)

    def seconds_until_half_open(self) -> int:
        open_until = cache.get(self._open_until)
        if open_until is None:
            return 0
        return max(0, math.ceil(open_until - time.time()))


class OutboundThrottle:
    """At least ``min_interval_seconds`` between two calls to one service: a call that
    comes too soon sleeps for the remainder.

    The last call's time lives in the cache. A lock serializes this process's threads, so
    two cold requests cannot slip through together; across processes it is best effort.
    """

    # One lock for the whole process: providers, and their throttles, are built per request.
    _lock = threading.Lock()

    def __init__(self, name: str, min_interval_seconds: float):
        self.min_interval = min_interval_seconds
        self._last_call = f"throttle:{name}:last_call"

    def wait_turn(self) -> None:
        with self._lock:
            last_call = cache.get(self._last_call)
            if last_call is not None:
                wait = last_call + self.min_interval - time.time()
                if wait > 0:
                    time.sleep(wait)
            cache.set(self._last_call, time.time(), timeout=60)
