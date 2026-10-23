"""Run N jobs at once, each in its own uniquely named sandbox, and record what we watched."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .audit import Audit
from .config import reports_dir
from .hooks import SafetyHook
from .providers import Provider
from .task import run_job


def run_concurrency(provider: Provider, hook: SafetyHook, audit: Audit, n: int = 5) -> dict:
    jobs = []

    def work(_: int):
        started = datetime.now(timezone.utc).isoformat()
        res = run_job(provider, hook, audit, policy="resume")
        return {"sandbox_name": res.sandbox_name, "started_at": started, "success": res.success,
                "wall_clock_s": round(res.wall_clock_s, 3), "error": res.error}

    with ThreadPoolExecutor(max_workers=n) as pool:
        jobs = list(pool.map(work, range(n)))

    names = [j["sandbox_name"] for j in jobs]
    collisions = len(names) - len(set(names))
    failed = [j for j in jobs if not j["success"]]
    leaked = provider.list_names()
    notes = [f"Watched: name collisions ({collisions} found across {n} concurrent jobs), shared state "
             f"(each guest has its own disk; audit/trace writers are lock-protected), leaked guests after the "
             f"batch ({len(leaked)}), failed jobs ({len(failed)})."]
    if failed:
        notes.append("Failures: " + "; ".join(sorted({str(j['error']) for j in failed})))
    if provider.mode == "mock":
        notes.append("SIMULATED run: provider rate limits, per-account sandbox caps (free trial = 1 retained "
                     "sandbox) and real cost blow-up were NOT exercised. Re-run with the local runtime or a "
                     "paid top-up to observe them.")
    else:
        notes.append("Real run: check the Celesto account limit; on the free trial only one retained sandbox "
                     "is allowed, so concurrent creates are expected to be rejected there.")
    report = {"jobs": jobs, "breakage_notes": " ".join(notes), "mode": provider.mode,
              "leaked_after_batch": leaked}
    (reports_dir() / "concurrency.json").write_text(json.dumps(report, indent=2))
    return report
