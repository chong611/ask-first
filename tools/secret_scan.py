"""Find secret-like strings. Used by the git pre-commit hook and by CI.

Usage: python tools/secret_scan.py [FILE ...]   (no args: staged files)
Exit 1 if anything matches. Matches print as path:line:pattern, never the value.

Personal info (DECISIONS Q46): if private/denylist.txt exists (gitignored, one
term per line: names, emails, local paths), those terms are matched too,
case-insensitively. The repo carries only this mechanism, never the list.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "hooks"))
from secret_patterns import COMPILED  # noqa: E402

# This file and its tests hold the patterns themselves.
SKIP = {"src/hooks/secret_patterns.py", "tests/test_tools.py", "tests/test_hooks.py"}


def denylist(root: Path) -> list[str]:
    path = root / "private" / "denylist.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [l.strip().lower() for l in lines if l.strip() and not l.startswith("#")]


def scan(paths: list[Path], root: Path, terms: list[str] | None = None) -> list[str]:
    terms = denylist(root) if terms is None else terms
    hits = []
    for path in paths:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        if rel in SKIP:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in COMPILED.items():
                if rx.search(line):
                    hits.append(f"{rel}:{n}:{name}")
            low = line.lower()
            if any(t in low for t in terms):
                hits.append(f"{rel}:{n}:personal-info")
    return hits


def staged(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout
    return [root / name for name in out.decode().split("\0") if name]


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    paths = [Path(a) for a in argv] if argv else staged(root)
    hits = scan(paths, root)
    for hit in hits:
        print(f"secret-like string: {hit}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
