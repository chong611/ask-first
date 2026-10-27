# Capabilities

Core rules name capabilities, not tools. This table maps each capability to its implementation in each adapter. Status: **done** (built and tested here), **planned** (milestone), or **docs only** (per vendor docs, never run).

| Capability | Claude Code | Raw API (reference adapter) | Status |
|---|---|---|---|
| Always-on rules | `CLAUDE.md` → `@AGENTS.md` (project); `~/.claude/rules/agent-playbook.md` (global) | System prompt assembled from `src/` | Generated files: **done**. Loading: **live-tested** (2026-10-23) |
| Structured questions (2.2) | AskUserQuestion tool | Numbered questions in the reply; the client renders the options | Claude Code: the Stop hook requires an AskUserQuestion call, by ID, for each Waiting on you item and for a decision in the text (Q78, Q89A, Q90A): **done**, live-tested (2026-10-26). Raw API: the adapter retries once when an item or a decision has no Q-ID: **done** (unit-tested) |
| Tier-2 block (2.3) | `permissions.ask` + PreToolUse hook + git hooks + branch protection | Policy check before each tool call | Claude Code: **done**, force-push live-tested. Raw API: planned (M4) |
| Report check (1.3) | Stop hook using `src/hooks/headings.py` | Post-response check | **done**, live-tested |
| Re-inject open items after compaction (Q41) | SessionStart `matcher: compact`; InstructionsLoaded check | n/a | **done** (unit-tested; compaction not live-tested) |
| Secret guard (5.1) | PreToolUse + `hooks/git/pre-commit` + CI | Output filter | **done** (unit-tested) |
| Skills (`/pb-rehearse`, `/pb-handoff`, `/pb-status`, `lessons`) | `.claude/skills/` | Prompt templates | **done** (format only; triggering not live-tested) |
| Transcript for grading | Hook logs (tier2, deferral) | Client logs JSONL | partial: hook logs done; full transcripts planned (M4) |
