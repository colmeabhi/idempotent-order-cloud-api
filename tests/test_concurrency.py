"""The same-key race: parallel requests with one Idempotency-Key must produce
exactly one order and one ledger row, and every caller gets the same answer."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.conftest import count

ORDER = {"customer_id": "c1", "item_id": "i1", "quantity": 1}
N = 50


def test_50_parallel_requests_same_key(live_server, db):
    barrier = threading.Barrier(N)

    def send(_):
        with httpx.Client(timeout=30) as c:
            barrier.wait()  # release all 50 at once
            return c.post(live_server + "/orders", json=ORDER,
                          headers={"Idempotency-Key": "race-key"})

    with ThreadPoolExecutor(N) as pool:
        responses = list(pool.map(send, range(N)))

    assert [r.status_code for r in responses] == [201] * N
    assert len({r.json()["order_id"] for r in responses}) == 1
    assert count(db, "orders") == 1
    assert count(db, "ledger") == 1
    assert count(db, "idempotency_records") == 1


def test_50_parallel_requests_same_key_different_payloads(live_server, db):
    """First writer wins; everyone else with a different body gets 409."""
    barrier = threading.Barrier(N)

    def send(i):
        with httpx.Client(timeout=30) as c:
            barrier.wait()
            return c.post(live_server + "/orders", json={**ORDER, "quantity": 1 + i % 2},
                          headers={"Idempotency-Key": "race-key-2"})

    with ThreadPoolExecutor(N) as pool:
        statuses = [r.status_code for r in pool.map(send, range(N))]

    assert set(statuses) <= {201, 409}
    assert count(db, "orders") == 1
    assert count(db, "ledger") == 1


def test_request_waits_for_in_flight_key_then_replays(live_server, db):
    """Deterministic version of the race: another transaction has claimed the
    key but not committed. The request must wait and then replay, not 500.
    (The old SELECT-then-INSERT code fails this every time.)"""
    from tests.conftest import db_connect
    import hashlib, json

    body = json.dumps(ORDER).encode()
    holder = db_connect()  # not autocommit: keeps the transaction open
    with holder.cursor() as cur:
        cur.execute(
            "INSERT INTO idempotency_records (idem_key, request_hash, response_body, status_code, created_at)"
            " VALUES (%s, %s, %s, 201, now())",
            ("held-key", hashlib.sha256(body).hexdigest(), '{"order_id": "held", "status": "created"}'))

    result = {}

    def send():
        result["r"] = httpx.post(live_server + "/orders", content=body, timeout=30,
                                 headers={"Idempotency-Key": "held-key",
                                          "Content-Type": "application/json"})

    t = threading.Thread(target=send)
    t.start()
    t.join(timeout=1.0)
    assert t.is_alive(), "request should be blocked behind the in-flight claim"
    holder.commit()
    holder.close()
    t.join(timeout=10)

    assert result["r"].status_code == 201
    assert result["r"].json() == {"order_id": "held", "status": "created"}
    assert count(db, "orders") == 0  # the holder's (fake) order; no second one was created
