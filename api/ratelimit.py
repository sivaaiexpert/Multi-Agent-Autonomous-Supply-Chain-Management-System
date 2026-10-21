"""Token-bucket rate limiter (per key). In-process: with several API processes each has its own budget, so put a gateway limiter in front for a hard global limit."""
from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        self.rate = per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, per_minute))
        self.clock, self.max_keys = clock, max_keys
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds)."""
        now = self.clock()
        with self._lock:
            tokens, last = self._b.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._b[key] = (tokens - 1.0, now)
                ok, wait = True, 0.0
            else:
                self._b[key] = (tokens, now)
                ok, wait = False, (1.0 - tokens) / self.rate if self.rate else 60.0
            if len(self._b) > self.max_keys:                       # bounded memory: drop the oldest half
                for k in sorted(self._b, key=lambda x: self._b[x][1])[: self.max_keys // 2]:
                    self._b.pop(k, None)
        return ok, wait
