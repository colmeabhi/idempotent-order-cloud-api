-- Idempotent: runs on every container start (start.sh).
-- The advisory lock serialises concurrent runs when several ECS tasks boot at once.
SELECT pg_advisory_lock(727001);

CREATE TABLE IF NOT EXISTS items (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT DEFAULT 'created'
);

CREATE TABLE IF NOT EXISTS ledger (
    ledger_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    customer_id TEXT NOT NULL,
    amount REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    idem_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response_body TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration: created_at used to be TEXT (ISO-8601). TTL queries need a real timestamp.
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_name = 'idempotency_records' AND column_name = 'created_at') = 'text' THEN
        ALTER TABLE idempotency_records
            ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at::timestamptz,
            ALTER COLUMN created_at SET DEFAULT now();
    END IF;
END $$;

-- Used by the TTL cleanup job.
CREATE INDEX IF NOT EXISTS idempotency_records_created_at_idx ON idempotency_records (created_at);

SELECT pg_advisory_unlock(727001);
