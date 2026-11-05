import threading
import time

import psycopg2
import pytest

from db import Database, PoolTimeout


def backend_pid(database):
    with database.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return cur.fetchone()[0]


@pytest.fixture
def pool():
    d = Database(minconn=1, maxconn=2, timeout=0.5)
    yield d
    d.close()


def test_connections_are_reused(pool):
    assert backend_pid(pool) == backend_pid(pool)


def test_no_pool_mode_opens_a_new_connection_each_time():
    d = Database(maxconn=0)
    assert backend_pid(d) != backend_pid(d)


def test_uncommitted_work_is_rolled_back_on_return(pool, db):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO items (name, value) VALUES ('x', 1)")
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM items")
        assert cur.fetchone()[0] == 0


def test_dead_connection_is_discarded(pool, db):
    pid = backend_pid(pool)
    with db.cursor() as cur:  # simulate RDS restart / failover killing the session
        cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    with pytest.raises(psycopg2.OperationalError):
        backend_pid(pool)
    assert backend_pid(pool) != pid  # next borrower gets a fresh, working connection


def test_exhausted_pool_waits_then_times_out(pool):
    held = threading.Event()
    release = threading.Event()

    def hold():
        with pool.connection():
            held.set()
            release.wait()

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for t in threads:
        t.start()
    held.wait(2)
    time.sleep(0.05)

    start = time.monotonic()
    with pytest.raises(PoolTimeout):
        with pool.connection():
            pass
    assert time.monotonic() - start >= 0.4  # waited, didn't fail instantly

    release.set()
    for t in threads:
        t.join()
    assert backend_pid(pool)  # usable again once released


def test_waiter_gets_connection_when_one_is_released(pool):
    release = threading.Event()

    def hold():
        with pool.connection():
            release.wait()

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for t in threads:
        t.start()
    threading.Timer(0.2, release.set).start()
    assert backend_pid(pool)  # blocks ~0.2s, then succeeds within the 0.5s timeout
    for t in threads:
        t.join()


def test_connections_survive_concurrent_use():
    """Regression: with psycopg2's default behaviour (minconn=1) connections
    returned while others were in use got closed, so every burst reconnected."""
    d = Database(maxconn=4, timeout=2)
    barrier = threading.Barrier(4)

    def borrow(pids):
        with d.connection() as conn, conn.cursor() as cur:
            barrier.wait()  # all 4 checked out at once
            cur.execute("SELECT pg_backend_pid()")
            pids.add(cur.fetchone()[0])

    def burst():
        pids = set()
        ts = [threading.Thread(target=borrow, args=(pids,)) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return pids

    first, second = burst(), burst()
    assert len(first) == 4
    assert first == second  # same 4 backends reused, none reconnected
    d.close()
