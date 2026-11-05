import json

from tests.conftest import count

ORDER = {"customer_id": "c1", "item_id": "i1", "quantity": 2}


def post_order(client, key, body=ORDER, **headers):
    hdrs = {"Idempotency-Key": key, **headers} if key else headers
    return client.post("/orders", json=body, headers=hdrs)


def test_create_order(client, db):
    r = post_order(client, "k1")
    assert r.status_code == 201
    assert r.json()["status"] == "created"
    assert count(db, "orders") == 1
    assert count(db, "ledger") == 1


def test_ledger_amount(client, db):
    order_id = post_order(client, "k1").json()["order_id"]
    with db.cursor() as cur:
        cur.execute("SELECT amount FROM ledger WHERE order_id = %s", (order_id,))
        assert abs(cur.fetchone()[0] - 9.99 * 2) < 1e-6


def test_replay_returns_same_response(client, db):
    first = post_order(client, "k1")
    second = post_order(client, "k1")
    assert second.status_code == 201
    assert second.json() == first.json()
    assert count(db, "orders") == 1
    assert count(db, "ledger") == 1


def test_same_key_different_payload_conflicts(client, db):
    post_order(client, "k1")
    r = post_order(client, "k1", {**ORDER, "quantity": 5})
    assert r.status_code == 409
    assert count(db, "orders") == 1


def test_different_keys_create_different_orders(client, db):
    a = post_order(client, "k1").json()["order_id"]
    b = post_order(client, "k2").json()["order_id"]
    assert a != b
    assert count(db, "orders") == 2


def test_missing_idempotency_key(client, db):
    assert post_order(client, None).status_code == 400
    assert count(db, "orders") == 0


def test_missing_field(client, db):
    r = post_order(client, "k1", {"customer_id": "c1", "item_id": "i1"})
    assert r.status_code == 400
    assert "quantity" in r.json()["error"]


def test_invalid_json(client, db):
    r = client.post("/orders", content=b"{not json", headers={"Idempotency-Key": "k1"})
    assert r.status_code == 400
    r = client.post("/orders", content=b"[1, 2]", headers={"Idempotency-Key": "k1"})
    assert r.status_code == 400


def test_retry_after_failure_after_commit_replays(client, db):
    failed = post_order(client, "k1", **{"X-Debug-Fail-After-Commit": "true"})
    assert failed.status_code == 500
    retry = post_order(client, "k1")
    assert retry.status_code == 201
    assert count(db, "orders") == 1
    with db.cursor() as cur:
        cur.execute("SELECT order_id FROM orders")
        assert retry.json()["order_id"] == cur.fetchone()[0]


def test_get_order(client, db):
    order_id = post_order(client, "k1").json()["order_id"]
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200
    assert r.json()["customer_id"] == "c1"
    assert client.get("/orders/does-not-exist").status_code == 404


def test_stored_response_matches(client, db):
    r = post_order(client, "k1")
    with db.cursor() as cur:
        cur.execute("SELECT response_body, status_code FROM idempotency_records WHERE idem_key='k1'")
        body, status = cur.fetchone()
    assert json.loads(body) == r.json()
    assert status == 201
