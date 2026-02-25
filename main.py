import hashlib
import json
import sqlite3
import uuid

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI()
DB = "orders.db"

# --- DB Setup ---


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    TEXT PRIMARY KEY,
            customer_id TEXT,
            item_id     TEXT,
            quantity    INTEGER,
            status      TEXT DEFAULT 'created'
        );
        CREATE TABLE IF NOT EXISTS ledger (
            ledger_id   TEXT PRIMARY KEY,
            order_id    TEXT,
            customer_id TEXT,
            amount      REAL
        );
        CREATE TABLE IF NOT EXISTS idempotency_records (
            idem_key      TEXT PRIMARY KEY,
            request_hash  TEXT,
            response_body TEXT,
            status_code   INTEGER
        );
    """)
    db.commit()
    db.close()


init_db()

# --- Routes ---


@app.post("/orders", status_code=201)
async def create_order(request: Request, idempotency_key: str = Header(None)):
    if not idempotency_key:
        return JSONResponse(
            {"error": "Idempotency-Key header required"}, status_code=400
        )

    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    data = json.loads(body)

    db = get_db()

    # Check if we've seen this key before
    record = db.execute(
        "SELECT * FROM idempotency_records WHERE idem_key = ?", (idempotency_key,)
    ).fetchone()
    if record:
        if record["request_hash"] != body_hash:
            return JSONResponse(
                {"error": "Same key used with different payload"}, status_code=409
            )
        # Replay the original response
        return JSONResponse(
            json.loads(record["response_body"]), status_code=record["status_code"]
        )

    # Create order + ledger entry
    order_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, 'created')",
        (order_id, data["customer_id"], data["item_id"], data["quantity"]),
    )
    db.execute(
        "INSERT INTO ledger VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), order_id, data["customer_id"], 9.99 * data["quantity"]),
    )
    db.commit()

    response_body = json.dumps({"order_id": order_id, "status": "created"})

    # Simulate: commit succeeded but response never reached client
    simulate = request.headers.get("X-Debug-Fail-After-Commit") == "true"
    if simulate:
        db.execute(
            "INSERT INTO idempotency_records VALUES (?, ?, ?, ?)",
            (idempotency_key, body_hash, response_body, 201),
        )
        db.commit()
        db.close()
        return JSONResponse({"error": "Simulated lost response"}, status_code=500)

    # Save idempotency record + return response
    db.execute(
        "INSERT INTO idempotency_records VALUES (?, ?, ?, ?)",
        (idempotency_key, body_hash, response_body, 201),
    )
    db.commit()
    db.close()
    return JSONResponse({"order_id": order_id, "status": "created"}, status_code=201)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if not order:
        return JSONResponse({"error": "Order not found"}, status_code=404)
    return dict(order)
