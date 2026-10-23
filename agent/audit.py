"""Append-only command audit log. Every attempt is a line, including blocked ones."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

EXECUTION_TARGET = "celesto_sandbox"


class Audit:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.path.write_text("")

    def log(self, sandbox_name: str, command: str, decision: str, reason: str | None = None) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sandbox_name": sandbox_name,
            "command": (command or "(empty)")[:4000],
            "decision": decision,
            "execution_target": EXECUTION_TARGET,
        }
        if reason:
            record["reason"] = reason
        with self._lock, self.path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")


class TraceWriter:
    """JSON-lines event trace for state/failure analysis."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.path.write_text("")

    def write(self, event: str, **fields) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self._lock, self.path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
