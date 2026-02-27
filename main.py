import hashlib
import json
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

# ── Structured JSON Logging ──────────────────────────────────────────────────

logger = logging.getLogger("order_service")
logger.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)


def log(level: str, request_id: str, message: str, **extra):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "request_id": request_id,
        "message": message,
        **extra,
    }
    logger.info(json.dumps(entry))


# ── App & DB ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Serverless Order API")
DB = "orders.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            item_id     TEXT NOT NULL,
            quantity    INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'created',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ledger (
            ledger_id   TEXT PRIMARY KEY,
            order_id    TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            amount      REAL NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );

        CREATE TABLE IF NOT EXISTS idempotency_records (
            idem_key      TEXT PRIMARY KEY,
            request_hash  TEXT NOT NULL,
            response_body TEXT NOT NULL,
            status_code   INTEGER NOT NULL,
            created_at    TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()


init_db()


# ── Middleware: attach request ID ────────────────────────────────────────────

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    log("INFO", request_id, "request_started",
        method=request.method, path=request.url.path)

    response = await call_next(request)

    response.headers["X-Request-ID"] = request_id

    log("INFO", request_id, "request_completed",
        method=request.method, path=request.url.path,
        status_code=response.status_code)

    return response


# ── Helpers ──────────────────────────────────────────────────────────────────

UNIT_PRICE = 9.99


def validate_order_payload(data: dict) -> str | None:
    """Return an error message if the payload is invalid, else None."""
    for field in ("customer_id", "item_id", "quantity"):
        if field not in data:
            return f"Missing required field: {field}"
    if not isinstance(data["quantity"], int) or data["quantity"] < 1:
        return "quantity must be a positive integer"
    return None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.post("/orders", status_code=201)
async def create_order(
    request: Request,
    idempotency_key: str = Header(None, alias="Idempotency-Key"),
):
    req_id = request.state.request_id

    # ── Validate header ──────────────────────────────────────────────────
    if not idempotency_key:
        log("WARN", req_id, "missing_idempotency_key")
        return JSONResponse(
            {"error": "Idempotency-Key header is required"}, status_code=400
        )

    # ── Parse & validate body ────────────────────────────────────────────
    try:
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()
        data = json.loads(body)
    except (json.JSONDecodeError, Exception):
        log("WARN", req_id, "invalid_json")
        return JSONResponse(
            {"error": "Request body must be valid JSON"}, status_code=400
        )

    validation_error = validate_order_payload(data)
    if validation_error:
        log("WARN", req_id, "validation_failed", detail=validation_error)
        return JSONResponse({"error": validation_error}, status_code=400)

    # ── DB operations ────────────────────────────────────────────────────
    db = get_db()
    try:
        # Check idempotency records
        record = db.execute(
            "SELECT * FROM idempotency_records WHERE idem_key = ?",
            (idempotency_key,),
        ).fetchone()

        if record:
            # Same key, different payload → 409
            if record["request_hash"] != body_hash:
                log("WARN", req_id, "idempotency_conflict", idem_key=idempotency_key)
                return JSONResponse(
                    {"error": "Idempotency-Key reused with a different payload"},
                    status_code=409,
                )

            # Same key, same payload → replay stored response
            log("INFO", req_id, "idempotent_replay", idem_key=idempotency_key)
            return JSONResponse(
                json.loads(record["response_body"]),
                status_code=record["status_code"],
            )

        # ── Create order + ledger + idempotency record (single transaction) ─
        now = datetime.now(timezone.utc).isoformat()
        order_id = str(uuid.uuid4())
        response_body = json.dumps({"order_id": order_id, "status": "created"})

        db.execute(
            "INSERT INTO orders (order_id, customer_id, item_id, quantity, status, created_at) "
            "VALUES (?, ?, ?, ?, 'created', ?)",
            (order_id, data["customer_id"], data["item_id"], data["quantity"], now),
        )
        db.execute(
            "INSERT INTO ledger (ledger_id, order_id, customer_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), order_id, data["customer_id"],
             UNIT_PRICE * data["quantity"], now),
        )
        db.execute(
            "INSERT INTO idempotency_records (idem_key, request_hash, response_body, status_code, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, body_hash, response_body, 201, now),
        )
        db.commit()  # ← single atomic commit for all three rows

        log("INFO", req_id, "order_created",
            order_id=order_id, idem_key=idempotency_key)

        # ── Simulate "commit succeeded, response lost" ──────────────────
        if request.headers.get("X-Debug-Fail-After-Commit", "").lower() == "true":
            log("WARN", req_id, "simulated_failure_after_commit",
                order_id=order_id, idem_key=idempotency_key)
            return JSONResponse(
                {"error": "Simulated failure after commit"}, status_code=500
            )

        return JSONResponse(
            {"order_id": order_id, "status": "created"}, status_code=201
        )

    except Exception as exc:
        log("ERROR", req_id, "unhandled_exception", detail=str(exc))
        return JSONResponse(
            {"error": "Internal server error"}, status_code=500
        )
    finally:
        db.close()


@app.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    req_id = request.state.request_id
    db = get_db()
    try:
        order = db.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not order:
            log("WARN", req_id, "order_not_found", order_id=order_id)
            return JSONResponse({"error": "Order not found"}, status_code=404)

        log("INFO", req_id, "order_retrieved", order_id=order_id)
        return dict(order)
    except Exception as exc:
        log("ERROR", req_id, "unhandled_exception", detail=str(exc))
        return JSONResponse({"error": "Internal server error"}, status_code=500)
    finally:
        db.close()
