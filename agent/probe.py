"""Smoke test against real Celesto: create -> exec -> delete, printing the RAW JSON envelopes.

    docker compose exec api python -m agent.probe

If anything fails, paste the full output back so the provider parsing can be adjusted.
"""
from __future__ import annotations

import json
import os
import sys

from .providers import CelestoCliProvider, SandboxError
from .task import new_name


def main() -> int:
    os.environ.setdefault("CELESTO_DEBUG", "1")
    provider = CelestoCliProvider()
    name = new_name()
    print(f"scope={provider.scope} mode={provider.mode} name={name}")
    try:
        provider.create(name)
        print(f"create: OK  (celesto id: {provider._real(name)})")
        for cmd in ("echo hello", "python3 --version", "exit 3"):
            raw = provider._call(["exec", provider._real(name), "--json", "--timeout", "30", "--", "sh", "-c", cmd])
            print(f"RAW exec {cmd!r}: {json.dumps(raw)[:500]}")
            print(f"PARSED: {provider.exec(name, cmd)}")
        print("list_names:", provider.list_names())
        return 0
    except SandboxError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        try:
            provider.delete(name)
            print("delete: OK")
        except SandboxError as exc:
            print(f"delete FAILED: {exc} (remove it manually: celesto computer list --cloud, then delete the leftover)")


if __name__ == "__main__":
    sys.exit(main())