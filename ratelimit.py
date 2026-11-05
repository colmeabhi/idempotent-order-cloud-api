"""Per-client-IP rate limiting backed by a shared store.

With ``memory://`` every ECS task keeps its own counters, so N tasks behind
the ALB allow N x the configured limit. Pointing ``RATE_LIMIT_STORAGE_URI``
at Redis (``redis://host:6379``) makes every task share one counter per IP.

The async limiter is used so a Redis round trip does not block the event loop.
If Redis is unreachable the limiter fails open (request allowed, error logged):
an outage of the limiter should not take the API down with it.
"""

import json
import logging
import os
import time

from limits import parse
from limits.aio.strategies import SlidingWindowCounterRateLimiter
from limits.storage import storage_from_string
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("orders")

# Load balancer health checks must never be throttled, or a busy IP could get
# a healthy task killed.
DEFAULT_EXEMPT = ("/health",)


def _async_uri(uri):
    """Accept plain URIs (redis://, memory://) and map them to limits' async schemes."""
    return uri if uri.startswith("async+") else f"async+{uri}"


def build_limiter(storage_uri=None):
    uri = _async_uri(storage_uri or os.getenv("RATE_LIMIT_STORAGE_URI", "memory://"))
    options = {}
    if "redis" in uri:
        options = {
            "implementation": "redispy",
            "socket_connect_timeout": 0.5,
            "socket_timeout": 0.5,
        }
    return SlidingWindowCounterRateLimiter(storage_from_string(uri, **options))


def client_ip(request: Request):
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # The ALB appends the real peer address as the last entry; earlier
        # entries are client-controlled and can't be trusted.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit=None, storage_uri=None, enabled=None,
                 exempt_paths=DEFAULT_EXEMPT, namespace="order-api"):
        super().__init__(app)
        if enabled is None:
            enabled = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
        self.enabled = enabled
        self.limit = parse(limit or os.getenv("RATE_LIMIT", "10/minute"))
        self.limiter = build_limiter(storage_uri) if enabled else None
        self.exempt_paths = set(exempt_paths)
        self.namespace = namespace
        self._last_error_log = 0.0

    async def dispatch(self, request, call_next):
        if not self.enabled or request.url.path in self.exempt_paths:
            return await call_next(request)

        ip = client_ip(request)
        try:
            allowed = await self.limiter.hit(self.limit, self.namespace, ip)
        except Exception as exc:  # store down -> fail open, log at most once per 10s
            now = time.monotonic()
            if now - self._last_error_log > 10:
                self._last_error_log = now
                logger.warning(json.dumps({"level": "WARN", "msg": "rate_limiter_unavailable",
                                           "detail": str(exc)}))
            allowed = True

        if not allowed:
            reset_at, _remaining = await self._window_stats(ip)
            retry_after = max(1, int(reset_at - time.time())) if reset_at else 60
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": str(retry_after)})
        return await call_next(request)

    async def _window_stats(self, ip):
        try:
            return await self.limiter.get_window_stats(self.limit, self.namespace, ip)
        except Exception:
            return None, 0
