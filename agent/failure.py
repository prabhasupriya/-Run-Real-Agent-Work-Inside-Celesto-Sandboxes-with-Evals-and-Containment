"""Step 7 of 10 fails on purpose; the named recovery policy is applied and traced."""
from __future__ import annotations

import json

from .audit import Audit, TraceWriter
from .config import reports_dir
from .hooks import SafetyHook
from .providers import Provider
from .task import TOTAL_STEPS, run_job

POLICY = "resume"

RATIONALE = (
    "Policy: resume. Steps 1-6 only build a small deterministic workspace and every step is idempotent, so "
    "the half-finished state left by a step-7 crash (result.partial) is harmless and the same disk is still "
    "valid. Step-7 failures are overwhelmingly transient (guest-agent timeout, OOM-kill of the test runner), "
    "so retrying only step 7 on the same guest costs one command, whereas rollback needs a checkpoint tar and "
    "fresh pays a new boot plus a replay of six steps and any LLM tokens. If the guest is unreachable the "
    "policy restarts it (stop/start persistence keeps the disk). Attempts are capped at three per step, after "
    "which the run is marked failed and the guest is deleted anyway. Rollback and fresh are implemented and "
    "selectable via run_job(policy=...) for non-idempotent workloads."
)


def run_failure_demo(provider: Provider, hook: SafetyHook, audit: Audit) -> dict:
    trace = TraceWriter(reports_dir() / "failure_trace.jsonl")
    trace.reset()
    res = run_job(provider, hook, audit, policy=POLICY, fail_step=7, trace=trace)
    policy_doc = {
        "failed_step": 7,
        "total_steps": TOTAL_STEPS,
        "policy": POLICY,
        "rationale": RATIONALE,
        "trace_path": "reports/failure_trace.jsonl",
        "outcome": {"job_succeeded": res.success, "recovered_failures": res.recovered_failures,
                    "sandbox_name": res.sandbox_name, "mode": provider.mode},
    }
    (reports_dir() / "failure_policy.json").write_text(json.dumps(policy_doc, indent=2))
    return policy_doc
