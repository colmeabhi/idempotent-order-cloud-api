"""Shared fixtures. Tests run against a real Postgres (and Redis where needed).

Connection settings come from the same env vars the app uses (DB_HOST, ...),
defaulting to localhost. CI provides both as service containers.
"""

import os
import pathlib
import socket
import subprocess
import sys
import time

import httpx
import psycopg2
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The app reads these at import time; the API tests must not be throttled.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_STORAGE_URI", "memory://")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/15")


def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "orders"), user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"))


@pytest.fixture(scope="session", autouse=True)
def schema():
    conn = db_connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute((ROOT / "init_db.sql").read_text())
    conn.close()


@pytest.fixture
def db():
    """A clean database for every test; yields an autocommit connection."""
    conn = db_connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE ledger, orders, idempotency_records, items RESTART IDENTITY")
    yield conn
    conn.close()


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """A real uvicorn with several worker processes, like several ECS tasks
    sharing one database. Needed to get genuinely concurrent requests."""
    port = _free_port()
    env = {**os.environ, "RATE_LIMIT_ENABLED": "false"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port),
         "--workers", os.getenv("TEST_SERVER_WORKERS", "8"), "--log-level", "warning"],
        cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("live server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def redis_available():
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping()
        return True
    except Exception:
        return False


@pytest.fixture
def redis_url():
    if not redis_available():
        pytest.skip(f"Redis not reachable at {REDIS_URL}")
    import redis
    redis.Redis.from_url(REDIS_URL).flushdb()
    return REDIS_URL
