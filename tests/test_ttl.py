import pathlib

import cleanup
from tests.conftest import ROOT, count, db_connect

ORDER = {"customer_id": "c1", "item_id": "i1", "quantity": 1}


def insert_key(conn, key, age_hours, request_hash="h", order_id="old"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO idempotency_records (idem_key, request_hash, response_body, status_code, created_at)"
            " VALUES (%s, %s, %s, 201, now() - make_interval(hours => %s))",
            (key, request_hash, f'{{"order_id": "{order_id}", "status": "created"}}', age_hours))


def test_expired_key_is_reclaimed_by_a_new_request(client, db):
    # Old record with a different payload hash: would be a 409 if still live.
    insert_key(db, "k1", age_hours=25)
    r = client.post("/orders", json=ORDER, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    assert r.json()["order_id"] != "old"
    assert count(db, "orders") == 1
    # ...and the key is live again: a retry replays the new order.
    again = client.post("/orders", json=ORDER, headers={"Idempotency-Key": "k1"})
    assert again.json() == r.json()


def test_unexpired_key_still_conflicts(client, db):
    insert_key(db, "k1", age_hours=23)
    r = client.post("/orders", json=ORDER, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 409
    assert count(db, "orders") == 0


def test_purge_deletes_only_expired_keys_in_batches(db):
    for i in range(5):
        insert_key(db, f"old-{i}", age_hours=30)
    insert_key(db, "fresh", age_hours=1)
    conn = db_connect()
    try:
        assert cleanup.purge_expired(conn, ttl=24 * 3600, batch_size=2) == 5
    finally:
        conn.close()
    with db.cursor() as cur:
        cur.execute("SELECT idem_key FROM idempotency_records")
        assert [r[0] for r in cur.fetchall()] == ["fresh"]


def test_purge_skips_when_another_process_holds_the_lock(db):
    insert_key(db, "old", age_hours=30)
    with db.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (cleanup.CLEANUP_LOCK_ID,))
    conn = db_connect()
    try:
        assert cleanup.purge_expired(conn, ttl=24 * 3600) is None
    finally:
        conn.close()
        with db.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (cleanup.CLEANUP_LOCK_ID,))
    assert count(db, "idempotency_records") == 1


def test_run_once_uses_pool(db):
    from db import Database
    insert_key(db, "old", age_hours=30)
    d = Database(maxconn=1)
    assert cleanup.run_once(d) == 1
    d.close()


def test_migration_converts_text_created_at(db):
    """Databases created before the TTL change have created_at TEXT."""
    with db.cursor() as cur:
        cur.execute("DROP TABLE idempotency_records")
        cur.execute("CREATE TABLE idempotency_records (idem_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,"
                    " response_body TEXT NOT NULL, status_code INTEGER NOT NULL, created_at TEXT NOT NULL)")
        cur.execute("INSERT INTO idempotency_records VALUES ('k', 'h', '{}', 201, '2026-01-02T03:04:05.123456+00:00')")
        cur.execute((ROOT / "init_db.sql").read_text())
        cur.execute("SELECT data_type FROM information_schema.columns"
                    " WHERE table_name='idempotency_records' AND column_name='created_at'")
        assert cur.fetchone()[0] == "timestamp with time zone"
        cur.execute("SELECT created_at = '2026-01-02T03:04:05.123456+00:00'::timestamptz FROM idempotency_records")
        assert cur.fetchone()[0] is True
        cur.execute((ROOT / "init_db.sql").read_text())  # re-running is a no-op
