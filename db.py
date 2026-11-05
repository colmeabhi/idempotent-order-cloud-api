"""Postgres connection handling.

A ``ThreadedConnectionPool`` keeps connections open between requests so a
request no longer pays for TCP + TLS + auth on every call.

psycopg2's pool raises ``PoolError`` as soon as it is exhausted instead of
waiting, so a ``BoundedSemaphore`` sized to ``maxconn`` sits in front of it:
callers queue for up to ``DB_POOL_TIMEOUT`` seconds and only then fail.

Setting ``DB_POOL_MAX=0`` disables pooling (one connection per request).
That mode exists so the load test can measure before/after on the same code.

A pooled connection can die while idle (Postgres restart, RDS failover), which
the client only notices when it next uses it. So a connection idle for more
than ``DB_POOL_PING_AFTER`` seconds is checked before being handed out, and
replaced if dead. The check runs before any application SQL, so replacing the
connection can never repeat a write. Busy connections skip it to avoid the
extra round trips.
"""

import contextlib
import os
import threading
import time

import psycopg2
import psycopg2.pool


class PoolTimeout(Exception):
    """No connection became free within DB_POOL_TIMEOUT."""


def _dsn_kwargs():
    return dict(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "orders"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
        application_name="order-api",
    )


class Database:
    def __init__(self, minconn=None, maxconn=None, timeout=None, ping_after=None):
        self.maxconn = int(os.getenv("DB_POOL_MAX", "10")) if maxconn is None else maxconn
        # psycopg2 keeps at most `minconn` idle connections and *closes* any
        # other connection when it is returned. With a low minconn, a burst of
        # traffic reconnects on nearly every request - while holding the pool's
        # global lock - which is slower than not pooling at all. So by default
        # the whole pool is kept warm: minconn == maxconn.
        self.minconn = int(os.getenv("DB_POOL_MIN", str(self.maxconn))) if minconn is None else minconn
        self.timeout = float(os.getenv("DB_POOL_TIMEOUT", "5")) if timeout is None else timeout
        self.ping_after = float(os.getenv("DB_POOL_PING_AFTER", "1")) if ping_after is None else ping_after
        self._last_used = {}  # id(conn) -> time.monotonic() when last returned
        self._pool = None
        self._slots = None
        self._lock = threading.Lock()

    @property
    def pooled(self):
        return self.maxconn > 0

    def _ensure_pool(self):
        # Lazy, so the app can boot (and report 503 on /health) while the DB
        # is still unreachable instead of crashing on import.
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        min(self.minconn, self.maxconn), self.maxconn, **_dsn_kwargs())
                    self._slots = threading.BoundedSemaphore(self.maxconn)
        return self._pool

    def _discard(self, pool, conn):
        self._last_used.pop(id(conn), None)
        pool.putconn(conn, close=True)

    def _is_alive(self, conn):
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return True
        except psycopg2.Error:
            return False

    def _checkout(self, pool):
        """Get a usable connection, replacing any that died while idle.

        Only a recently returned connection skips the check. One with no
        _last_used entry may have been opened long ago to fill the pool's
        minconn, so it is checked too. The loop ends because the pool holds at
        most maxconn dead connections; if Postgres is really down, opening a
        new connection raises.
        """
        while True:
            conn = pool.getconn()
            last_used = self._last_used.get(id(conn))
            recently_used = last_used is not None and time.monotonic() - last_used < self.ping_after
            if not conn.closed and (recently_used or self._is_alive(conn)):
                return conn
            self._discard(pool, conn)

    @contextlib.contextmanager
    def connection(self):
        """Yield a connection. Rolls back on error; never leaks a slot."""
        if not self.pooled:
            conn = psycopg2.connect(**_dsn_kwargs())
            try:
                yield conn
            finally:
                conn.close()
            return

        pool = self._ensure_pool()
        if not self._slots.acquire(timeout=self.timeout):
            raise PoolTimeout(f"no DB connection free within {self.timeout}s")
        conn = None
        broken = False
        try:
            conn = self._checkout(pool)
            yield conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # Connection died (e.g. RDS failover/restart): don't hand it out again.
            broken = True
            raise
        finally:
            if conn is not None:
                if not broken and not conn.closed:
                    try:
                        conn.rollback()  # leave nothing open for the next borrower
                    except psycopg2.Error:
                        broken = True
                if broken or conn.closed:
                    self._discard(pool, conn)
                else:
                    self._last_used[id(conn)] = time.monotonic()
                    pool.putconn(conn)
            self._slots.release()

    def close(self):
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None
                self._slots = None
                self._last_used.clear()
