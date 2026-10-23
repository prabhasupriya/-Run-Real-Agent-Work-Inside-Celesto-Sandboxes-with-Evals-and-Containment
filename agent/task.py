"""The agent job: "Issue -> Failing Test". 10 steps, all executed inside one Celesto computer.

Given a bug report, the agent writes a minimal failing pytest in the guest and proves it
fails for the RIGHT reason (an AssertionError on the reported behaviour), not because the
setup is broken (ImportError, missing pytest, syntax error).

State design: step 7 is the designed crash point. Three named recovery policies exist
(resume / rollback / fresh). Lifecycle: every sandbox created is deleted in `finally`,
on success, failure and Ctrl-C.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from .audit import Audit, TraceWriter
from .hooks import CommandBlockedError, SafetyHook
from .ledger import Ledger
from .llm import generate_test
from .providers import Provider, SandboxError
from .sandbox import GuardedSandbox

WORK = "/tmp/work"
TOTAL_STEPS = 10

BUGGY_MODULE = '''def slugify(text):
    return text.strip().lower().replace(" ", "-")
'''
BUG_REPORT = (
    "slugify('Hello   World') returns 'hello---world'. "
    "Repeated whitespace should collapse to a single hyphen: expected 'hello-world'."
)
WRONG_REASON = re.compile(r"ModuleNotFoundError|ImportError|SyntaxError|NameError|no tests ran|IndentationError")


class StepFailed(RuntimeError):
    pass


def new_name() -> str:
    return "sbx-" + uuid.uuid4().hex[:10]


class Ctx:
    def __init__(self, sb: GuardedSandbox, use_llm: bool):
        self.sb = sb
        self.use_llm = use_llm
        self.data: dict = {}

    def run_ok(self, command: str):
        r = self.sb.run(command)
        if r.exit_code != 0:
            raise StepFailed(f"`{command[:60]}` exited {r.exit_code}: {r.stderr.strip()[:120]}")
        return r


# ---- the ten steps (each idempotent so that resume/rollback/fresh are all safe) ----------
def s1_workspace(c: Ctx): c.run_ok(f"mkdir -p {WORK}")
def s2_python(c: Ctx): c.data["python"] = c.run_ok("python3 --version").stdout.strip()
def s3_pytest(c: Ctx): c.run_ok("python3 -m pytest --version || pip install -q pytest")
def s4_fixture(c: Ctx): c.run_ok(f"mkdir -p {WORK}") and c.sb.write_file(f"{WORK}/slugify.py", BUGGY_MODULE)
def s5_report(c: Ctx): c.sb.write_file(f"{WORK}/issue.md", "# Bug\n\n" + BUG_REPORT + "\n")


def s6_generate(c: Ctx):
    code, source = generate_test(BUG_REPORT, c.use_llm)
    c.data["test_source"] = source
    c.sb.write_file(f"{WORK}/test_repro.py", code)


def s7_run_test(c: Ctx):
    r = c.run_ok(f"cd {WORK} && python3 -m pytest -x -q test_repro.py > result.txt 2>&1; echo $?")
    code = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if code == "0":
        raise StepFailed("test passed: it does not reproduce the bug")
    c.data["pytest_exit"] = int(code) if code.isdigit() else -1


def s8_classify(c: Ctx):
    out = c.run_ok(f"cat {WORK}/result.txt").stdout
    right = ("AssertionError" in out or "assert " in out) and "hello---world" in out \
        and not WRONG_REASON.search(out)
    if not right:
        raise StepFailed("test fails for the WRONG reason (setup problem), not the reported bug")
    c.data["right_reason"] = True
    c.data["failure_line"] = next((l.strip() for l in out.splitlines() if "AssertionError" in l), "")


def s9_proof(c: Ctx):
    proof = {
        "issue": BUG_REPORT, "test_file": "test_repro.py", "pytest_exit": c.data["pytest_exit"],
        "fails_for_right_reason": c.data["right_reason"], "failure_line": c.data["failure_line"],
        "test_source": c.data.get("test_source"), "sandbox_name": c.sb.name,
    }
    c.sb.write_file(f"{WORK}/proof.json", json.dumps(proof))


def s10_verify(c: Ctx):
    proof = json.loads(c.run_ok(f"cat {WORK}/proof.json").stdout)
    if not proof.get("fails_for_right_reason"):
        raise StepFailed("proof.json does not certify a right-reason failure")
    c.data["proof"] = proof


STEPS = [("prepare_workspace", s1_workspace), ("probe_python", s2_python), ("ensure_pytest", s3_pytest),
         ("write_fixture", s4_fixture), ("write_bug_report", s5_report), ("generate_test", s6_generate),
         ("run_test", s7_run_test), ("classify_failure", s8_classify), ("write_proof", s9_proof),
         ("verify_proof", s10_verify)]
assert len(STEPS) == TOTAL_STEPS


@dataclass
class JobResult:
    sandbox_name: str
    success: bool = False
    wall_clock_s: float = 0.0
    boot_to_ready_s: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None
    recovered_failures: int = 0
    sandboxes_created: int = 1
    cleanup_errors: list[str] = field(default_factory=list)
    proof: dict | None = None


def run_job(provider: Provider, hook: SafetyHook, audit: Audit, *, policy: str = "resume",
            fail_step: int | None = None, fail_permanent: bool = False,
            interrupt_at_step: int | None = None, trace: TraceWriter | None = None,
            ledger: Ledger | None = None, use_llm: bool = False, max_attempts: int = 3) -> JobResult:
    if policy not in ("resume", "rollback", "fresh"):
        raise ValueError(f"unknown policy {policy}")
    t0 = provider.now()
    names = [new_name()]
    res = JobResult(sandbox_name=names[0])
    injected: set[int] = set()
    attempts: dict[int, int] = {}
    ckpt_after: int | None = None
    ctx: Ctx | None = None

    def ev(event: str, **kw):
        if trace:
            trace.write(event, sandbox_name=names[-1], **kw)

    def post(state: str):
        if ledger:
            ledger.post(res.sandbox_name, state)

    def boot(name: str, first: bool) -> tuple[GuardedSandbox, float]:
        t = provider.now()
        sb = GuardedSandbox(provider, name, hook, audit)
        provider.create(name)               # Created: Celesto returned a guest
        if first:
            post("created")
        ev("created")
        probe = sb.run("echo ready")        # boot probe: the boot-to-ready clock stops here
        if probe.exit_code != 0 or "ready" not in probe.stdout:
            raise SandboxError("boot probe failed")
        if first:
            post("ready")
        ev("ready")
        return sb, provider.now() - t

    def recover(step_no: int) -> int:
        """Apply the named policy. Returns the 0-based index of the step to run next."""
        nonlocal ctx
        if policy == "resume":
            try:
                alive = "alive" in ctx.sb.run("echo alive").stdout
            except SandboxError:
                alive = False
            if not alive:
                provider.start(names[-1])  # stop/start persistence: same disk comes back
                ev("sandbox_restarted")
            ev("policy_applied", policy="resume", step=step_no,
               detail="same guest and disk kept; retrying the failed step only")
            return step_no - 1
        if policy == "rollback":
            if ckpt_after is not None:
                ctx.sb.run(f"rm -rf {WORK} && mkdir -p {WORK} && tar xzf /tmp/ckpt.tgz -C {WORK}")
                ev("policy_applied", policy="rollback", step=step_no,
                   detail=f"workspace restored to checkpoint after step {ckpt_after}")
                return ckpt_after
            ctx.sb.run(f"rm -rf {WORK} && mkdir -p {WORK}")
            ev("policy_applied", policy="rollback", step=step_no, detail="no checkpoint yet; clean workspace")
            return 0
        # fresh: throw the guest away and replay from step 1 on a new uniquely named guest
        old = names[-1]
        provider.delete(old)
        names.append(new_name())
        res.sandboxes_created += 1
        sb, _ = boot(names[-1], first=False)
        ctx = Ctx(sb, use_llm)
        ev("policy_applied", policy="fresh", step=step_no, detail=f"deleted {old}; replaying from step 1")
        return 0

    try:
        tb = provider.now()
        try:
            sb, res.boot_to_ready_s = boot(names[0], first=True)
        except Exception:
            res.boot_to_ready_s = max(0.0, provider.now() - tb)
            raise
        ctx = Ctx(sb, use_llm)
        post("running")
        ev("running")
        i = 0
        while i < TOTAL_STEPS:
            n = i + 1
            step_name, fn = STEPS[i]
            if interrupt_at_step == n:
                raise KeyboardInterrupt("simulated Ctrl-C")
            try:
                ev("step_started", step=n, step_name=step_name)
                if fail_step == n and (fail_permanent or n not in injected):
                    injected.add(n)
                    ctx.sb.run(f"printf partial > {WORK}/result.partial")  # half-finished state on disk
                    raise StepFailed(f"injected crash at step {n} (simulated OOM-kill of the test runner)")
                fn(ctx)
                ev("step_succeeded", step=n, step_name=step_name)
                if policy == "rollback" and n == 6:
                    ctx.sb.run(f"tar czf /tmp/ckpt.tgz -C {WORK} .")
                    ckpt_after = 6
                    ev("checkpoint_taken", step=n)
                i += 1
            except CommandBlockedError:
                raise
            except (StepFailed, SandboxError) as exc:
                attempts[n] = attempts.get(n, 0) + 1
                ev("step_failed", step=n, step_name=step_name, error=str(exc), attempt=attempts[n])
                ev("mid_task_failure", step=n)
                if attempts[n] >= max_attempts:
                    raise
                i = recover(n)
        res.success = True
        res.recovered_failures = sum(attempts.values())
        res.proof = ctx.data.get("proof")
        post("succeeded")
        ev("job_succeeded")
    except Exception as exc:  # noqa: BLE001 - recorded, then cleanup runs in finally
        res.success = False
        res.error = f"{type(exc).__name__}: {exc}"
        post("failed")
        ev("job_failed", error=res.error)
    finally:
        # Lifecycle hygiene: runs on success, failure AND KeyboardInterrupt/SystemExit.
        for name in reversed(names):
            try:
                provider.delete(name)
                ev("sandbox_deleted", deleted=name)
            except Exception as exc:  # noqa: BLE001
                res.cleanup_errors.append(f"{name}: {exc}")
        if ledger:
            ledger.post(res.sandbox_name, "deleted")
        res.wall_clock_s = max(0.0, provider.now() - t0)
        res.cost_usd = res.wall_clock_s * provider.price_per_hour / 3600
    return res
