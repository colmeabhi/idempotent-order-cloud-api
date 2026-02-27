import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI()
DB = "orders.db"

# Structured JSON logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("orders")


def log(level, req_id, msg, **kw):
    logger.info(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                             "level": level, "req_id": req_id, "msg": msg, **kw}))


# DB setup
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY, customer_id TEXT, item_id TEXT,
            quantity INTEGER, status TEXT DEFAULT 'created'
        );
        CREATE TABLE IF NOT EXISTS ledger (
            ledger_id TEXT PRIMARY KEY, order_id TEXT, customer_id TEXT, amount REAL
        );
        CREATE TABLE IF NOT EXISTS idempotency_records (
            idem_key TEXT PRIMARY KEY, request_hash TEXT,
            response_body TEXT, status_code INTEGER, created_at TEXT
        );
    """)
    db.commit()
    db.close()


init_db()


@app.post("/orders", status_code=201)
async def create_order(request: Request,
                       idempotency_key: str = Header(None, alias="Idempotency-Key")):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

    if not idempotency_key:
        return JSONResponse({"error": "Idempotency-Key header required"}, status_code=400)

    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    data = json.loads(body)

    # Basic validation
    for field in ("customer_id", "item_id", "quantity"):
        if field not in data:
            return JSONResponse({"error": f"Missing field: {field}"}, status_code=400)

    db = get_db()
    try:
        record = db.execute(
            "SELECT * FROM idempotency_records WHERE idem_key = ?", (idempotency_key,)
        ).fetchone()

        if record:
            if record["request_hash"] != body_hash:
                log("WARN", req_id, "conflict", idem_key=idempotency_key)
                return JSONResponse({"error": "Same key, different payload"}, status_code=409)
            log("INFO", req_id, "replay", idem_key=idempotency_key)
            return JSONResponse(json.loads(record["response_body"]), status_code=record["status_code"])

        # All three writes in a single atomic commit
        order_id = str(uuid.uuid4())
        resp = json.dumps({"order_id": order_id, "status": "created"})

        db.execute("INSERT INTO orders VALUES (?,?,?,?,'created')",
                   (order_id, data["customer_id"], data["item_id"], data["quantity"]))
        db.execute("INSERT INTO ledger VALUES (?,?,?,?)",
                   (str(uuid.uuid4()), order_id, data["customer_id"], 9.99 * data["quantity"]))
        db.execute("INSERT INTO idempotency_records VALUES (?,?,?,?,?)",
                   (idempotency_key, body_hash, resp, 201, datetime.now(timezone.utc).isoformat()))
        db.commit()

        log("INFO", req_id, "created", order_id=order_id)

        # Simulate failure after commit
        if request.headers.get("X-Debug-Fail-After-Commit") == "true":
            log("WARN", req_id, "simulated_fail", order_id=order_id)
            return JSONResponse({"error": "Simulated failure"}, status_code=500)

        return JSONResponse({"order_id": order_id, "status": "created"}, status_code=201)
    except Exception as e:
        log("ERROR", req_id, "error", detail=str(e))
        return JSONResponse({"error": "Internal server error"}, status_code=500)
    finally:
        db.close()


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    db = get_db()
    try:
        order = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not order:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return dict(order)
    finally:
        db.close()
