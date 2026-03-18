# Idempotent Order Cloud API

FastAPI service with Postgres persistence, containerized with Docker.

## Local Setup

### Prerequisites
- Docker & Docker Compose

### Run Locally

```bash
docker compose up -d --build
```

The API will be available at `http://localhost:8080`.

### Health Check

```bash
curl -i http://localhost:8080/health
```

### Database Migrations

Tables are created automatically on container startup via `init_db.sql` run by `start.sh`. No manual migration step needed.

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

### Create an Order (idempotent)

```bash
curl -X POST http://localhost:8080/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```

### Stop

```bash
docker compose down
# To also remove the Postgres volume:
docker compose down -v
```

## Secrets Handling

- **Local:** Environment variables are set in `docker-compose.yml`. For custom values, create a `.env` file (gitignored) and reference it with `env_file` in compose.
- **AWS:** Non-secret env vars are set in the ECS Task Definition `environment` block. The DB password is stored in AWS SSM Parameter Store and injected into the task definition via `secrets` with `valueFrom` pointing to the SSM parameter ARN.

## AWS Deployment

### Architecture

```
Client → ALB (public) → ECS Fargate (API) → RDS Postgres
```

### AWS Components Used

- **ECS Fargate** — runs the API container (0.25 vCPU, 0.5 GB)
- **RDS Postgres** — `db.t3.micro`, single-AZ
- **Application Load Balancer** — public-facing
- **CloudWatch Logs** — enabled on ECS task
- **SSM Parameter Store** — stores DB password

### Deployment Steps

1. **Push image to ECR:**
   ```bash
   aws ecr create-repository --repository-name order-api
   aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
   docker build -t order-api .
   docker tag order-api:latest <account>.dkr.ecr.<region>.amazonaws.com/order-api:latest
   docker push <account>.dkr.ecr.<region>.amazonaws.com/order-api:latest
   ```

2. **Create RDS Postgres instance** (`db.t3.micro`, single-AZ, public or VPC-accessible to ECS)

3. **Store DB password in SSM:**
   ```bash
   aws ssm put-parameter --name /order-api/db-password --value "<password>" --type SecureString
   ```

4. **Create ECS cluster, task definition, service, ALB, and target group** — see `ecs-task-def.json` for the task definition. The ALB target group health check is configured to `GET /health` on port 8080.

5. **Verify:**
   ```bash
   curl -i http://<ALB_DNS>/health
   ```

### Public ALB URL

`http://order-api-alb-1514561568.us-east-2.elb.amazonaws.com`

### ECS Service Name

`order-api-service`

### Database Type

RDS Postgres (`db.t3.micro`, single-AZ)

### Instance Types

- ECS: Fargate 0.25 vCPU / 0.5 GB
- RDS: db.t3.micro

## Load Test

### Run

```bash
k6 run loadtest.js
```

### Results

| Metric | Value |
|--------|-------|
| VUs | 10 |
| Duration | 30s |
| RPS | ~TBD |
| p50 latency | ~TBD |
| p95 latency | ~TBD |
| p99 latency | ~TBD |
| Failed requests | ~TBD |

### Analysis

`<to be filled after running load test>`

## Endpoints

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/health` | Health check (DB connectivity) |
| GET | `/` | HTML landing page |
| POST | `/items` | Create an item |
| GET | `/items/{id}` | Get item by ID |
| POST | `/orders` | Create order (idempotent, requires `Idempotency-Key` header) |
| GET | `/orders/{order_id}` | Get order by ID |