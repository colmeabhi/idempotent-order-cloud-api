"""Delete idempotency records older than the TTL.

Keys only need to live as long as a client might retry (default 24h). After
that they are just table bloat. Expiry is also enforced at request time (see
_create_order_tx), so the job only reclaims space; it is not needed for
correctness and it's fine if it runs late.

Runs inside the API on an interval (IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS),
guarded by an advisory lock so only one ECS task purges at a time. It can also
be run by hand or from cron:

    python cleanup.py
"""

import json
import logging
import os

logger = logging.getLogger("orders")

CLEANUP_LOCK_ID = 727002


def ttl_seconds():
    return int(float(os.getenv("IDEMPOTENCY_TTL_HOURS", "24")) * 3600)


def purge_expired(conn, ttl=None, batch_size=1000):
    """Delete expired keys in small batches (short locks, small WAL bursts).

    Returns the number of rows deleted, or None if another process holds the
    cleanup lock. Leaves the connection with no open transaction.
    """
    ttl = ttl_seconds() if ttl is None else ttl
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (CLEANUP_LOCK_ID,))
        got_lock = cur.fetchone()[0]
        conn.commit()
        if not got_lock:
            return None
        try:
            total = 0
            while True:
                cur.execute(
                    "DELETE FROM idempotency_records WHERE idem_key IN ("
                    "  SELECT idem_key FROM idempotency_records"
                    "  WHERE created_at < now() - make_interval(secs => %s)"
                    "  LIMIT %s FOR UPDATE SKIP LOCKED)",
                    (ttl, batch_size))
                deleted = cur.rowcount
                conn.commit()
                total += deleted
                if deleted < batch_size:
                    return total
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (CLEANUP_LOCK_ID,))
            conn.commit()


def run_once(database):
    with database.connection() as conn:
        deleted = purge_expired(conn)
    if deleted:
        logger.info(json.dumps({"level": "INFO", "msg": "idempotency_cleanup", "deleted": deleted}))
    return deleted


if __name__ == "__main__":
    from db import Database

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    d = Database(maxconn=1)
    print(json.dumps({"deleted": run_once(d)}))
    d.close()
