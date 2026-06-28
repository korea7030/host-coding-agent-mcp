from __future__ import annotations

import re


class SecurityViolation(ValueError):
    pass


SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:api[_-]?key|token|bot_token|client_secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+\S+"),
    re.compile(r"(?i)\bx-api-key\s*:\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{20,}\b"),
]

DANGEROUS_PATTERNS = [
    re.compile(r"(?i)\brm\s+-rf\s+/"),
    re.compile(r"(?i)\bsudo\s+rm\b"),
    re.compile(r"(?i)\bchmod\s+-R\s+777\b"),
    re.compile(r"(?i)\bchown\s+-R\b"),
    re.compile(r"(?i)\b(?:curl|wget)\b[^\n|]{0,500}\|\s*(?:sh|bash)\b"),
    re.compile(r"(?i)\bdd\s+if="),
    re.compile(r"(?i)\bmkfs(?:\.\w+)?\b"),
    re.compile(r"(?i)\bsecurity\s+dump-keychain\b"),
]

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def validate_task(task: str) -> None:
    if not task.strip():
        raise SecurityViolation("task must not be empty")
    if any(pattern.search(task) for pattern in SECRET_PATTERNS):
        raise SecurityViolation("task contains secret-like content")
    if any(pattern.search(task) for pattern in DANGEROUS_PATTERNS):
        raise SecurityViolation("task contains a blocked command pattern")


def redact(text: str, max_chars: int = 200_000) -> tuple[str, bool]:
    clean = ANSI_ESCAPE.sub("", text)
    changed = clean != text
    for pattern in SECRET_PATTERNS:
        clean, count = pattern.subn("[REDACTED]", clean)
        changed = changed or count > 0
    if len(clean) > max_chars:
        clean = clean[:max_chars] + "\n[OUTPUT TRUNCATED]"
        changed = True
    return clean, changed
