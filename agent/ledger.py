"""Best-effort client for the Run Ledger API. A ledger outage never fails an agent run."""
from __future__ import annotations

import json
import urllib.request

PROBLEM_ID = "issue-to-failing-test"


class Ledger:
    def __init__(self, base_url: str | None):
        self.base_url = base_url.rstrip("/") if base_url else None

    def post(self, sandbox_name: str, state: str) -> None:
        if not self.base_url:
            return
        body = json.dumps({
            "run_id": f"run-{sandbox_name}", "sandbox_name": sandbox_name,
            "problem_id": PROBLEM_ID, "state": state, "execution_target": "celesto_sandbox",
        }).encode()
        req = urllib.request.Request(f"{self.base_url}/api/runs", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass
