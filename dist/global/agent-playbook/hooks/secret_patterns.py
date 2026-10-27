"""Secret-like string patterns, shared by the PreToolUse hook (rule 5.1),
tools/secret_scan.py (pre-commit) and tools/check.py (CI)."""

import re

PATTERNS = {
    "anthropic-key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai-style-key": r"\bsk-[A-Za-z0-9]{20,}",
    "github-token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "aws-access-key": r"\bAKIA[0-9A-Z]{16}\b",
    "google-api-key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "slack-token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "assigned-secret": r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
}
COMPILED = {name: re.compile(rx) for name, rx in PATTERNS.items()}


def find(text: str) -> list[str]:
    """Names of the patterns that match anywhere in text (never the values)."""
    return [name for name, rx in COMPILED.items() if rx.search(text)]
