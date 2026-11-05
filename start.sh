#!/bin/sh
set -e
PGPASSWORD=$DB_PASSWORD psql -v ON_ERROR_STOP=1 -q -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f /app/init_db.sql
exec uvicorn main:app --host 0.0.0.0 --port 8080
