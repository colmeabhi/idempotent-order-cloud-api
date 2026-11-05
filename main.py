import asyncio
import hashlib
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psycopg2.extras
from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import cleanup
from db import Database, PoolTimeout
from ratelimit import RateLimitMiddleware

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("orders")

# Pooled connections (DB_POOL_MIN / DB_POOL_MAX / DB_POOL_TIMEOUT); see db.py.
db = Database()


IDEMPOTENCY_TTL = cleanup.ttl_seconds()
CLEANUP_INTERVAL = int(os.getenv("IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS", "3600"))


async def cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            await run_in_threadpool(cleanup.run_once, db)
        except Exception as exc:  # keep the loop alive; try again next interval
            logger.warning(json.dumps({"level": "WARN", "msg": "idempotency_cleanup_failed",
                                       "detail": str(exc)}))


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(cleanup_loop()) if CLEANUP_INTERVAL > 0 else None
    yield
    if task:
        task.cancel()
    db.close()


app = FastAPI(lifespan=lifespan)

# Rate limiting: RATE_LIMIT (default 10/minute per client IP), shared across
# tasks via RATE_LIMIT_STORAGE_URI=redis://... See ratelimit.py.
app.add_middleware(RateLimitMiddleware)


def log(level, req_id, msg, **kw):
    logger.info(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                             "level": level, "req_id": req_id, "msg": msg, **kw}))


@app.exception_handler(PoolTimeout)
async def pool_timeout_handler(request, exc):
    log("WARN", request.headers.get("X-Request-ID"), "db_pool_exhausted", detail=str(exc))
    return JSONResponse({"error": "Service busy, retry"}, status_code=503,
                        headers={"Retry-After": "1"})


@app.get("/health")
def health():
    try:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
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

def _insert_item(name, value):
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO items (name, value) VALUES (%s, %s) RETURNING id", (name, value))
        item_id = cur.fetchone()[0]
        conn.commit()
        return item_id


@app.post("/items", status_code=201)
async def create_item(request: Request):
    data = await request.json()
    if "name" not in data or "value" not in data:
        return JSONResponse({"error": "Missing name or value"}, status_code=400)
    try:
        # psycopg2 is blocking: run it in the threadpool, not on the event loop.
        item_id = await run_in_threadpool(_insert_item, data["name"], data["value"])
    except PoolTimeout:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"id": item_id, "name": data["name"], "value": data["value"]}, status_code=201)


@app.get("/items/{item_id}")
def get_item(item_id: int):
    with db.connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM items WHERE id = %s", (item_id,))
        item = cur.fetchone()
    if not item:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return dict(item)


# ── Orders (idempotent) ────────────────────────────────────

def _create_order_tx(idempotency_key, body_hash, data):
    """Returns (status_code, response_dict, outcome) where outcome is
    'created', 'replay' or 'conflict'. Runs in a worker thread."""
    with db.connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        order_id = str(uuid.uuid4())
        resp = {"order_id": order_id, "status": "created"}

        # Claim the key before doing any work. If another transaction holds the
        # same key, this INSERT blocks on the unique index until that transaction
        # finishes; then it either conflicts (other one committed -> replay) or
        # succeeds (other one rolled back -> we own the key). No SELECT-then-INSERT
        # window, so two parallel requests can't both get past the check.
        #
        # A key older than the TTL counts as unused: the DO UPDATE ... WHERE takes
        # it over, so expiry is exact even if the cleanup job hasn't run yet.
        cur.execute(
            "INSERT INTO idempotency_records (idem_key, request_hash, response_body, status_code, created_at) "
            "VALUES (%s,%s,%s,%s,now()) "
            "ON CONFLICT (idem_key) DO UPDATE SET request_hash = EXCLUDED.request_hash, "
            "  response_body = EXCLUDED.response_body, status_code = EXCLUDED.status_code, "
            "  created_at = EXCLUDED.created_at "
            "WHERE idempotency_records.created_at < now() - make_interval(secs => %s) "
            "RETURNING idem_key",
            (idempotency_key, body_hash, json.dumps(resp), 201, IDEMPOTENCY_TTL))

        if cur.fetchone() is None:
            # Live key already committed by an earlier (or concurrent) request.
            conn.rollback()
            cur.execute("SELECT * FROM idempotency_records WHERE idem_key = %s", (idempotency_key,))
            record = cur.fetchone()
            if record["request_hash"] != body_hash:
                return 409, {"error": "Same key, different payload"}, "conflict"
            return record["status_code"], json.loads(record["response_body"]), "replay"

        cur.execute("INSERT INTO orders (order_id, customer_id, item_id, quantity) VALUES (%s,%s,%s,%s)",
                    (order_id, data["customer_id"], data["item_id"], data["quantity"]))
        cur.execute("INSERT INTO ledger (ledger_id, order_id, customer_id, amount) VALUES (%s,%s,%s,%s)",
                    (str(uuid.uuid4()), order_id, data["customer_id"], 9.99 * data["quantity"]))
        conn.commit()
        return 201, resp, "created"


@app.post("/orders", status_code=201)
async def create_order(request: Request,
                       idempotency_key: str = Header(None, alias="Idempotency-Key")):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

    if not idempotency_key:
        return JSONResponse({"error": "Idempotency-Key header required"}, status_code=400)

    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    try:
        data = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "Body must be valid JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    for field in ("customer_id", "item_id", "quantity"):
        if field not in data:
            return JSONResponse({"error": f"Missing field: {field}"}, status_code=400)

    try:
        status, resp, outcome = await run_in_threadpool(
            _create_order_tx, idempotency_key, body_hash, data)
    except PoolTimeout:
        raise
    except Exception as e:
        log("ERROR", req_id, "error", detail=str(e))
        return JSONResponse({"error": "Internal server error"}, status_code=500)

    if outcome == "conflict":
        log("WARN", req_id, "conflict", idem_key=idempotency_key)
    elif outcome == "replay":
        log("INFO", req_id, "replay", idem_key=idempotency_key)
    else:
        log("INFO", req_id, "created", order_id=resp["order_id"])
        if request.headers.get("X-Debug-Fail-After-Commit") == "true":
            log("WARN", req_id, "simulated_fail", order_id=resp["order_id"])
            return JSONResponse({"error": "Simulated failure"}, status_code=500)

    return JSONResponse(resp, status_code=status)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    with db.connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
    if not order:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return dict(order)
