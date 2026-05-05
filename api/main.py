"""FastAPI backend wrapping the BigQuery MRR query for the React dashboard.

Run:
    uvicorn api.main:app --reload --port 8000

Environment (read from .env at the project root):
    BIGQUERY_PROJECT — GCP project that owns the stripe_raw dataset.

Auth: BigQuery uses Application Default Credentials.
    gcloud auth application-default login
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import bigquery

# Mirror scripts/etl.py: .env always wins over stale shell vars.
load_dotenv(override=True)

PROJECT_ID = os.environ.get("BIGQUERY_PROJECT", "")
REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = REPO_ROOT / "sql" / "mrr_monthly.sql"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/mrr")
def mrr():
    # SQL is read at request time so edits hot-reload without a server restart.
    try:
        sql = SQL_FILE.read_text()
        client = bigquery.Client(project=PROJECT_ID)
        rows = list(client.query(sql).result())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Cast to plain primitives — Row.mrr_amount is Decimal, Row.month is date.
    return [
        {
            "month": row.month.isoformat(),
            "mrr_amount": float(row.mrr_amount),
            "is_current": bool(row.is_current),
        }
        for row in rows
    ]
