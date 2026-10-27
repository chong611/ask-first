# Contributing

## Setup
No dependencies beyond Python 3.10+ (standard library only). The paid measurement tools also need `pip install anthropic`.

```sh
python tools/build.py                   # regenerate dist/ from src/
python tools/check.py                   # L0: stale dist, byte budget, headings, hidden Unicode, secrets, paths, public files
python -m unittest discover -s tests    # all tests; never touch the network
git config core.hooksPath hooks/git     # pre-commit: secret scan and stale dist/
```

On Windows, use `python`, not `python3`. CI runs on Linux and Windows.

## Rules for changes
- **Edit `src/`, never `dist/`.** CI fails when `dist/` differs from a fresh build.
- **Byte budget.** The always-on text (core plus profiles, HTML comment lines excluded) stays at or under 6,200 bytes. To add a sentence, remove one.
- **A rule needs evidence.** Name the real failure it prevents. Prefer a hook or a test over prose; prose is the last resort.
- **A hook change needs both directions tested:**
  - the risky form asks;
  - the ordinary form still runs without a prompt. Over-asking counts as a failure.
- **Be honest in docs.** `docs/ENFORCEMENT.md` says what is enforced and what is not. Update it with the change.
- **Record product changes** under `## [Unreleased]` in `CHANGELOG.md`.

## Releases
1. Bump `VERSION` (SemVer).
2. Move the Unreleased notes under the new version in `CHANGELOG.md`.
3. Tag `vX.Y.Z` on `main`.

`install.py` prints the version it installs and records it in `~/.claude/agent-playbook/installed.json`.
