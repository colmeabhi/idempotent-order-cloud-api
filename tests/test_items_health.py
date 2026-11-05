def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "db": "connected"}


def test_index(client):
    assert "Order API" in client.get("/").text


def test_item_roundtrip(client, db):
    r = client.post("/items", json={"name": "alpha", "value": 123})
    assert r.status_code == 201
    item = client.get(f"/items/{r.json()['id']}").json()
    assert item == {"id": r.json()["id"], "name": "alpha", "value": 123}


def test_item_validation(client, db):
    assert client.post("/items", json={"name": "x"}).status_code == 400
    assert client.get("/items/999").status_code == 404
