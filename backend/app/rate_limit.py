"""Small process-local rate limits for the single-worker deployment."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque

from fastapi import HTTPException, Request


_lock = threading.Lock()
_requests: dict[str, deque[float]] = {}
_MAX_BUCKETS = 10_000
_MAX_WINDOW_SECONDS = 3600


def reset_rate_limits() -> None:
    """Clear process-local buckets for isolated tests."""
    with _lock:
        _requests.clear()


def enforce_rate_limit(
    request: Request,
    scope: str,
    limit: int,
    window_seconds: int,
    identity: str | None = None,
) -> None:
    client_host = request.client.host if request.client else "unknown"
    keys = [f"{scope}:ip:{client_host}"]
    if identity:
        identity_hash = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()
        keys.append(f"{scope}:identity:{identity_hash}")

    now = time.monotonic()
    retry_after = 0.0
    with _lock:
        if len(_requests) >= _MAX_BUCKETS:
            for key, timestamps in list(_requests.items()):
                while timestamps and now - timestamps[0] >= _MAX_WINDOW_SECONDS:
                    timestamps.popleft()
                if not timestamps:
                    _requests.pop(key, None)

        missing_keys = sum(key not in _requests for key in keys)
        if len(_requests) + missing_keys > _MAX_BUCKETS:
            raise HTTPException(
                status_code=503,
                detail="Rate limit capacity is temporarily exhausted.",
            )

        for key in keys:
            timestamps = _requests.setdefault(key, deque())
            while timestamps and now - timestamps[0] >= window_seconds:
                timestamps.popleft()
            if len(timestamps) >= limit:
                retry_after = max(
                    retry_after,
                    window_seconds - (now - timestamps[0]),
                )

        if retry_after:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please wait before trying again.",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )

        for key in keys:
            _requests[key].append(now)
