# Idempotent Order API

Retry-safe order API with application-level idempotency for exactly-once semantics. Built with FastAPI and SQLite.

## Infrastructure

| Resource | Detail |
|----------|--------|
| EC2 Instance | `t2.nano` (`i-07e0f5eaca7e8ab55`) |
| Region | `us-west-2a` |
| OS | Ubuntu 24 (Linux) |
| Public IP | `<YOUR_EC2_PUBLIC_IP>` |
| App Port | `8000` |
| Database | SQLite (`orders.db` on instance) |

### Security Group (`launch-wizard-1` / `sg-00ba338fca4612ba6`)

**Inbound Rules:**

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 22 | TCP | `<YOUR_IP>/32` | SSH |
| 8000 | TCP | 0.0.0.0/0 | API |

**Outbound:** All traffic allowed.

## Deploy & Run

```bash
# SSH into instance
ssh -i <your-key.pem> ubuntu@<YOUR_EC2_PUBLIC_IP>

# Clone repo
git clone <repo-url>
cd idempotent-order-cloud-api

# Setup
sudo apt install python3.12-venv -y
python3 -m venv venv
source venv/bin/activate
pip install "fastapi[standard]"

# Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

Base URL: `http://<YOUR_EC2_PUBLIC_IP>:8000`

## API Endpoints

### `POST /orders`
Creates an order. Requires `Idempotency-Key` header.

### `GET /orders/{order_id}`
Retrieves an order by ID.

## Verification Steps

**Step 1 — Create an order:**
```bash
curl -X POST http://<YOUR_EC2_PUBLIC_IP>:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```

**Step 2 — Retry with same key (idempotent replay):**
```bash
curl -X POST http://<YOUR_EC2_PUBLIC_IP>:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```

**Step 3 — Same key, different payload (409 Conflict):**
```bash
curl -X POST http://<YOUR_EC2_PUBLIC_IP>:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":5}'
```

**Step 4 — Simulate failure after commit:**
```bash
curl -X POST http://<YOUR_EC2_PUBLIC_IP>:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -H "X-Debug-Fail-After-Commit: true" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```

**Step 5 — Retry after failure (returns stored response):**
```bash
curl -X POST http://<YOUR_EC2_PUBLIC_IP>:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```

**Step 6 — Verify order exists:**
```bash
curl http://<YOUR_EC2_PUBLIC_IP>:8000/orders/<order_id>
```
