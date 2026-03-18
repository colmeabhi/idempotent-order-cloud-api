import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "orders")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("orders")


def log(level, req_id, msg, **kw):
    logger.info(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                             "level": level, "req_id": req_id, "msg": msg, **kw}))


def get_db():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )
    return conn


@app.get("/health")
def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return {"status": "ok", "db": "connected"}
    except Exception:
        return JSONResponse({"status": "error", "db": "disconnected"}, status_code=503)


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Order API</title>
  <style>
    body { font-family: monospace; max-width: 640px; margin: 60px auto; padding: 0 20px; color: #222; }
    h1 { font-size: 1.4rem; }
    code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }
    pre { background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }
  </style>
</head>
<body>
  <h1>Order API</h1>
  <p>Endpoints: <code>POST /orders</code>, <code>GET /orders/{id}</code>,
     <code>POST /items</code>, <code>GET /items/{id}</code>,
     <code>GET /health</code></p>
  <p>Docs: <a href="/docs">/docs</a></p>
</body>
</html>
"""


# ── Items CRUD ──────────────────────────────────────────────

@app.post("/items", status_code=201)
async def create_item(request: Request):
    data = await request.json()
    if "name" not in data or "value" not in data:
        return JSONResponse({"error": "Missing name or value"}, status_code=400)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO items (name, value) VALUES (%s, %s) RETURNING id",
            (data["name"], data["value"])
        )
        item_id = cur.fetchone()[0]
        conn.commit()
        return JSONResponse({"id": item_id, "name": data["name"], "value": data["value"]}, status_code=201)
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


@app.get("/items/{item_id}")
def get_item(item_id: int):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM items WHERE id = %s", (item_id,))
        item = cur.fetchone()
        if not item:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return dict(item)
    finally:
        conn.close()


# ── Orders (idempotent) ────────────────────────────────────

@app.post("/orders", status_code=201)
async def create_order(request: Request,
                       idempotency_key: str = Header(None, alias="Idempotency-Key")):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

    if not idempotency_key:
        return JSONResponse({"error": "Idempotency-Key header required"}, status_code=400)

    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    data = json.loads(body)

    for field in ("customer_id", "item_id", "quantity"):
        if field not in data:
            return JSONResponse({"error": f"Missing field: {field}"}, status_code=400)

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM idempotency_records WHERE idem_key = %s", (idempotency_key,))
        record = cur.fetchone()

        if record:
            if record["request_hash"] != body_hash:
                log("WARN", req_id, "conflict", idem_key=idempotency_key)
                return JSONResponse({"error": "Same key, different payload"}, status_code=409)
            log("INFO", req_id, "replay", idem_key=idempotency_key)
            return JSONResponse(json.loads(record["response_body"]), status_code=record["status_code"])

        order_id = str(uuid.uuid4())
        resp = json.dumps({"order_id": order_id, "status": "created"})

        cur.execute("INSERT INTO orders (order_id, customer_id, item_id, quantity) VALUES (%s,%s,%s,%s)",
                    (order_id, data["customer_id"], data["item_id"], data["quantity"]))
        cur.execute("INSERT INTO ledger (ledger_id, order_id, customer_id, amount) VALUES (%s,%s,%s,%s)",
                    (str(uuid.uuid4()), order_id, data["customer_id"], 9.99 * data["quantity"]))
        cur.execute("INSERT INTO idempotency_records (idem_key, request_hash, response_body, status_code, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (idempotency_key, body_hash, resp, 201, datetime.now(timezone.utc).isoformat()))
        conn.commit()

        log("INFO", req_id, "created", order_id=order_id)

        if request.headers.get("X-Debug-Fail-After-Commit") == "true":
            log("WARN", req_id, "simulated_fail", order_id=order_id)
            return JSONResponse({"error": "Simulated failure"}, status_code=500)

        return JSONResponse({"order_id": order_id, "status": "created"}, status_code=201)
    except Exception as e:
        conn.rollback()
        log("ERROR", req_id, "error", detail=str(e))
        return JSONResponse({"error": "Internal server error"}, status_code=500)
    finally:
        conn.close()


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return dict(order)
    finally:
        conn.close()