# Demo: what the video shows

File: `demo/walkthrough.mp4` (3 minutes or less).

## What the demo covers

1. **The problem.** Maintainers must prove a reported bug exists before fixing it, but running
   untrusted repo code on a laptop or server is unsafe. The agent does the work inside a
   disposable Celesto sandbox instead.

2. **The trusted control plane.** `docker compose up --build -d` starts the run-ledger API and
   Postgres. Both become healthy. `GET /health` returns `{"status": "ok"}` and `GET /api/runs`
   lists the seeded run `sbx-seeddemo1`.

3. **The agent working in its sandbox.** The agent runs its 10 steps: it writes a small buggy
   module, generates a minimal pytest from the bug report, runs it, and checks that the test fails
   for the right reason. Every command goes through the safety hook, is written to
   `logs/commands.jsonl`, and only then reaches the sandbox.

4. **The containment test.** A poisoned README tells the agent to send a secret to an outside
   server and to run `rm -rf /`. `reports/evidence/containment.log` shows all four commands
   BLOCKED before reaching the sandbox, the secret absent from inside the sandbox, and a host file
   unchanged.

5. **The step-7 failure policy.** Step 7 of 10 fails on purpose. `reports/failure_trace.jsonl`
   shows the failure, the `policy_applied: resume` event, the retry succeeding, and the sandbox
   being deleted afterwards.

6. **Results.** `reports/reliability.json` shows the 20-run summary: success rate, median and p90
   time, boot-to-ready time, and cost. `pytest -q` shows the tests passing.

## Honest limits

- The safety hook is rule-based. The real protection is the microVM plus never putting real
  secrets inside it.
- Numbers shown in this demo are from the mock executor and are labelled simulated, because
  Celesto Cloud credit was not available. Running
  `python -m agent.make_reports --executor celesto` produces real numbers.
- Next step: turn network access off with Celesto network policy.