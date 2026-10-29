# Changelog

Product changes only.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: [SemVer](https://semver.org/).

## [Unreleased]

## [1.1.2] - 2026-10-29

### Fixed
- A wildcard path (`C:/*/*/*/x.txt`) checks the time limit before reading each folder, not once per path level, so a slow disk can no longer hold the check past Claude Code's 10-second hook timeout, where the call would run without asking. A check cut short still asks.
- The tests and `publish_public.py` remove the temp folders they create; each run used to leave about 40 to 90 behind.

## [1.1.1] - 2026-10-28

### Fixed
- `check.py` counts the live personal profile (`~/.claude/rules/owner.md`) before the old copy in `src/profiles/`, which could be stale and hide an over-budget profile.
- `publish_public.py` runs the public tree's checks with an empty home, so they never depend on the maintainer's personal profile.

## [1.1.0] - 2026-10-28

### Added
- Other agents: Codex, Cursor and Gemini CLI, through `install.py global --client codex|cursor|gemini` and one adapter per agent (`src/hooks/client_<name>.py`, `tools/target_<name>.py`). Unit-tested with payloads shaped from each agent's docs; not yet live-tested.
- Approval codes for agents that cannot show an approval prompt: the risky call is denied with a four-character code, and only the user's own `approve CODE` / `批准 CODE` lets that exact call through once.
- `tools/deepseek.py`: starts Claude Code on DeepSeek's Anthropic-compatible API, so every hook still applies; the key is never stored.
- `tools/publish_public.py` and `public/overlay/` build the public repo from the private one.

### Changed
- The policy treats Codex, Cursor and Gemini instruction, hook, rule and settings files like Claude Code's: changing them asks first.

### Fixed
- Fail closed: a crashed hook (a broken policy file or adapter included) or a check about to pass the agent's timeout now denies before-tool calls, in each agent's own format, instead of letting them through.

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
