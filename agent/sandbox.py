"""GuardedSandbox: the single choke point every agent tool call passes through.

hook (may block) -> audit line (allow/block) -> Celesto provider. Nothing else executes.
"""
from __future__ import annotations

import base64

from .audit import Audit
from .hooks import CommandBlockedError, SafetyHook
from .providers import ExecResult, Provider


class GuardedSandbox:
    def __init__(self, provider: Provider, name: str, hook: SafetyHook, audit: Audit):
        self.provider = provider
        self.name = name
        self.hook = hook
        self.audit = audit

    def run(self, command: str, timeout: int = 120) -> ExecResult:
        try:
            self.hook.before(command)
        except CommandBlockedError as exc:
            self.audit.log(self.name, command, "block", exc.reason)
            raise
        self.audit.log(self.name, command, "allow")
        return self.provider.exec(self.name, command, timeout)

    def write_file(self, path: str, content: str) -> ExecResult:
        payload = base64.b64encode(content.encode()).decode()
        return self.run(f"printf %s {payload} | base64 -d > {path}")
