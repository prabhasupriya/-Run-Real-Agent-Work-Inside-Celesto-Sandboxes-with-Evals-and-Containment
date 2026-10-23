"""Prove cleanup on success, failure and interrupt by actually running all three."""
from __future__ import annotations

import json

from .audit import Audit
from .config import reports_dir
from .hooks import SafetyHook
from .providers import Provider
from .task import run_job


def run_lifecycle(provider: Provider, hook: SafetyHook, audit: Audit) -> dict:
    names: list[str] = []

    ok = run_job(provider, hook, audit, policy="resume")
    names.append(ok.sandbox_name)
    cleanup_success = ok.success and ok.sandbox_name not in provider.list_names() and not ok.cleanup_errors

    bad = run_job(provider, hook, audit, policy="resume", fail_step=7, fail_permanent=True)
    names.append(bad.sandbox_name)
    cleanup_failure = (not bad.success) and bad.sandbox_name not in provider.list_names() \
        and not bad.cleanup_errors

    # Ctrl-C: find the name the interrupted job used by diffing the audit-visible guest list
    before = set(provider.list_names())
    interrupted = False
    try:
        run_job(provider, hook, audit, policy="resume", interrupt_at_step=4)
    except KeyboardInterrupt:
        interrupted = True
    leaked = set(provider.list_names()) - before
    cleanup_interrupt = interrupted and not leaked
    # the interrupted job's name is the last sandbox in the audit log
    last = [json.loads(l) for l in audit.path.read_text().splitlines() if l.strip()][-1]["sandbox_name"]
    names.append(last)

    report = {
        "cleanup_on_success": bool(cleanup_success),
        "cleanup_on_failure": bool(cleanup_failure),
        "cleanup_on_interrupt": bool(cleanup_interrupt),
        "sandbox_names": list(dict.fromkeys(names)),
        "mechanism": "agent/task.py run_job(): try/except/finally deletes every guest it created "
                     "(including fresh-policy replacements); KeyboardInterrupt/SIGTERM propagate "
                     "through finally. Names are sbx-<uuid4 hex[:10]>, never reused.",
        "mode": provider.mode,
    }
    (reports_dir() / "lifecycle.json").write_text(json.dumps(report, indent=2))
    return report
