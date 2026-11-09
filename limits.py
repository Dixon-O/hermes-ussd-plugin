"""In-memory token-bucket rate limiting. No Hermes imports.

State is per process and resets on restart. That is deliberate: limits protect the SMS bill and the
event loop from bursts, they are not an audit trail. The hard daily cap lives in the database.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable


class Limiter:
    """Allows ``burst`` events immediately, then ``per_second`` sustained, per key."""

    def __init__(self, burst: float, per_second: float, *, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if burst <= 0 or per_second <= 0:
            raise ValueError("burst and per_second must be positive")
        self.burst, self.rate, self.max_keys, self.clock = float(burst), float(per_second), max_keys, clock
        self._b: "OrderedDict[str, tuple[float, float]]" = OrderedDict()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self.clock()
        tokens, last = self._b.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        ok = tokens >= cost
        if ok:
            tokens -= cost
        self._b[key] = (tokens, now)  # re-insert at the recent end
        while len(self._b) > self.max_keys:
            self._b.popitem(last=False)  # forget the least recently seen key
        return ok

    def __len__(self) -> int:
        return len(self._b)


def per_minute(n: int, **kw) -> Limiter:
    return Limiter(burst=max(1, n), per_second=max(1, n) / 60.0, **kw)


def per_hour(n: int, **kw) -> Limiter:
    return Limiter(burst=max(1, n), per_second=max(1, n) / 3600.0, **kw)
