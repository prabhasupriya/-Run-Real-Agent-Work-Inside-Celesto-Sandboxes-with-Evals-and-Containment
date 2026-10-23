"""Safety hook: code that runs BEFORE the guest sees a command and can block it.

This is defence in depth, not the isolation boundary. The boundary is the microVM plus the
design rule that no real secret is ever injected into a guest. The hook is heuristic: it
catches the obvious exfiltration, secret-reading and destructive patterns and, importantly,
leaves an audit line for every attempt.
"""
from __future__ import annotations

import re
import shlex
from urllib.parse import urlparse

from .config import allowed_hosts


class CommandBlockedError(Exception):
    def __init__(self, reason: str, command: str):
        super().__init__(reason)
        self.reason = reason
        self.command = command


NET_TOOLS = {"curl", "wget"}
RAW_NET_TOOLS = {"nc", "ncat", "netcat", "socat", "telnet", "ftp", "scp", "sftp", "ssh", "rsync"}
SHELLS = {"sh", "bash", "dash", "zsh"}
PYTHONS = {"python", "python3"}
PROTECTED_TARGETS = {"/", "/*", "~", "~/", "$HOME", "${HOME}", "/home", "/root", "/etc", "/usr", "/bin", "/var"}
ARG_TAKES_VALUE = {"-o", "-O", "-d", "-H", "-u", "-X", "-A", "-T", "-e", "--output", "--data",
                   "--header", "--user", "--request", "--user-agent", "--data-binary", "--post-data"}

SECRET_VAR = re.compile(r"\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)\w*", re.I)
SENSITIVE_PATH = re.compile(
    r"""(?:^|[\s/'"])\.env(?:\.\w+)?(?=$|[\s'"|;&])"""
    r"|/proc/(?:self|\d+)/environ|\.ssh/|\.aws/|\.netrc|id_rsa"
)
FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{")
PY_NET = re.compile(r"socket|urllib|http\.client|requests|urlopen|ftplib|smtplib")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _host_allowed(host: str | None, allowed: list[str]) -> bool:
    if not host:
        return False
    host = host.lower()
    return any(host == a or host.endswith("." + a) for a in allowed)


class SafetyHook:
    def __init__(self, allowed: list[str] | None = None, canaries: list[str] | None = None,
                 canary_vars: list[str] | None = None):
        self.allowed = [a.lower() for a in (allowed if allowed is not None else allowed_hosts())]
        self.canaries = [c for c in (canaries or []) if c]
        self.canary_vars = canary_vars or []

    # -- public API -------------------------------------------------------------------
    def before(self, command: str) -> None:
        reason = self.inspect(command)
        if reason:
            raise CommandBlockedError(reason, command)

    def inspect(self, command: str, _depth: int = 0) -> str | None:
        if not command or not command.strip():
            return "empty command"
        if _depth > 3:
            return "shell nesting too deep"

        for canary in self.canaries:
            if canary in command:
                return "canary secret value present in command"
        for var in self.canary_vars:
            if re.search(r"\$\{?" + re.escape(var) + r"\b", command):
                return f"reference to canary variable ${var}"
        if SECRET_VAR.search(command):
            return "references a secret-looking environment variable"
        if SENSITIVE_PATH.search(command):
            return "touches a sensitive path (.env, environ, ssh/aws credentials)"
        if FORK_BOMB.search(command):
            return "fork bomb pattern"

        top = _tokens(command)
        if top and top[0].rsplit("/", 1)[-1] in SHELLS and "-c" in top:
            idx = top.index("-c")
            if idx + 1 < len(top):
                inner = self.inspect(top[idx + 1], _depth + 1)
                if inner:
                    return f"nested shell: {inner}"

        for segment in re.split(r"\|\||&&|;|\||\n", command):
            reason = self._segment(segment.strip())
            if reason:
                return reason
        return None

    # -- internals --------------------------------------------------------------------
    def _segment(self, segment: str) -> str | None:
        toks = _tokens(segment)
        if not toks:
            return None
        cmd = toks[0].rsplit("/", 1)[-1]

        if cmd == "rm":
            flags = [t for t in toks[1:] if t.startswith("-")]
            recursive = "--recursive" in flags or any(
                "r" in f.lower() for f in flags if not f.startswith("--"))
            targets = [t for t in toks[1:] if not t.startswith("-")]
            if "--no-preserve-root" in toks or (recursive and any(t in PROTECTED_TARGETS for t in targets)):
                return "destructive rm on a protected path"
        if cmd.startswith("mkfs") or cmd == "shred":
            return f"destructive command: {cmd}"
        if cmd == "dd" and any(t.startswith("of=/dev/") for t in toks):
            return "dd writing to a block device"

        if cmd in RAW_NET_TOOLS:
            return f"raw network tool not permitted: {cmd}"

        if cmd in PYTHONS and "-c" in toks and PY_NET.search(segment):
            return "python one-liner using network modules"

        if cmd in NET_TOOLS:
            skip = False
            found = False
            for tok in toks[1:]:
                if skip:
                    skip = False
                    continue
                if tok in ARG_TAKES_VALUE:
                    skip = True
                    continue
                if tok.startswith("-"):
                    continue
                if "$" in tok or "`" in tok:
                    return "network request with dynamic (unverifiable) target"
                if "://" in tok or re.match(r"^[\w.-]+\.[a-z]{2,}(?:[/:]|$)", tok) \
                        or re.match(r"^\d+\.\d+\.\d+\.\d+", tok):
                    found = True
                    host = urlparse(tok if "://" in tok else "//" + tok).hostname
                    if not _host_allowed(host, self.allowed):
                        return f"outbound request to non-allow-listed host: {host}"
            if not found:
                return f"{cmd} without a verifiable allow-listed target"
        return None
