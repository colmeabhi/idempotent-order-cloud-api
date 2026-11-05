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


def terminate(db, pids):
    """Kill sessions the way an RDS restart / failover would."""
    with db.cursor() as cur:
        for pid in pids:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.05)  # termination is asynchronous


def test_dead_connection_is_discarded(db):
    # Used moments ago, so the liveness check is skipped: the query itself fails,
    # and the connection must still not be handed out again.
    d = Database(minconn=1, maxconn=2, timeout=0.5, ping_after=3600)
    pid = backend_pid(d)
    terminate(db, [pid])
    with pytest.raises(psycopg2.OperationalError):
        backend_pid(d)
    assert backend_pid(d) != pid  # next borrower gets a fresh, working connection
    d.close()


def test_idle_dead_connections_are_replaced_before_use(db):
    """Regression: after a Postgres restart every idle connection used to cost
    one client a 500, up to DB_POOL_MAX of them per process."""
    d = Database(minconn=3, maxconn=3, timeout=1, ping_after=0)
    barrier = threading.Barrier(3)
    pids = set()

    def borrow():
        with d.connection() as conn, conn.cursor() as cur:
            barrier.wait()  # hold all 3 at once so each backend is distinct
            cur.execute("SELECT pg_backend_pid()")
            pids.add(cur.fetchone()[0])

    threads = [threading.Thread(target=borrow) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(pids) == 3

    terminate(db, pids)
    # None of these may raise, even though every pooled connection is dead.
    assert {backend_pid(d) for _ in range(5)}.isdisjoint(pids)
    d.close()


def test_api_survives_postgres_restart(client, db, monkeypatch):
    import main
    assert client.post("/items", json={"name": "a", "value": 1}).status_code == 201
    monkeypatch.setattr(main.db, "ping_after", 0)
    with db.cursor() as cur:
        cur.execute("SELECT pid FROM pg_stat_activity WHERE application_name = 'order-api'")
        pids = [row[0] for row in cur.fetchall()]
    assert pids
    terminate(db, pids)
    assert client.get("/items/1").status_code == 200


def test_unreachable_database_still_raises(db):
    d = Database(minconn=1, maxconn=1, timeout=0.5, ping_after=0)
    pid = backend_pid(d)
    terminate(db, [pid])
    d._pool._kwargs.update(host="127.0.0.1", port="1", connect_timeout=1)  # can't reconnect
    for _ in range(2):  # maxconn=1: a leaked slot would turn the 2nd try into PoolTimeout
        with pytest.raises(psycopg2.OperationalError):
            backend_pid(d)


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
