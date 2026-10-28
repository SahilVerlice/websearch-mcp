"""Per-engine circuit breaker.

An engine that starts answering with captchas / 429s will keep doing so for a
while. Without a breaker every subsequent search pays its timeout and then
reports the same failure. The breaker remembers the failure and skips the
engine for a cool-down period, reporting it as ``cooldown`` instead.
"""
from __future__ import annotations

import threading
import time

from .types import BlockedError, HttpStatusError

# First cool-down, doubled on each consecutive failure, capped.
BASE_COOLDOWN = 60.0
MAX_COOLDOWN = 900.0


class Breaker:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}

    def available(self, engine: str) -> bool:
        with self._lock:
            entry = self._state.get(engine)
            if not entry:
                return True
            return time.monotonic() >= entry["until"]

    def remaining(self, engine: str) -> float:
        with self._lock:
            entry = self._state.get(engine)
            if not entry:
                return 0.0
            return max(0.0, entry["until"] - time.monotonic())

    def record_success(self, engine: str) -> None:
        with self._lock:
            self._state.pop(engine, None)

    def record_failure(self, engine: str, exc: Exception) -> None:
        """Trip the breaker on evidence the engine is refusing us.

        That means a bot challenge (:class:`BlockedError`) or a server-side
        failure (:class:`HttpStatusError` with a 5xx, or a 429 the body did not
        label as a challenge). A timeout, a connection reset or a parse error
        is not evidence of a block — those engines are retried normally on the
        next call, because a single slow request should not cost five minutes
        of that engine.

        Both classes have to count: ``net.request`` only raises ``BlockedError``
        for 401/403/429, so an engine answering every request with a 500 or a
        JSON-bodied 429 used to retry forever while the breaker never opened —
        the exact cost the breaker exists to avoid.
        """
        if not isinstance(exc, (BlockedError, HttpStatusError)):
            return
        if isinstance(exc, HttpStatusError) and not (
                exc.status == 429 or 500 <= exc.status < 600):
            # A 404/410 is the engine's answer about that URL, not about us.
            return
        with self._lock:
            entry = self._state.setdefault(engine, {"fails": 0, "until": 0.0})
            entry["fails"] += 1
            cooldown = min(MAX_COOLDOWN, BASE_COOLDOWN * (2 ** (entry["fails"] - 1)))
            entry["until"] = time.monotonic() + cooldown

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                name: round(max(0.0, entry["until"] - now), 1)
                for name, entry in self._state.items()
                if entry["until"] > now
            }


BREAKER = Breaker()
