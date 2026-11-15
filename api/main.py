import hashlib
import json
import os
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="Health Hub", docs_url=None, redoc_url=None)
DSN = os.environ["DATABASE_URL"]
INGEST_KEY = os.environ["HEALTHHUB_INGEST_KEY"]

@contextmanager
def db():
    with psycopg.connect(DSN) as connection:
        yield connection

@app.on_event("startup")
def initialise() -> None:
    with db() as connection:
        connection.execute("""
          CREATE TABLE IF NOT EXISTS health_events (
            id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL,
            event_type TEXT NOT NULL, occurred_at TIMESTAMPTZ,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB NOT NULL, digest CHAR(64) UNIQUE NOT NULL
          );
          CREATE INDEX IF NOT EXISTS health_events_type_time
          ON health_events(event_type, occurred_at DESC);
        """)

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}

@app.post("/v1/ingest")
def ingest(record: dict, x_api_key: str = Header(default="")) -> dict:
    if x_api_key != INGEST_KEY:
        raise HTTPException(401, "unauthorized")
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with db() as connection:
        connection.execute(
            """INSERT INTO health_events (source,event_type,occurred_at,payload,digest)
               VALUES (%s,%s,%s,%s::jsonb,%s) ON CONFLICT (digest) DO NOTHING""",
            (str(record.get("source", "unknown")), str(record.get("event_type", "unknown")),
             record.get("occurred_at"), payload, digest),
        )
    return {"accepted": True}
