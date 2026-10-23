"""20+ run reliability eval on fresh sandboxes, plus the post-mortem writer."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .audit import Audit
from .config import reports_dir
from .hooks import SafetyHook
from .ledger import Ledger
from .providers import Provider
from .stats import summarize
from .task import run_job


def run_eval(provider: Provider, hook: SafetyHook, audit: Audit, n: int = 20,
             ledger: Ledger | None = None, use_llm: bool = False) -> dict:
    runs = []
    for i in range(n):
        res = run_job(provider, hook, audit, policy="resume", ledger=ledger, use_llm=use_llm)
        runs.append({
            "sandbox_name": res.sandbox_name,
            "fresh": True,
            "success": res.success,
            "wall_clock_s": round(res.wall_clock_s, 3),
            "boot_to_ready_s": round(res.boot_to_ready_s, 3),
            "cost_usd": round(res.cost_usd, 6),
            "recovered_failures": res.recovered_failures,
            "error": res.error,
        })
        print(f"  eval run {i + 1}/{n}: {'ok ' if res.success else 'FAIL'} {res.wall_clock_s:.2f}s {res.sandbox_name}")
    summary = summarize(runs)
    report = {
        **summary,
        "mode": provider.mode,
        "pricing_note": f"cost_usd = wall_clock_s * ${provider.price_per_hour}/hr (Micro size) / 3600",
        "postmortem_path": "reports/postmortem.md",
        "runs": runs,
    }
    if provider.mode == "mock":
        report["warning"] = ("SIMULATED: produced by the in-memory mock provider with injected faults. "
                             "Re-run with AGENT_EXECUTOR=celesto for real Celesto numbers.")
    out = reports_dir() / "reliability.json"
    out.write_text(json.dumps(report, indent=2))
    (reports_dir() / "postmortem.md").write_text(build_postmortem(report))
    return report


def build_postmortem(report: dict) -> str:
    runs = report["runs"]
    failures = [r for r in runs if not r["success"]]
    recovered = [r for r in runs if r["success"] and r["recovered_failures"]]
    lines = ["# Reliability post-mortem", ""]
    if report["mode"] == "mock":
        lines += ["> **These numbers are SIMULATED** (in-memory mock provider, injected faults at fixed rates). "
                  "They validate the harness and the arithmetic, not Celesto's real reliability. "
                  "Re-run `python -m agent.make_reports --executor celesto` for real numbers.", ""]
    lines += [f"Executor: `{report['mode']}`. Runs: {report['run_count']}. "
              f"Success rate: {report['success_rate']:.1%}. Failed rows are kept in reliability.json.", ""]
    if failures:
        lines += [f"## Failures ({len(failures)})", ""]
        for err, count in Counter((r["error"] or "unknown").split("\n")[0] for r in failures).most_common():
            lines.append(f"- {count}x `{err}`")
        lines += ["", "Classification: boot-time failures are infrastructure faults (nothing to resume, the run is "
                  "marked failed and the half-created guest is still deleted). Failures after boot mean a step "
                  "exhausted its 3 attempts under the resume policy; the guest was deleted afterwards either way.", ""]
    else:
        lines += ["## Failures", "", "No run failed in this batch. That is a statement about this sample only; "
                  "the failure modes watched for were boot-probe timeouts, guest-agent timeouts mid-step, "
                  "and right-reason misclassification at step 8.", ""]
    lines += [f"## Recovered runs ({len(recovered)})", "",
              "Runs that hit a transient fault and still succeeded through the `resume` policy "
              "(same disk, retry the failed step). These count as successes but cost extra wall-clock.", "",
              "## What I would fix next", "",
              "1. Add a boot retry on a *fresh name* (boot failures currently fail the run outright).",
              "2. Separate 'action did not run' from 'outcome unknown' for non-idempotent steps; all current "
              "steps are idempotent, which is why blind retry is safe here.",
              "3. Cap total spend per run, not just attempts per step."]
    text = "\n".join(lines) + "\n"
    assert len(text) >= 200
    return text
