"""Run Ledger API (trusted control plane).

Stores run rows in Postgres and serves the graded report files. It never executes
agent tools: command execution lives in the `agent` package and goes through Celesto.
"""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from psycopg.rows import dict_row

log = logging.getLogger("ledger")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ledger:ledger@ledger:5432/ledger")
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "/app/reports")).resolve()
LOGS_DIR = Path(os.environ.get("LOGS_DIR", "/app/logs")).resolve()

NAME_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")
STATES = {"created", "ready", "running", "succeeded", "failed", "deleted"}
TARGET = "celesto_sandbox"

INIT_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sandbox_name TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('created','ready','running','succeeded','failed','deleted')),
    execution_target TEXT NOT NULL CHECK (execution_target = 'celesto_sandbox'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO runs (run_id, sandbox_name, problem_id, state, execution_target)
VALUES ('run-seeddemo1', 'sbx-seeddemo1', 'issue-to-failing-test', 'deleted', 'celesto_sandbox')
ON CONFLICT (run_id) DO NOTHING;
"""

COLUMNS = "run_id, sandbox_name, problem_id, state, execution_target, created_at, updated_at"


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Belt and braces: the compose init script seeds on first boot, this keeps the API
    # usable against any empty Postgres too. Idempotent.
    for attempt in range(30):
        try:
            with connect() as conn:
                conn.execute(INIT_SQL)
            log.info("ledger schema ready")
            break
        except psycopg.Error as exc:
            log.warning("ledger not ready (attempt %s): %s", attempt + 1, exc)
            time.sleep(2)
    yield


app = FastAPI(title="Celesto Run Ledger", lifespan=lifespan)


def _bad(msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=400)


def _serialise(row: dict) -> dict:
    out = dict(row)
    for key in ("created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/runs")
def list_runs():
    try:
        with connect() as conn:
            rows = conn.execute(f"SELECT {COLUMNS} FROM runs ORDER BY created_at, run_id").fetchall()
    except psycopg.Error as exc:
        return JSONResponse({"error": f"ledger unavailable: {exc}"}, status_code=503)
    return [_serialise(r) for r in rows]


RUN_BODY_SCHEMA = {
    "type": "object",
    "required": ["run_id", "sandbox_name", "problem_id", "state", "execution_target"],
    "properties": {
        "run_id": {"type": "string"},
        "sandbox_name": {"type": "string", "pattern": "^sbx-[a-z0-9]{8,}$"},
        "problem_id": {"type": "string"},
        "state": {"type": "string", "enum": sorted(STATES)},
        "execution_target": {"type": "string", "enum": [TARGET]},
    },
}
RUN_BODY_EXAMPLE = {
    "run_id": "run-demo-0001",
    "sandbox_name": "sbx-demo000001",
    "problem_id": "issue-to-failing-test",
    "state": "created",
    "execution_target": "celesto_sandbox",
}


# openapi_extra only documents the body for /docs (Swagger "Try it out" would otherwise send
# an empty body and get the 400 below). Validation stays in the handler.
@app.post(
    "/api/runs",
    status_code=201,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/json": {"schema": RUN_BODY_SCHEMA, "example": RUN_BODY_EXAMPLE}}}},
)
async def create_run(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _bad("body must be valid JSON")
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")

    for key in ("run_id", "sandbox_name", "problem_id", "state", "execution_target"):
        if not isinstance(body.get(key), str) or not body[key].strip():
            return _bad(f"{key} must be a non-empty string")
    if body["execution_target"] != TARGET:
        return _bad(f"execution_target must be '{TARGET}'")
    if body["state"] not in STATES:
        return _bad(f"state must be one of {sorted(STATES)}")
    if not NAME_RE.match(body["sandbox_name"]):
        return _bad("sandbox_name must match sbx- followed by 8+ lowercase letters or digits")

    try:
        with connect() as conn:
            row = conn.execute(
                f"""INSERT INTO runs (run_id, sandbox_name, problem_id, state, execution_target)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        sandbox_name = EXCLUDED.sandbox_name,
                        problem_id = EXCLUDED.problem_id,
                        state = EXCLUDED.state,
                        execution_target = EXCLUDED.execution_target,
                        updated_at = now()
                    RETURNING {COLUMNS}""",
                (body["run_id"], body["sandbox_name"], body["problem_id"], body["state"], TARGET),
            ).fetchone()
    except psycopg.Error as exc:
        return JSONResponse({"error": f"ledger unavailable: {exc}"}, status_code=503)
    return JSONResponse(_serialise(row), status_code=201)


def _safe(base: Path, rel: str) -> Path | None:
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


@app.get("/api/reports")
def list_reports():
    files = sorted(
        str(p.relative_to(REPORTS_DIR)) for p in REPORTS_DIR.rglob("*") if p.is_file()
    ) if REPORTS_DIR.exists() else []
    return {"reports": files}


@app.get("/api/reports/{path:path}")
def get_report(path: str):
    target = _safe(REPORTS_DIR, path)
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target)


@app.get("/api/commands")
def get_commands():
    target = _safe(LOGS_DIR, "commands.jsonl")
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, media_type="application/x-ndjson")