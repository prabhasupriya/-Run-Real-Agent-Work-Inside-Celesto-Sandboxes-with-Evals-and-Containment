"""Live API contract tests. Run against a running stack:  API_URL=http://localhost:8080 pytest tests/test_api.py"""
import os
import uuid

import pytest

httpx = pytest.importorskip("httpx")
BASE = os.environ.get("API_URL", "http://localhost:8080")


def _up() -> bool:
    try:
        return httpx.get(f"{BASE}/health", timeout=2).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="API not running (docker compose up -d)")


def test_health():
    r = httpx.get(f"{BASE}/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_seed_listed():
    rows = httpx.get(f"{BASE}/api/runs").json()
    assert isinstance(rows, list) and "sbx-seeddemo1" in [r["sandbox_name"] for r in rows]


def _body(**kw):
    base = {"run_id": "run-" + uuid.uuid4().hex[:8], "sandbox_name": "sbx-" + uuid.uuid4().hex[:10],
            "problem_id": "p1", "state": "created", "execution_target": "celesto_sandbox"}
    return {**base, **kw}


def test_post_valid_and_invalid():
    ok = httpx.post(f"{BASE}/api/runs", json=_body())
    assert ok.status_code == 201 and ok.json()["state"] == "created"
    assert httpx.post(f"{BASE}/api/runs", json=_body(execution_target="host")).status_code == 400
    assert httpx.post(f"{BASE}/api/runs", json=_body(state="exploded")).status_code == 400
    assert httpx.post(f"{BASE}/api/runs", json=_body(sandbox_name="bad_name")).status_code == 400


def test_reports_served():
    assert httpx.get(f"{BASE}/api/reports/reliability.json").status_code == 200
    assert httpx.get(f"{BASE}/api/reports/../app/main.py").status_code in (404,)
