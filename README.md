# ask-first

Rules for coding agents that ask before risky actions, and the tools to measure whether those rules actually work. The installed package is called `agent-playbook`. Version: see `VERSION` and `CHANGELOG.md`.

Most rule sets (CLAUDE.md, AGENTS.md, skill packs) are prose that the model may or may not follow. This repo does three things:

1. **One source, generated entry files.** Rules live once in `src/`. `tools/build.py` writes the files each agent reads. CI fails if the generated files drift from the source.
2. **Enforcement where possible, labelled where not.** Every rule is marked as hard-blocked by a hook, partly blocked, or advisory only (`docs/ENFORCEMENT.md`).
3. **Behaviour you can measure.** Real corrections made to an agent become test cases. You then compare agent behaviour with and without the playbook.

The rules in `src/` are a reference example. They were mined from one person's real sessions and confirmed rule by rule. They are not a claim about what is best for everyone.

## Result

Measured on the raw Claude API in an 84-run comparison (US$2.14 in all). With the rules, the agent asked before deleting untracked data in 10 of 10 runs; without them, in 0 of 10. Small samples, one model asked (10 of the 84 runs were answered by its fallback model), toy repos: the report says what these numbers can and cannot show. Report: `evidence/live/full-84-review.md`.

Not measured yet: skill triggering, re-injection after compaction, and the with/without comparison inside Claude Code itself.

## 中文说明

- **做什么**：让装了它的地方（你的电脑，或某个云端环境）里每个 Claude Code 会话都照同一套规矩做事：
  - 有证据才说"完成"；
  - 改了东西，用五个标题汇报；
  - 删数据、强推、发 PR 之前先问你；
  - 可选：用中文回复（`lang-zh-CN` profile）。
- **安装或更新**：
  1. 在这个 repo 的文件夹里先 `git pull`；
  2. 运行 `python tools\install.py global`，看它列出的变化；
  3. 确认没问题后，再加 `--yes` 运行一次。
- **云端**：
  1. 把 `tools/cloud-setup.sh` 的全部内容贴进云端环境设置里的 Setup script；
  2. 把这个 repo 加进该环境的 Repositories。
- **卸载**：先运行 `python tools\install.py uninstall global` 看会删什么，再加 `--yes`。它只删它自己装的东西，你自己的设置会保留。
- **认出它的会话**：playbook 的会话标题以 `[playbook]` 开头。在别的项目里纠正 agent 时，它只会在 `~/.claude/agent-playbook/lessons-inbox.md` 记一笔，不会跑去改 playbook。

## Layout

| Path | What |
|---|---|
| `src/rules/core.md` | Core rules, always on (English) |
| `src/profiles/*.md` | Opt-in profiles |
| `dist/` | Generated. Do not edit. |
| `tools/` | `build.py`, `check.py` (L0), `secret_scan.py`, `install.py`, `grade.py` (L1/L2), `compare.py` (with/without) |
| `adapters/api/` | Reference adapter: the same rules and checks on the raw Claude API |
| `tests/scenarios.json` | With/without scenarios |
| `hooks/git/pre-commit` | Blocks secret-like strings and a stale `dist/` |
| `docs/` | Capability map and enforcement table |
| `evidence/live/` | The with/without measurement above |

## Use (Claude Code)

Every command first prints a diff and writes nothing. Re-run it with `--yes` to apply.

```sh
python tools/install.py global                 # ~/.claude: rules, skills, hooks (this Python, absolute paths)
python tools/install.py global --profile lang-zh-CN --profile advisor   # optional profiles; updates keep them
python tools/install.py project PATH           # a repo on this machine; hooks go to .claude/settings.local.json
python tools/install.py project PATH --cloud   # a repo used in cloud sessions; hooks go to the committed settings.json
python tools/install.py check PATH             # project core and global core must be byte-identical
python tools/install.py uninstall global       # remove what the global install added (PATH for a repo; --purge: logs too)
```

Existing CLAUDE.md, AGENTS.md and settings are merged, never replaced. Each changed file is backed up.

A fresh install has the core only. The profiles are opt-in:
- `lang-zh-CN`: reply in Simplified Chinese;
- `advisor`: lead with flaws, rate confidence.

Personal facts about you go in `~/.claude/rules/owner.md`, your own file, which Claude Code loads and the installer never edits. The installed version is in `~/.claude/agent-playbook/installed.json`.

For cloud sessions, paste `tools/cloud-setup.sh` into the environment's Setup script and select this repo when you start a session.

On Windows, use `python`, not `python3`. The `python3` command there is often a Microsoft Store stub.

## Measure

```sh
python tools/grade.py ~/.claude/projects/PROJECT/SESSION.jsonl   # L1: free, deterministic
python tools/grade.py FILE.jsonl --judge                         # L2: prints the cost; add --run to pay
python tools/compare.py                                          # with/without: prints the cost only
python tools/compare.py --run --max-usd 5                        # paid; stops before passing the cap
python adapters/api/playbook_api.py --root PATH                  # chat with the playbook on the raw API
```

The paid commands need `pip install anthropic` and an API key in `PB_ANTHROPIC_API_KEY` (its own name, so it never changes how Claude Code itself is billed). The adapter and `compare.py` run the model's shell commands on this machine: use a container or VM.

## Develop

See `CONTRIBUTING.md`. Changes are listed in `CHANGELOG.md`, and security reports go through `SECURITY.md`.

Comments and docs cite tags such as `Q102A` or `M2`. They point to the maintainer's internal decision log and milestones, which are not published; they are kept so each rule can be traced.

## License

MIT
