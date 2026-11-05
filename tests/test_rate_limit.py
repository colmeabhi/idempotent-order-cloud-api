from fastapi import FastAPI
from fastapi.testclient import TestClient

from ratelimit import RateLimitMiddleware


def make_app(limit="3/minute", storage_uri="memory://", **kw):
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limit=limit, storage_uri=storage_uri,
                       enabled=True, **kw)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def hit(client, n, path="/ping", ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return [client.get(path, headers=headers).status_code for _ in range(n)]


def test_limit_enforced():
    with TestClient(make_app()) as c:
        assert hit(c, 5) == [200, 200, 200, 429, 429]


def test_429_has_retry_after():
    with TestClient(make_app(limit="1/minute")) as c:
        c.get("/ping")
        r = c.get("/ping")
        assert r.status_code == 429
        assert 1 <= int(r.headers["Retry-After"]) <= 60


def test_health_is_exempt():
    with TestClient(make_app(limit="1/minute")) as c:
        assert hit(c, 5, "/health") == [200] * 5


def test_limit_is_per_client_ip_using_last_forwarded_entry():
    with TestClient(make_app(limit="2/minute")) as c:
        assert hit(c, 3, ip="spoofed, 1.1.1.1") == [200, 200, 429]
        # A different spoofed prefix doesn't buy a fresh bucket...
        assert hit(c, 1, ip="other, 1.1.1.1") == [429]
        # ...but a different real client (last hop) does.
        assert hit(c, 2, ip="2.2.2.2") == [200, 200]


def test_memory_storage_is_per_instance():
    """The original problem: two tasks with memory storage each allow the full limit."""
    with TestClient(make_app()) as a, TestClient(make_app()) as b:
        assert hit(a, 3) == [200] * 3
        assert hit(b, 3) == [200] * 3


def test_redis_storage_is_shared_across_instances(redis_url):
    """Two app instances (= two ECS tasks) pointed at one Redis share the limit."""
    with TestClient(make_app(storage_uri=redis_url)) as a, \
         TestClient(make_app(storage_uri=redis_url)) as b:
        assert hit(a, 2) == [200, 200]
        assert hit(b, 2) == [200, 429]
        assert hit(a, 1) == [429]


def test_fails_open_when_redis_is_down():
    app = make_app(limit="1/minute", storage_uri="redis://127.0.0.1:1/0")
    with TestClient(app) as c:
        assert hit(c, 3) == [200, 200, 200]
