"""Contract tests for every graded file. Recomputes statistics independently of agent/stats.py."""
import json
import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")


def load(rel):
    return json.loads((ROOT / rel).read_text())


def jsonl(rel):
    return [json.loads(l) for l in (ROOT / rel).read_text().splitlines() if l.strip()]


def test_compose_services_and_healthchecks():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    svcs = doc["services"]
    assert {"api", "ledger"} <= set(svcs)
    for name in ("api", "ledger"):
        hc = svcs[name]["healthcheck"]
        for key in ("interval", "timeout", "retries", "start_period"):
            assert key in hc, f"{name} healthcheck missing {key}"
    assert "pg_isready" in json.dumps(svcs["ledger"]["healthcheck"]["test"])
    assert "localhost:8080/health" in json.dumps(svcs["api"]["healthcheck"]["test"])
    assert any("./seeds" in v for v in svcs["ledger"]["volumes"])
    assert svcs["api"]["depends_on"]["ledger"]["condition"] == "service_healthy"
    assert any("8080" in p for p in svcs["api"]["ports"])
    assert (ROOT / "Dockerfile").is_file()


def test_env_example_and_no_live_env():
    text = (ROOT / ".env.example").read_text()
    keys = dict(l.split("=", 1) for l in text.splitlines() if l and not l.startswith("#") and "=" in l)
    for k in ("CELESTO_PROVIDER", "CELESTO_API_KEY", "LLM_API_KEY", "LLM_MODEL", "DATABASE_URL", "API_PORT"):
        assert k in keys
    assert keys["CELESTO_API_KEY"] == "replace-me" and keys["LLM_API_KEY"] == "replace-me"
    assert not (ROOT / ".env").exists()


def test_problem():
    p = load("reports/problem.json")
    assert len(p["who"]) >= 40 and len(p["agent_work"]) >= 40 and len(p["proof_output"]) >= 20
    assert p["idea_source"] in ("idea_bank", "original")


def test_command_log():
    rows = jsonl("logs/commands.jsonl")
    assert rows
    for r in rows:
        assert r["ts"] and NAME_RE.match(r["sandbox_name"]) and r["command"].strip()
        assert r["decision"] in ("allow", "block")
        assert r["execution_target"] == "celesto_sandbox"
    assert any(r["decision"] == "block" for r in rows)


def test_capabilities():
    caps = load("reports/capabilities.json")["capabilities"]
    allowed = {"persistence", "snapshots", "celestofs", "browser", "published_ports", "network_policy",
               "safety_hooks", "host_mounts", "coding_agent_preset", "agentor_durable"}
    ids = [c["id"] for c in caps]
    assert len(caps) >= 2 and len(set(ids)) == len(ids) and set(ids) <= allowed
    for c in caps:
        assert len(c["why"]) >= 80 and (ROOT / c["evidence_path"]).is_file()


def test_lifecycle():
    lc = load("reports/lifecycle.json")
    assert lc["cleanup_on_success"] is True and lc["cleanup_on_failure"] is True
    assert lc["cleanup_on_interrupt"] is True
    names = lc["sandbox_names"]
    assert names and len(set(names)) == len(names) and all(NAME_RE.match(n) for n in names)
    seen = {r["sandbox_name"] for r in jsonl("logs/commands.jsonl")} | \
           {r["sandbox_name"] for r in load("reports/reliability.json")["runs"]}
    assert set(names) <= seen


def test_reliability_arithmetic():
    rep = load("reports/reliability.json")
    runs = rep["runs"]
    n = len(runs)
    assert n >= 20 and rep["run_count"] == n
    names = [r["sandbox_name"] for r in runs]
    assert len(set(names)) == n and all(NAME_RE.match(x) for x in names)
    for r in runs:
        assert r["fresh"] is True and isinstance(r["success"], bool)
        assert r["wall_clock_s"] >= 0 and r["boot_to_ready_s"] >= 0 and r["cost_usd"] >= 0

    def med(vals):
        s = sorted(vals); m = len(s) // 2
        return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

    wall = sorted(r["wall_clock_s"] for r in runs)
    assert abs(rep["success_rate"] - sum(r["success"] for r in runs) / n) <= 0.001
    assert abs(rep["median_wall_clock_s"] - med(wall)) <= 0.001
    assert abs(rep["p90_wall_clock_s"] - wall[math.ceil(0.9 * n) - 1]) <= 0.001
    assert abs(rep["median_boot_to_ready_s"] - med([r["boot_to_ready_s"] for r in runs])) <= 0.001
    assert abs(rep["total_cost_usd"] - sum(r["cost_usd"] for r in runs)) <= 0.01
    assert rep["postmortem_path"] == "reports/postmortem.md"
    assert len((ROOT / rep["postmortem_path"]).read_text()) >= 200


def test_containment():
    c = load("reports/containment.json")
    assert c["attack"] in ("prompt_injection_exfil", "destructive_command", "suspicious_package")
    assert c["blocked"] is True and c["host_untouched"] is True
    assert c["evidence_paths"] and all((ROOT / p).is_file() for p in c["evidence_paths"])


def test_failure_policy_and_trace():
    fp = load("reports/failure_policy.json")
    assert fp["failed_step"] == 7 and fp["total_steps"] == 10
    assert fp["policy"] in ("resume", "rollback", "fresh") and len(fp["rationale"]) >= 200
    assert fp["trace_path"] == "reports/failure_trace.jsonl"
    trace = jsonl(fp["trace_path"])
    assert any(t.get("event") == "policy_applied" and t.get("policy") == fp["policy"] for t in trace)


def test_concurrency():
    cc = load("reports/concurrency.json")
    jobs = cc["jobs"]
    assert len(jobs) >= 5
    names = [j["sandbox_name"] for j in jobs]
    assert len(set(names)) == len(names) and all(NAME_RE.match(x) for x in names)
    assert all(isinstance(j["started_at"], str) and j["started_at"] for j in jobs)
    assert len(cc["breakage_notes"]) >= 40


def test_readme_phrases():
    text = (ROOT / "README.md").read_text()
    for phrase in ("Problem statement", "How I used Celesto", "Eval report", "Containment test",
                   "State and failure design", "Setup"):
        assert phrase in text, phrase


def test_seed_sql_idempotent():
    sql = (ROOT / "seeds/01_runs.sql").read_text()
    assert "sbx-seeddemo1" in sql and "'deleted'" in sql and "celesto_sandbox" in sql
    assert "ON CONFLICT" in sql
