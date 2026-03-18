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
    created_at TEXT NOT NULL
);