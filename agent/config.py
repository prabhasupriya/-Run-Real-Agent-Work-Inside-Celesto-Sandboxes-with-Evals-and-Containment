"""Environment-driven configuration. Every variable read here is listed in .env.example."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def reports_dir() -> Path:
    return Path(env("REPORTS_DIR", str(ROOT / "reports")))


def logs_dir() -> Path:
    return Path(env("LOGS_DIR", str(ROOT / "logs")))


def allowed_hosts() -> list[str]:
    raw = env("ALLOWED_HOSTS", "pypi.org,files.pythonhosted.org")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def price_per_hour() -> float:
    return float(env("CELESTO_PRICE_PER_HOUR", "0.015"))


def llm_configured() -> bool:
    key = env("LLM_API_KEY")
    base = env("LLM_BASE_URL")
    return bool(key) and key != "replace-me" and bool(base) and "example-llm-host" not in base
