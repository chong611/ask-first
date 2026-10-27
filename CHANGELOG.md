# Changelog

Product changes only.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: [SemVer](https://semver.org/).

## [Unreleased]

## [1.0.1] - 2026-10-27

### Fixed
- The hook now reads input that starts with a UTF-8 BOM. Before, such input (Windows PowerShell 5.1 pipes add one) was dropped silently, so a call that should ask got no decision.

## [1.0.0] - 2026-10-27

First versioned release. It contains everything built in milestones M1 to M4.

### Added
- Core rules (`src/rules/core.md`) with a 6,200-byte budget for the always-on text, plus two optional profiles: `lang-zh-CN` and `advisor`.
- Generator (`tools/build.py`): one source, with generated `dist/` for global and project installs.
- Installer (`tools/install.py`):
  - global and project installs;
  - a diff before every write;
  - merged settings and timestamped backups;
  - `check` and `uninstall`;
  - `--profile` to opt in to profiles, kept on later updates;
  - the installed version and commit, recorded in `~/.claude/agent-playbook/installed.json`.
- Claude Code hooks (`src/hooks/`):
  - Rule 2.3: tier-2 actions (deleting user data, history rewrites, force pushes, PRs and publishing, scheduling, edits to the agent's own settings) ask first.
  - Rule 5.1: secret reads and secret prints ask first.
  - Rule 1.3: a turn that changed something must end with the report.
  - Rule 2.2: decisions must be asked as structured questions.
  - Reminders for exit codes hidden by a pipe and for PowerShell `&&`.
  - Log-only checks for deferrals and for English replies under `lang-zh-CN`.
  - After compaction, open items are restated.
- Skills: `pb-status`, `pb-rehearse`, `pb-handoff`, and `lessons`. Outside the playbook repo, `lessons` only notes the lesson in `~/.claude/agent-playbook/lessons-inbox.md`.
- Measurement:
  - transcript grader `tools/grade.py` (L1 deterministic, L2 judged);
  - with/without runner `tools/compare.py`;
  - raw API reference adapter `adapters/api/`.
- Cloud environment setup script (`tools/cloud-setup.sh`).
- L0 checks and CI on Linux and Windows; pre-commit secret scan.

### Changed
- Profiles are opt-in; a fresh install is core only.
- A personal profile now lives in `~/.claude/rules/owner.md`, the user's own file. The first install moves an old `src/profiles/owner.md` there.
