# Idempotent Order Cloud API

FastAPI service with Postgres persistence and a Redis-backed rate limiter,
containerized with Docker and deployed to AWS (ALB → ECS Fargate → RDS) with
Terraform and GitHub Actions.

```
Client → ALB (public) → ECS Fargate (API, 2 tasks) → RDS Postgres
                                  └──────────────→ ElastiCache Redis (rate limits)
```

## Local Setup

### Prerequisites
- Docker & Docker Compose

### Run Locally

```bash
docker compose up -d --build
```

This starts Postgres, Redis and the API at `http://localhost:8080`.

### Health Check

```bash
curl -i http://localhost:8080/health
```

### Database Migrations

`start.sh` runs `init_db.sql` on every container start. The script is
idempotent and takes an advisory lock, so several tasks booting at once are
safe. It also migrates older databases (for example `idempotency_records.created_at`
from `TEXT` to `TIMESTAMPTZ`).

### Test Persistence

```bash
# Create an item
curl -s -X POST http://localhost:8080/items \
  -H "Content-Type: application/json" \
  -d '{"name":"alpha","value":123}'

# Restart API — data persists
docker compose restart api
curl -s http://localhost:8080/items/1

# Restart Postgres — data persists (volume-backed)
docker compose restart postgres
sleep 3
curl -s http://localhost:8080/items/1
```

### Stop

```bash
docker compose down
```

To also remove the Postgres volume:

```bash
docker compose down -v
```

## Idempotent Orders

`POST /orders` requires an `Idempotency-Key` header. The first request with a
key creates the order and a ledger entry and stores the response. Any retry
with the same key and body gets the stored response back with no new order.
The same key with a different body gets `409`.

```bash
curl -X POST http://localhost:8080/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```

### Concurrent requests with the same key

The handler used to `SELECT` the key and then `INSERT` it. Two parallel
requests could both see "no key", and the slower one then hit the primary-key
violation and returned **500**. With 16 workers, 7 of 20 rounds of 50 parallel
requests produced a 500.

Now the request **claims the key first**, in the same transaction as the order:

```sql
INSERT INTO idempotency_records (...) VALUES (...)
ON CONFLICT (idem_key) DO UPDATE ... WHERE created_at < now() - <ttl>   -- only expired keys
RETURNING idem_key
```

```mermaid
sequenceDiagram
    participant A as Request A
    participant B as Request B (same key)
    participant DB as Postgres
    A->>DB: BEGIN; INSERT key ON CONFLICT ... RETURNING
    DB-->>A: 1 row (A owns the key)
    B->>DB: BEGIN; INSERT key ON CONFLICT ... RETURNING
    Note over B,DB: blocks on the unique index<br/>until A's transaction ends
    A->>DB: INSERT order, INSERT ledger, COMMIT
    DB-->>B: 0 rows (conflict with committed key)
    B->>DB: SELECT stored response
    DB-->>B: {order_id: A's order}
    Note over A,B: both return 201 with the same order_id
```

If A rolls back instead, B's insert succeeds and B creates the order. There is
no window in which both can create one.

### Key expiry (TTL)

Keys live for `IDEMPOTENCY_TTL_HOURS` (default 24h), long enough to cover client
retries:

- **At request time:** the claim above takes over a key older than the TTL,
  so expiry is exact even if cleanup hasn't run.
- **Cleanup:** every `IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS` (default 1h) the API
  deletes expired keys in batches of 1000. A Postgres advisory lock means only
  one ECS task purges per cycle. Run it by hand with `python cleanup.py`.
  Orders and ledger rows are never deleted.

### Simulated failure after commit

```bash
curl -X POST http://localhost:8080/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -H "X-Debug-Fail-After-Commit: true" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```

This returns 500 even though the order was committed. Retrying with the same key
and without the header returns 201 with the original `order_id`.

## Rate Limiting

Sliding-window limit of **10 requests per minute per client IP**
(`RATE_LIMIT`), implemented as middleware in `ratelimit.py`.

- **Shared across tasks:** counters live in Redis (`RATE_LIMIT_STORAGE_URI`).
  The old in-memory store let each ECS task count separately, so 2 tasks
  allowed 20/min. `memory://` is still the default for running without Redis.
- **Client IP:** the last entry of `X-Forwarded-For`, which the ALB appends.
  Earlier entries are client-controlled and are ignored.
- **`/health` is exempt**, so ALB and ECS health checks are never throttled.
- Over the limit you get `429` with a `Retry-After` header.
- If Redis is unreachable the limiter **fails open** (requests are allowed and
  a warning is logged) instead of taking the API down.

### Test Rate Limiting

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "request $i: %{http_code}\n" http://localhost:8080/items/1
done
```

Requests 1–10 return `200`; 11 and 12 return `429`.

## Connection Pooling

`db.py` keeps a pool of Postgres connections per process (`DB_POOL_MAX`,
default 10) instead of opening one per request. Details that matter:

- psycopg2's pool **raises** when exhausted. A semaphore makes callers wait up
  to `DB_POOL_TIMEOUT` seconds, after which the API returns `503` with `Retry-After`.
- psycopg2 **closes** any returned connection above `minconn`. With the usual
  `minconn=1`, a burst of traffic reconnected on nearly every request while
  holding the pool's global lock, which was *slower* than no pool (≈90 vs
  ≈180 req/s). `DB_POOL_MIN` therefore defaults to `DB_POOL_MAX`.
- Connections that die (Postgres restart, RDS failover) are discarded instead
  of being handed out again. A connection idle for more than
  `DB_POOL_PING_AFTER` seconds is checked before use and replaced if dead, so a
  restart doesn't cost a client a `500` for each pooled connection. The check
  runs before any application SQL, so it can't repeat a write.
- Blocking DB calls run in the threadpool. Previously the `async` handlers
  called psycopg2 directly and blocked the event loop.

Sizing: `desired_count × 2 (deploy overlap) × DB_POOL_MAX` must stay below RDS
`max_connections` (about 80 on `db.t3.micro`). The defaults give 2 × 2 × 10 = 40.

## Load Test

`loadtest/orders.js` exercises the write path. Each iteration creates an order
with a new key, replays it (checking that the same `order_id` comes back), then
`GET`s it. `loadtest/items.js` is the original `/health` + `/items` test.

Disable the limiter, or every request after the 10th is a `429`:

```bash
RATE_LIMIT_ENABLED=false docker compose up -d --build
```

The original profile:

```bash
k6 run loadtest/orders.js
```

The saturation run:

```bash
k6 run -e VUS=50 -e SLEEP=0 loadtest/orders.js
```

Against the ALB:

```bash
k6 run -e BASE_URL=http://<alb-dns> loadtest/orders.js
```

### Results: `/orders`, before vs after pooling

Measured on an Apple M4 (10 cores, 16 GB) with k6 2.3 and the API, Postgres 16
and k6 all running natively on the same machine. One uvicorn worker (like one
Fargate task), limiter off, 30 s per run. Each figure is the median of 3 runs,
with 0% errors and 100% checks passed in every run. "Before" is commit
`9e24a6b`, the last one without pooling, measured on the same machine.

**10 VUs, 0.1 s think time** (same shape as the original test)

| | Before | Threadpool only | Threadpool + pool |
|---|---|---|---|
| RPS | 146 | 231 | **246** |
| p50 | 36.6 ms | 8.3 ms | **7.0 ms** |
| p95 | 62.8 ms | 16.8 ms | **12.5 ms** |
| p99 | 86.4 ms | 23.9 ms | **22.0 ms** |

**50 VUs, no think time** (saturation)

| | Before | Threadpool only | Threadpool + pool |
|---|---|---|---|
| RPS | 303 | 544 | **3,558** |
| p50 | 160.8 ms | 88.9 ms | **13.4 ms** |
| p95 | 191.4 ms | 148.5 ms | **22.4 ms** |
| p99 | 259.7 ms | 184.3 ms | **32.7 ms** |

- **Before:** a new connection per request, and the async handler blocked the
  event loop, so requests ran one at a time.
- **Threadpool only** (`DB_POOL_MAX=0`): requests overlap but still pay for
  the connect each time.
- **Threadpool + pool:** 1.7× the RPS and 5× lower p95 at the original
  profile; 11.7× the RPS and 8.6× lower p95 at saturation. The light profile
  says little about throughput: 10 VUs with 0.1 s sleep cap it at about 260 RPS,
  so 246 is close to that ceiling. Its latency figures are the useful part.

**Read these as a comparison, not as production capacity:**

- macOS Postgres uses `wal_sync_method=open_datasync`, which doesn't force a
  full flush to the drive, so commits are much cheaper than on a Linux server.
  An earlier run of this test on a different host peaked at 570 RPS.
- There is no network between the API and the database. Against RDS each query
  adds a round trip, and each new connection also pays for TLS, so the absolute
  numbers will be lower.
- k6 shares the CPU with the API and Postgres.

The liveness check on idle connections costs nothing measurable here: 3,558 RPS
with it and 3,599 without. It only runs on connections idle for over 1 s, which
never happens at saturation.

The earlier results (`/health` + `/items`, connection per request, before
the rate limiter was added) were 253 RPS with p95 13.3 ms, on a different
host and endpoint mix, so they aren't directly comparable.

## Tests

The tests need Postgres, plus Redis for the shared-limit test, which is
skipped if Redis isn't reachable. Start both:

```bash
docker compose up -d postgres redis
```

Install the dev dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the suite:

```bash
python -m pytest -v
```

| File | Covers |
|---|---|
| `tests/test_concurrency.py` | **50 parallel requests with one key → 50 × 201, same `order_id`, exactly 1 order and 1 ledger row**, against an 8-worker uvicorn. Also a deterministic version that holds the key in an open transaction. The old code fails all three. |
| `tests/test_orders.py` | Create, replay, 409 on a different payload, validation, retry after a failure after commit |
| `tests/test_ttl.py` | Expired key taken over, live key still 409, batched purge, advisory-lock skip, TEXT→TIMESTAMPTZ migration |
| `tests/test_db_pool.py` | Reuse, rollback on return, dead-connection eviction, **API keeps working after every pooled connection dies**, wait-then-timeout, no reconnects under bursts |
| `tests/test_rate_limit.py` | Limit, `Retry-After`, `/health` exempt, `X-Forwarded-For`, memory = per instance, **Redis = shared across instances**, fail-open |

All 41 tests pass, both against the Compose Postgres and Redis and against
native Postgres 16 and Redis.

**CI** (`.github/workflows/ci.yml`) runs on every PR and every branch push. It
runs pytest against Postgres 16 and Redis 7 service containers, builds the
Docker image, and runs `terraform fmt`/`validate`.

## Configuration

| Variable | Default | |
|---|---|---|
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `localhost` / `5432` / `orders` / `postgres` / `postgres` | |
| `DB_POOL_MAX` | `10` | Connections per process; `0` = no pooling |
| `DB_POOL_MIN` | `= DB_POOL_MAX` | See Connection Pooling |
| `DB_POOL_TIMEOUT` | `5` | Seconds to wait for a free connection before returning 503 |
| `DB_POOL_PING_AFTER` | `1` | Check a connection before use if it has been idle this many seconds |
| `RATE_LIMIT` | `10/minute` | Any [limits](https://limits.readthedocs.io) string, e.g. `100/hour` |
| `RATE_LIMIT_STORAGE_URI` | `memory://` | `redis://host:6379/0` to share limits across tasks |
| `RATE_LIMIT_ENABLED` | `true` | |
| `IDEMPOTENCY_TTL_HOURS` | `24` | |
| `IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS` | `3600` | `0` disables the in-app purge |

## Secrets Handling

- **Local:** environment variables are set in `docker-compose.yml`.
- **AWS:** Terraform generates the DB password and stores it as an SSM
  `SecureString` at `/order-api/db-password`. ECS injects it into the
  container through the task definition's `secrets`, so it never appears in
  the task definition, the image or GitHub. It does sit in Terraform state,
  which is why the state bucket is private, encrypted and versioned.
- **CI/CD:** GitHub Actions assumes an IAM role over OIDC, so no AWS keys are
  stored in GitHub.

## AWS Deployment (Terraform + GitHub Actions)

### What gets created

| File | Resources |
|---|---|
| `infra/bootstrap/` (once, by hand) | S3 state bucket, DynamoDB lock table, ECR repo, GitHub OIDC provider and deploy role |
| `infra/stack/network.tf` | VPC across 2 AZs, public subnets (ALB, tasks), private subnets (RDS, Redis), security groups |
| `infra/stack/rds.tf`, `ssm.tf` | RDS Postgres 16 `db.t3.micro` (encrypted, private, 7-day backups, deletion protection) and the password in SSM |
| `infra/stack/redis.tf` | ElastiCache Redis 7 `cache.t4g.micro` for the rate limiter |
| `infra/stack/alb.tf` | ALB with the `/health` target group; optional HTTPS via `certificate_arn` |
| `infra/stack/ecs.tf` | Cluster, log group, IAM roles, task definition, service (2 tasks, circuit breaker with rollback) |

Tasks run in public subnets with a public IP so they can reach ECR, SSM and
CloudWatch without a NAT gateway (about $32/month). Their security group
accepts traffic only from the ALB. For stricter isolation, move them to the
private subnets and add a NAT gateway or VPC endpoints.

### One-time setup

1. **Bootstrap**, with admin credentials. Go to the bootstrap module:

   ```bash
   cd infra/bootstrap
   ```

   Initialise it:

   ```bash
   terraform init
   ```

   Apply it with a globally unique bucket name:

   ```bash
   terraform apply -var state_bucket_name=<unique-bucket-name>
   ```

2. In GitHub, create an **environment** named `production` under
   Settings → Environments. Optionally add required reviewers to gate deploys.
3. Add **repository variables** from the bootstrap outputs
   (Settings → Secrets and variables → Actions → Variables):
   `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `TF_STATE_BUCKET`, `TF_LOCK_TABLE`,
   `ECR_REPOSITORY`.

### Deploying

Push to `main`, or run the **Deploy** workflow by hand. `.github/workflows/deploy.yml`:

1. runs the full CI suite;
2. builds the image and pushes `order-api:<git sha>` to ECR;
3. runs `terraform apply -var image_tag=<git sha>` on `infra/stack`, which
   rolls out a new task-definition revision and waits for the service to be
   stable. If the new tasks never become healthy, ECS rolls back and the job fails;
4. curls `/health` through the ALB.

### Running Terraform locally

Initialise the stack against the remote state:

```bash
cd infra/stack
terraform init \
  -backend-config="bucket=<TF_STATE_BUCKET>" \
  -backend-config="key=order-api/terraform.tfstate" \
  -backend-config="region=us-east-2" \
  -backend-config="dynamodb_table=order-api-terraform-lock"
```

Then plan against an image tag that already exists in ECR:

```bash
terraform plan -var image_tag=<existing-tag>
```

To tear down, first set `db_deletion_protection = false` and apply. RDS
takes a final snapshot (`order-api-final`) on destroy.

### Existing manually created stack

The ALB at `http://order-api-alb-1514561568.us-east-2.elb.amazonaws.com`,
ECS service `order-api-service` and RDS instance were created by hand. The
Terraform stack creates its own resources with the same names. Delete or
`terraform import` the manual ones before the first apply to avoid name
clashes and double billing.

## Endpoints

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/health` | Health check (DB connectivity); not rate-limited |
| GET | `/` | HTML landing page |
| POST | `/items` | Create an item |
| GET | `/items/{id}` | Get item by ID |
| POST | `/orders` | Create order (idempotent, requires `Idempotency-Key` header) |
| GET | `/orders/{order_id}` | Get order by ID |
