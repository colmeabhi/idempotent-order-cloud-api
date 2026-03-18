#!/bin/sh
PGPASSWORD=$DB_PASSWORD psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -f /app/init_db.sql
uvicorn main:app --host 0.0.0.0 --port 8080