# idempotent-order-cloud-api

Retry-safe order API with application-level idempotency for exactly-once semantics in distributed systems.

## Overview

A FastAPI service that accepts order creation requests and guarantees exactly-once processing using an `Idempotency-Key` header. Duplicate requests with the same key and payload replay the original response; duplicate requests with the same key but a different payload are rejected with a `409 Conflict`.

Order data is persisted in a local SQLite database (`orders.db`).

## Requirements

- Python 3.12+

## Setup

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install fastapi uvicorn
```

## Running the Server

```bash
uvicorn main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

Interactive docs: `http://127.0.0.1:8000/docs`

## API Endpoints

### `POST /orders`

Creates a new order. Requires the `Idempotency-Key` header.

**Request headers:**
- `Idempotency-Key` (required) — a unique string per request (e.g. a UUID)
- `Content-Type: application/json`

**Request body:**
```json
{
  "customer_id": "cust1",
  "item_id": "item1",
  "quantity": 2
}
```

**Response (201):**
```json
{
  "order_id": "<uuid>",
  "status": "created"
}
```

---

### `GET /orders/{order_id}`

Retrieves an order by ID.

**Response (200):**
```json
{
  "order_id": "<uuid>",
  "customer_id": "cust1",
  "item_id": "item1",
  "quantity": 2,
  "status": "created",
  "created_at": "2026-02-26T00:00:00+00:00"
}
```

## Example curl Commands

**Create an order:**
```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```

**Replay (same key + same payload → returns original response):**
```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```

**Conflict (same key, different payload → 409):**
```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":5}'
```

**Simulate failure after commit (for testing retry logic):**
```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -H "X-Debug-Fail-After-Commit: true" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```

**Retry after simulated failure (returns the stored response):**
```bash
curl -X POST http://127.0.0.1:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```

**Retrieve an order:**
```bash
curl http://127.0.0.1:8000/orders/<order_id>
```
