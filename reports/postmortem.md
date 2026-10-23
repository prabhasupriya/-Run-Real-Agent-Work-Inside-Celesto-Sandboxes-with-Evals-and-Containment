# Reliability post-mortem

> **These numbers are SIMULATED** (in-memory mock provider, injected faults at fixed rates). They validate the harness and the arithmetic, not Celesto's real reliability. Re-run `python -m agent.make_reports --executor celesto` for real numbers.

Executor: `mock`. Runs: 20. Success rate: 95.0%. Failed rows are kept in reliability.json.

## Failures (1)

- 1x `SandboxError: boot probe timed out (simulated)`

Classification: boot-time failures are infrastructure faults (nothing to resume, the run is marked failed and the half-created guest is still deleted). Failures after boot mean a step exhausted its 3 attempts under the resume policy; the guest was deleted afterwards either way.

## Recovered runs (1)

Runs that hit a transient fault and still succeeded through the `resume` policy (same disk, retry the failed step). These count as successes but cost extra wall-clock.

## What I would fix next

1. Add a boot retry on a *fresh name* (boot failures currently fail the run outright).
2. Separate 'action did not run' from 'outcome unknown' for non-idempotent steps; all current steps are idempotent, which is why blind retry is safe here.
3. Cap total spend per run, not just attempts per step.
