"""Execution providers. Every agent tool call lands in a Celesto computer, never on the host.

* CelestoCliProvider drives real guests (local runtime or Celesto Cloud) through the
  `celesto computer ... --json` CLI. This is the only module that spawns a host process,
  and the only thing it ever spawns is the Celesto CLI itself (create/exec/stop/start/
  delete/list). The *agent's* commands are passed to the guest as data.
* MockProvider is an in-memory simulator for CI / reviewers without KVM or a cloud key.
  It executes nothing at all. Anything produced with it is labelled mode="mock".
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .config import price_per_hour


@dataclass
class ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxError(RuntimeError):
    """Infrastructure-level failure (boot, transport, missing guest). Not a command failure."""


class Provider:
    name = "base"
    mode = "base"

    @property
    def price_per_hour(self) -> float:
        return price_per_hour()

    def now(self) -> float:
        return time.monotonic()

    def create(self, name: str) -> None: raise NotImplementedError
    def exec(self, name: str, command: str, timeout: int = 120) -> ExecResult: raise NotImplementedError
    def stop(self, name: str) -> None: raise NotImplementedError
    def start(self, name: str) -> None: raise NotImplementedError
    def delete(self, name: str) -> None: raise NotImplementedError
    def list_names(self) -> list[str]: raise NotImplementedError


# ---------------------------------------------------------------------------------------
def _parse_envelope(stdout: str, returncode: int, stderr: str = "") -> dict:
    """Celesto prints a telemetry banner before the JSON envelope, so parse from the first '{'."""
    idx = stdout.find("{")
    if idx != -1:
        try:
            payload, _ = json.JSONDecoder().raw_decode(stdout[idx:])
            if isinstance(payload, dict):
                payload.setdefault("ok", returncode == 0)
                return payload
        except json.JSONDecodeError:
            pass
    return {"ok": returncode == 0, "exit_code": returncode,
            "data": {"stdout": stdout, "stderr": stderr},
            "error": (stderr.strip() or None) if returncode else None}


def _err_text(payload: dict) -> str:
    """Celesto errors are objects: {"code": ..., "message": ...}."""
    err = payload.get("error")
    if isinstance(err, dict):
        return f"{err.get('code', 'error')}: {err.get('message', '')}".strip()
    return str(err) if err else ""


def _extract_id(data) -> str | None:
    """Find the computer id in a create envelope; the exact key is not documented."""
    if isinstance(data, str):
        return data.strip() if data.strip() and " " not in data.strip() else None
    if isinstance(data, dict):
        for key in ("vm_id", "id", "computer_id", "name"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key]
        for value in data.values():
            if isinstance(value, dict):
                found = _extract_id(value)
                if found:
                    return found
    return None


class CelestoCliProvider(Provider):
    """Celesto Cloud does not let callers choose a computer name (`-n` is rejected), so the
    agent keeps its own unique `sbx-...` names for logs/reports and maps each one to the id
    Celesto assigns. Local runtime accepts --name, so there the two are identical."""

    name = "celesto"

    def __init__(self, binary: str = "celesto"):
        self.binary = binary
        self.cloud = os.environ.get("CELESTO_PROVIDER", "cloud").lower() == "cloud"
        # The CLI defaults to --local, so the scope flag must always be passed explicitly.
        self.scope = "--cloud" if self.cloud else "--local"
        self.mode = "celesto-cloud" if self.cloud else "celesto-local"
        self._ids: dict[str, str] = {}      # our sbx-name -> real computer id
        self._lock = threading.Lock()

    def _real(self, name: str) -> str:
        return self._ids.get(name, name)

    def _call(self, args: list[str], timeout: int = 180) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "LANG") or k.startswith("CELESTO_")}
        env.setdefault("CELESTO_NO_TELEMETRY", "1")
        cmd = [self.binary, "computer", args[0], self.scope, *args[1:]]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"celesto CLI failed: {exc}") from exc
        payload = _parse_envelope(proc.stdout, proc.returncode, proc.stderr)
        if os.environ.get("CELESTO_DEBUG") == "1":
            print(f"[celesto-debug] {' '.join(cmd[:6])} -> {json.dumps(payload)[:600]}", file=sys.stderr)
        return payload

    def _try(self, variants: list[list[str]], timeout: int = 180) -> dict:
        """Run argument variants in order, falling back only on CLI usage errors
        (an optional flag this scope does not accept)."""
        payload: dict = {}
        for args in variants:
            payload = self._call(args, timeout)
            err = payload.get("error")
            if payload.get("ok") or not (isinstance(err, dict) and err.get("code") == "usage_error"):
                return payload
        return payload

    def _list_raw(self) -> list[str]:
        # `--all` is rejected for cloud scope; _try retries without it on a usage error.
        payload = self._try([["list", "--all", "--json"], ["list", "--json"]])
        data = payload.get("data")
        items = data if isinstance(data, list) else next(
            (v for v in (data or {}).values() if isinstance(v, list)), [])
        out = []
        for item in items:
            out.append((item.get("vm_id") or item.get("id") or item.get("name")) if isinstance(item, dict)
                       else str(item))
        return [n for n in out if n]

    def create(self, name: str) -> None:
        before = set(self._list_raw()) if self.cloud else set()
        base = ["create"] + ([] if self.cloud else ["--name", name])
        payload = self._try([base + ["--boot-timeout", "120", "--json"], base + ["--json"]], timeout=300)
        if not payload.get("ok"):
            raise SandboxError(f"create {name} failed: {_err_text(payload)}")
        real = _extract_id(payload.get("data"))
        if not real and self.cloud:
            new = set(self._list_raw()) - before
            real = next(iter(new)) if len(new) == 1 else None
        if not real and not self.cloud:
            real = name
        if not real:
            raise SandboxError(f"create {name} succeeded but the computer id could not be read; "
                               f"delete it manually (celesto computer list --cloud). Raw: "
                               f"{json.dumps(payload)[:300]}")
        with self._lock:
            self._ids[name] = real

    def exec(self, name: str, command: str, timeout: int = 120) -> ExecResult:
        real = self._real(name)
        tail = ["--json"]
        payload = self._try([
            ["exec", real, *tail, "--timeout", str(max(1, timeout)), "--", "sh", "-c", command],
            ["exec", real, *tail, "--", "sh", "-c", command],
        ], timeout=timeout + 30)
        data = payload.get("data")
        has_result = isinstance(data, dict) and any(k in data for k in ("stdout", "stderr", "exit_code", "output"))
        if not has_result:
            if not payload.get("ok"):
                raise SandboxError(f"exec on {name} failed: {_err_text(payload)}")
            data = {"stdout": "" if data is None else str(data)}
        code = data.get("exit_code", payload.get("exit_code", 0))
        stdout = data.get("stdout", data.get("output", ""))
        return ExecResult(int(code or 0), str(stdout or ""), str(data.get("stderr", "") or ""))

    def stop(self, name: str) -> None:
        payload = self._call(["stop", self._real(name), "--json"])
        if not payload.get("ok"):
            raise SandboxError(f"stop {name} failed: {_err_text(payload)}")

    def start(self, name: str) -> None:
        real = self._real(name)
        payload = self._try([["start", real, "--boot-timeout", "120", "--json"],
                             ["start", real, "--json"]], timeout=300)
        if not payload.get("ok"):
            raise SandboxError(f"start {name} failed: {_err_text(payload)}")

    def delete(self, name: str) -> None:
        payload = self._call(["delete", self._real(name), "--json"])
        if not payload.get("ok"):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            detail = (_err_text(payload) + " " + json.dumps(data.get("failed") or [])).lower()
            if "not found" not in detail:   # already gone: deleting twice is fine
                raise SandboxError(f"delete {name} failed: {detail.strip()}")
        with self._lock:
            self._ids.pop(name, None)

    def list_names(self) -> list[str]:
        """Live computers, reported under our sbx- names where we know the mapping."""
        inverse = {real: ours for ours, real in self._ids.items()}
        return [inverse.get(real, real) for real in self._list_raw()]


# ---------------------------------------------------------------------------------------
class MockProvider(Provider):
    """Deterministic in-memory simulator. No process is spawned, no host file is touched."""

    name = "mock"
    mode = "mock"

    WRITE_RE = re.compile(r"^printf %s ([A-Za-z0-9+/=]*) \| base64 -d > (\S+)$")

    def __init__(self, seed: int = 0, fault_rate: float = 0.0, boot_fail_rate: float = 0.0):
        self.rng = random.Random(seed)
        self.fault_rate = fault_rate
        self.boot_fail_rate = boot_fail_rate
        self.vms: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    # virtual clock (per thread) so simulated latency shows up in measured durations
    def _advance(self, seconds: float) -> None:
        self._local.offset = getattr(self._local, "offset", 0.0) + seconds

    def now(self) -> float:
        return time.monotonic() + getattr(self._local, "offset", 0.0)

    def _rand(self, lo: float, hi: float) -> float:
        with self._lock:
            return self.rng.uniform(lo, hi)

    def _roll(self, p: float) -> bool:
        with self._lock:
            return self.rng.random() < p

    def create(self, name: str) -> None:
        self._advance(self._rand(0.35, 0.9))
        if self._roll(self.boot_fail_rate):
            raise SandboxError("boot probe timed out (simulated)")
        with self._lock:
            if name in self.vms:
                raise SandboxError(f"name collision: {name}")
            self.vms[name] = {"fs": {}, "state": "running"}

    def _vm(self, name: str) -> dict:
        vm = self.vms.get(name)
        if vm is None:
            raise SandboxError(f"sandbox not found: {name}")
        if vm["state"] != "running":
            raise SandboxError(f"sandbox {name} is stopped")
        return vm

    def exec(self, name: str, command: str, timeout: int = 120) -> ExecResult:
        vm = self._vm(name)
        slow = "pytest -x" in command
        self._advance(self._rand(1.2, 2.4) if slow else self._rand(0.05, 0.35))
        if self._roll(self.fault_rate):
            raise SandboxError("guest-agent timeout (simulated)")
        fs = vm["fs"]

        m = self.WRITE_RE.match(command)
        if m:
            fs[m.group(2)] = base64.b64decode(m.group(1)).decode()
            return ExecResult(0)
        if command.startswith("mkdir -p"):
            return ExecResult(0)
        if command.startswith("python3 --version"):
            return ExecResult(0, "Python 3.11.9\n")
        if "pytest --version" in command:
            return ExecResult(0, "pytest 8.3.2\n")
        if "pytest -x" in command:
            return self._pytest(fs)
        if command.startswith("cat "):
            path = command.split()[1]
            return ExecResult(0, fs[path]) if path in fs else ExecResult(1, "", f"cat: {path}: No such file")
        if command.startswith("printenv"):
            return ExecResult(0, "absent\n") if "|| echo absent" in command else ExecResult(1)
        if command.startswith("echo "):
            return ExecResult(0, command[5:].replace("$?", "0").strip("'\"") + "\n")
        return ExecResult(0)

    def _pytest(self, fs: dict) -> ExecResult:
        module = fs.get("/tmp/work/slugify.py")
        test = fs.get("/tmp/work/test_repro.py")
        if test is None or "def test_" not in test:
            out, code = "no tests ran in 0.01s\n", 5
        elif module is None or "from slugify import" not in test:
            out, code = "E   ModuleNotFoundError: No module named 'slugify'\n", 2
        elif 'replace(" ", "-")' in module:
            out = ("F\n=== FAILURES ===\n___ test_collapses_repeated_whitespace ___\n"
                   '>       assert slugify("Hello   World") == "hello-world"\n'
                   "E       AssertionError: assert 'hello---world' == 'hello-world'\n1 failed in 0.03s\n")
            code = 1
        else:
            out, code = ".\n1 passed in 0.01s\n", 0
        fs["/tmp/work/result.txt"] = out
        return ExecResult(0, f"{code}\n")

    def stop(self, name: str) -> None:
        self._advance(0.2)
        if name in self.vms:
            self.vms[name]["state"] = "stopped"

    def start(self, name: str) -> None:
        self._advance(0.4)
        if name not in self.vms:
            raise SandboxError(f"sandbox not found: {name}")
        self.vms[name]["state"] = "running"

    def delete(self, name: str) -> None:
        self._advance(0.15)
        with self._lock:
            self.vms.pop(name, None)

    def list_names(self) -> list[str]:
        with self._lock:
            return sorted(self.vms)


def make_provider(executor: str, seed: int = 7, faulty: bool = True) -> Provider:
    if executor == "mock":
        return MockProvider(seed=seed, fault_rate=0.012 if faulty else 0.0,
                            boot_fail_rate=0.03 if faulty else 0.0)
    return CelestoCliProvider()