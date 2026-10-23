"""Test generation. LLM output is untrusted text: it is only ever written into a guest file."""
from __future__ import annotations

import json
import re
import urllib.request

from .config import env, llm_configured

TEMPLATE = '''from slugify import slugify


def test_collapses_repeated_whitespace():
    assert slugify("Hello   World") == "hello-world"
'''

PROMPT = (
    "Write a minimal pytest file named test_repro.py that reproduces this bug. "
    "It must start with 'from slugify import slugify', contain exactly one test function, "
    "and use a plain assert. Return only Python code, no commentary.\n\nBug report:\n{report}"
)
FORBIDDEN = re.compile(r"\b(subprocess|socket|requests|urllib|os\.system|shutil|open\()")


def _valid(code: str) -> bool:
    return "from slugify import" in code and "def test_" in code and not FORBIDDEN.search(code)


def generate_test(bug_report: str, use_llm: bool) -> tuple[str, str]:
    if use_llm and llm_configured():
        try:
            body = json.dumps({
                "model": env("LLM_MODEL"),
                "messages": [{"role": "user", "content": PROMPT.format(report=bug_report)}],
                "temperature": 0,
            }).encode()
            req = urllib.request.Request(
                env("LLM_BASE_URL").rstrip("/") + "/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {env('LLM_API_KEY')}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = json.load(resp)["choices"][0]["message"]["content"]
            code = re.sub(r"^```(?:python)?\s*|\s*```$", "", text.strip())
            if _valid(code):
                return code + "\n", "llm"
        except Exception:
            pass
    return TEMPLATE, "template"
