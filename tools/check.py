"""L0 checks: run in CI on every push, need no API key.

1. dist/ matches a fresh build of src/
2. always-on text fits the byte budget (HTML comment lines excluded)
3. report headings in core and lang-zh-CN match src/hooks/headings.py
4. no hidden or bidirectional Unicode in src/
5. no secret-like strings in tracked files
6. every tracked path is valid on Windows (no : * ? " < > |, no reserved names)
7. no private markers in files bound for the public repo (tools/public-allowlist.txt)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "hooks"))

import build  # noqa: E402
import headings  # noqa: E402
import secret_scan  # noqa: E402

ROOT = build.ROOT
BUDGET_BYTES = 6200  # raised from 6,000 by DECISIONS Q67; adding still means replacing

# Zero-width, bidi controls and other invisible characters used to hide text.
HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def always_on_bytes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        for line in build.read(path).splitlines(keepends=True):
            if not line.startswith("<!--"):
                total += len(line.encode("utf-8"))
    return total


def owner_candidates() -> list[Path]:
    """Where a personal profile may live on this machine; the first one present is counted. The live file
    comes first: the old copy in src/profiles (before Q155) can be stale and hid an over-budget profile."""
    return [Path.home() / ".claude" / "rules" / "owner.md", build.SRC / "profiles" / "owner.md"]


def budget_paths(owners: list[Path] | None = None) -> list[Path]:
    """owners=None looks on this machine; tests pass a list so the result does not depend on it."""
    profiles = build.SRC / "profiles"
    paths = [build.SRC / "rules" / "core.md"]
    paths += [profiles / f"{p}.md" for p in build.PUBLIC_PROFILES]
    for owner in owner_candidates() if owners is None else owners:  # personal, counted when present
        if owner.exists():
            paths.append(owner)
            break
    return paths


def check_budget(owners: list[Path] | None = None) -> list[str]:
    used = always_on_bytes(budget_paths(owners))
    if used > BUDGET_BYTES:
        return [f"always-on text is {used} bytes, budget {BUDGET_BYTES}"]
    return []


def check_headings() -> list[str]:
    errors = []
    core = build.core()
    zh = build.profile("lang-zh-CN")
    if " · ".join(headings.EN) not in core:
        errors.append("core report headings differ from src/hooks/headings.py EN")
    if " · ".join(headings.ZH) not in zh:
        errors.append("lang-zh-CN report headings differ from src/hooks/headings.py ZH")
    return errors


def check_hidden() -> list[str]:
    errors = []
    for path in sorted(build.SRC.rglob("*.md")):
        for n, line in enumerate(build.read(path).splitlines(), 1):  # a leading BOM is not hidden text
            if HIDDEN.search(line):
                errors.append(f"hidden Unicode: {path.relative_to(ROOT)}:{n}")
    return errors


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [ROOT / name for name in out.decode().split("\0") if name]


WINDOWS_BAD = re.compile(r'[:*?"<>|\\]|[. ]$')
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.I)


def portable_path_errors(paths: list[str]) -> list[str]:
    errors = []
    for rel in paths:
        for part in rel.split("/"):
            if WINDOWS_BAD.search(part) or WINDOWS_RESERVED.match(part):
                errors.append(f"path not valid on Windows: {rel!r}")
                break
    return errors


def check_portable() -> list[str]:
    return portable_path_errors([p.relative_to(ROOT).as_posix() for p in tracked_files()])


def check_secrets() -> list[str]:
    return [f"secret-like string: {hit}" for hit in secret_scan.scan(tracked_files(), ROOT)]


# Markers of the owner's private life that must stay out of the public file list (Q152).
# Generic patterns live here. Personal terms live only in MARKER_FILES, which are never
# published: docs/private-markers.txt (tracked in the private repo, not in the allowlist)
# and the gitignored private/denylist.txt (Q46, same format as for tools/secret_scan.py).
# Without either file (the public repo's CI) only the generic patterns are checked.
GENERIC_MARKERS = re.compile(r"@gmail\.com|LAPTOP-[A-Z0-9]", re.I)
MARKER_FILES = (ROOT / "docs" / "private-markers.txt", ROOT / "private" / "denylist.txt")


def marker_terms(files: list[Path] | tuple[Path, ...] | None = None) -> list[str]:
    """Plain terms, one per line; blank lines and lines starting with # are skipped."""
    terms = []
    for path in MARKER_FILES if files is None else files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            continue
        terms += [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    return terms


def private_markers(files: list[Path] | tuple[Path, ...] | None = None) -> re.Pattern | None:
    """The personal terms as one case-insensitive pattern, or None when no marker file exists.
    A space in a term also matches a run of spaces, "_" or "-"."""
    terms = marker_terms(files)
    if not terms:
        return None
    alts = sorted({r"[\s_-]+".join(re.escape(w) for w in t.split()) for t in terms}, key=len, reverse=True)
    return re.compile("|".join(alts), re.I)


def scan_markers(paths: list[Path], pattern: re.Pattern | None, root: Path | None = None) -> list[str]:
    """One error per line holding a generic marker or a match of pattern (from private_markers).
    A personal term is reported as "personal term", never spelled out."""
    root = ROOT if root is None else root
    errors = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        for n, line in enumerate(text.splitlines(), 1):
            labels = [m.group(0) for m in GENERIC_MARKERS.finditer(line)]
            if pattern is not None and pattern.search(line):
                labels.append("personal term")
            errors += [f"private marker ({label}) in public file {rel}:{n}" for label in labels]
    return errors


def public_files() -> list[Path]:
    import fnmatch
    globs = [l.strip() for l in (ROOT / "tools" / "public-allowlist.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    tracked = tracked_files()
    overlays = {p.relative_to(ROOT).as_posix() for p in tracked}
    files = []
    for path in tracked:
        rel = path.relative_to(ROOT).as_posix()
        if any(fnmatch.fnmatch(rel, g) or (g.endswith("/**") and rel.startswith(g[:-2])) for g in globs):
            # what is published: a version under public/overlay/ replaces the original (tools/publish_public.py)
            files.append(ROOT / f"public/overlay/{rel}" if f"public/overlay/{rel}" in overlays else path)
    return files


def check_public() -> list[str]:
    """Every public file, this one included."""
    return scan_markers(public_files(), private_markers())


def main() -> int:
    errors = [f"stale: dist/{rel}" for rel in build.stale()]
    errors += check_budget() + check_headings() + check_hidden() + check_secrets() + check_portable()
    errors += check_public()
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"L0 ok ({always_on_bytes(budget_paths())}/{BUDGET_BYTES} bytes always-on)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
