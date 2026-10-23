"""Run every required experiment and write reports/*.json, logs/commands.jsonl.

    python -m agent.make_reports --executor mock      # no KVM / no cloud key needed (simulated)
    python -m agent.make_reports --executor celesto   # real guests (local runtime or Celesto Cloud)
"""
from __future__ import annotations

import argparse
import signal
import sys

from .audit import Audit
from .concurrency import run_concurrency
from .config import env, logs_dir, reports_dir
from .containment import run_containment
from .evals import run_eval
from .failure import run_failure_demo
from .hooks import SafetyHook
from .ledger import Ledger
from .lifecycle import run_lifecycle
from .providers import make_provider


def _sigterm(*_):
    raise KeyboardInterrupt("SIGTERM")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--executor", choices=["mock", "celesto"], default=env("AGENT_EXECUTOR", "celesto"))
    ap.add_argument("--runs", type=int, default=int(env("EVAL_RUNS", "20")))
    ap.add_argument("--jobs", type=int, default=int(env("CONCURRENT_JOBS", "5")))
    args = ap.parse_args(argv)
    if args.runs < 20 or args.jobs < 5:
        ap.error("the contract needs --runs >= 20 and --jobs >= 5")

    signal.signal(signal.SIGTERM, _sigterm)
    reports_dir().mkdir(parents=True, exist_ok=True)
    audit = Audit(logs_dir() / "commands.jsonl")
    audit.reset()
    hook = SafetyHook()
    seed = int(env("MOCK_SEED", "7"))
    ledger = Ledger(env("LEDGER_URL") or None)
    print(f"executor={args.executor}")

    clean = make_provider(args.executor, seed=seed, faulty=False)
    print("[1/5] containment test");  c = run_containment(clean, audit)
    print("[2/5] step-7 failure policy"); f = run_failure_demo(clean, hook, audit)
    print("[3/5] lifecycle (success / failure / interrupt)"); lc = run_lifecycle(clean, hook, audit)
    faulty = make_provider(args.executor, seed=seed, faulty=True)
    print(f"[4/5] reliability eval ({args.runs} fresh sandboxes)")
    rel = run_eval(faulty, hook, audit, n=args.runs, ledger=ledger, use_llm=env("USE_LLM", "0") == "1")
    print(f"[5/5] concurrency ({args.jobs} jobs)"); cc = run_concurrency(faulty, hook, audit, n=args.jobs)

    print("\n== summary ==")
    print(f"containment: blocked={c['blocked']} host_untouched={c['host_untouched']}")
    print(f"failure policy: {f['policy']} (job_succeeded={f['outcome']['job_succeeded']})")
    print(f"lifecycle: success={lc['cleanup_on_success']} failure={lc['cleanup_on_failure']} "
          f"interrupt={lc['cleanup_on_interrupt']}")
    print(f"reliability: rate={rel['success_rate']:.2%} median={rel['median_wall_clock_s']}s "
          f"p90={rel['p90_wall_clock_s']}s boot={rel['median_boot_to_ready_s']}s cost=${rel['total_cost_usd']}")
    print(f"concurrency: {len(cc['jobs'])} jobs, leaked={cc['leaked_after_batch']}")
    ok = c["blocked"] and c["host_untouched"] and all(
        lc[k] for k in ("cleanup_on_success", "cleanup_on_failure", "cleanup_on_interrupt"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
