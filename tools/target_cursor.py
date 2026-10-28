"""Install target for Cursor: python tools/install.py global --client cursor (and uninstall global).

~/.cursor/hooks.json                     our entries merged in, every other hook and key kept
                                         (https://cursor.com/docs/hooks)
~/.cursor/agent-playbook/hooks/*.py      the hook scripts, the same as ~/.claude/agent-playbook/hooks/
~/.cursor/plugins/local/agent-playbook/  the global rules as a local plugin: .cursor-plugin/plugin.json and
                                         rules/agent-playbook.mdc (alwaysApply), untested; notes() has the
                                         fallback (https://cursor.com/docs/reference/plugins)
~/.cursor/skills/<name>/SKILL.md         only skills missing from ~/.claude/skills, which Cursor reads too
                                         (https://cursor.com/docs/context/skills); no duplicates
Project rules need nothing new: Cursor reads AGENTS.md. State, logs and config.json stay in
~/.claude/agent-playbook, shared with Claude Code, so one config covers both.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

FRAGMENT = "cursor/hooks.fragment.json"
PLUGIN = "cursor/plugins/local/agent-playbook/"
MANIFEST = PLUGIN + ".cursor-plugin/plugin.json"
RULES = PLUGIN + "rules/agent-playbook.mdc"
FRONT = "---\ndescription: agent-playbook core rules, always on\nalwaysApply: true\n---\n"
HOOKS = "global/agent-playbook/hooks/"
SKILLS = "global/skills/"
# preToolUse can only deny (Cursor does not enforce "ask" there), so it gets the approval code.
FILE_TOOLS = "^(Read|Write|Edit|StrReplace|MultiEdit|ApplyPatch|Delete)$"
CHANGE_TOOLS = "^(Shell|Write|Edit|StrReplace|MultiEdit|ApplyPatch|Delete)$|^MCP:"
SAFE = re.compile(r"^[A-Za-z0-9_./:-]+$")  # a path that needs no quoting in PowerShell or a POSIX shell


def hooks_config(command: str) -> dict:
    """Permission hooks fail closed: a crash or timeout blocks the call instead of letting it through."""
    def entry(matcher: str | None = None, **opts) -> list[dict]:
        return [{"command": command, **({"matcher": matcher} if matcher else {}), **opts}]

    gate = {"timeout": 15, "failClosed": True}
    return {"version": 1, "hooks": {
        "beforeShellExecution": entry(**gate),
        "beforeMCPExecution": entry(**gate),
        "preToolUse": entry(FILE_TOOLS, **gate),
        "postToolUse": entry(CHANGE_TOOLS, timeout=10),
        "beforeSubmitPrompt": entry(timeout=10),
        "afterAgentResponse": entry(timeout=10),
        "stop": entry(timeout=15, loop_limit=5),  # the hook itself sends at most 2 follow-ups a turn
    }}


def hook_command(python: str, script: str, windows: bool) -> str:
    """Cursor runs hook commands through PowerShell on Windows and a POSIX shell elsewhere. A plain path
    runs in both (PowerShell, and the bash some Windows setups end up in); otherwise quote for PowerShell."""
    if not windows:
        return f"{shlex.quote(python)} {shlex.quote(script)} --client cursor"
    if SAFE.match(python) and SAFE.match(script):
        return f"{python} {script} --client cursor"
    return f"& {ps_quote(python)} {ps_quote(script)} --client cursor"


PS_QUOTES = "'" + "".join(map(chr, range(0x2018, 0x201C)))  # PowerShell reads the curly ones as ' too


def ps_quote(text: str) -> str:
    """A PowerShell literal: no $ or ` expansion; each quote character doubled."""
    return "'" + re.sub(f"([{PS_QUOTES}])", r"\1\1", text) + "'"


def outputs(build) -> dict[str, str]:
    """dist-relative path ("cursor/...") -> content."""
    manifest = {"name": "agent-playbook", "version": build.VERSION,
                "description": "agent-playbook core rules for every Cursor chat"}
    return {
        FRAGMENT: json.dumps(hooks_config("{COMMAND}"), indent=2) + "\n",
        MANIFEST: json.dumps(manifest, indent=2) + "\n",
        RULES: FRONT + build.HEADER + build.core(),
    }


def installed_files(build) -> dict[str, str]:
    """The part of build.outputs() this target installs, without building the other clients' targets."""
    return {**{HOOKS + name: text for name, text in build.hooks().items()},
            **{f"{SKILLS}{name}/SKILL.md": text for name, text in build.skills().items()},
            **outputs(build)}


def plan_global(home: Path, out: dict[str, str], python: str, profiles: list[str]) -> dict[Path, str | None]:
    import build
    import install
    cursor = home / ".cursor"
    hooks_dir = cursor / "agent-playbook" / "hooks"
    plan: dict[Path, str | None] = {}
    for rel, text in out.items():
        if rel.startswith(HOOKS):
            plan[hooks_dir / rel[len(HOOKS):]] = text
        elif rel == RULES:
            plan[cursor / rel[len("cursor/"):]] = text + "".join("\n" + build.profile(p) for p in profiles)
        elif rel.startswith(PLUGIN):
            plan[cursor / rel[len("cursor/"):]] = text
        elif rel.startswith(SKILLS):
            name = rel[len(SKILLS):].split("/")[0]
            mine = cursor / "skills" / name / "SKILL.md"
            if not (home / ".claude" / "skills" / name / "SKILL.md").exists():
                plan[mine] = text
            elif install.read(mine) == text:
                plan[mine] = None  # Cursor already loads the ~/.claude copy: drop our duplicate
    command = hook_command(python, (hooks_dir / "playbook_hook.py").as_posix(), os.name == "nt")
    hooks_json = cursor / "hooks.json"
    plan[hooks_json] = merge_hooks(install.read(hooks_json), hooks_config(command))
    return plan


def installed_profiles(home: Path) -> list[str] | None:
    """The profiles in the installed rules, so an update without --profile keeps them; None when the rules
    were never installed for Cursor (install.py then takes the Claude Code install's profiles)."""
    import build
    import install
    text = install.read(home / ".cursor" / RULES[len("cursor/"):])
    if text is None:
        return None
    return [p for p in install.PROFILE_LINE.findall(text) if p in build.PUBLIC_PROFILES]


def plan_uninstall(home: Path) -> tuple[dict[Path, str | None], list[str]]:
    import build
    import install
    cursor = home / ".cursor"
    plan: dict[Path, str | None] = {}
    notes: list[str] = []

    def remove(path: Path, ours) -> None:
        current = install.read(path)
        if current is None:
            return
        if ours(current):
            plan[path] = None
        else:
            notes.append(f"kept {path}: it changed since the install; remove it yourself if it is ours")

    for rel, text in installed_files(build).items():
        if rel.startswith(HOOKS):
            remove(cursor / "agent-playbook" / "hooks" / rel[len(HOOKS):], lambda c, t=text: c == t)
        elif rel.startswith(SKILLS):
            remove(cursor / "skills" / rel[len(SKILLS):], lambda c, t=text: c == t)
        elif rel == RULES:  # the chosen profiles follow the core, so compare the start only
            remove(cursor / rel[len("cursor/"):], lambda c: c.startswith(FRONT + build.HEADER))
        elif rel == MANIFEST:  # its version changes with every release
            remove(cursor / rel[len("cursor/"):], lambda c: '"name": "agent-playbook"' in c)
    hooks_json = cursor / "hooks.json"
    current = install.read(hooks_json)
    if current is not None:  # removed only if the install created it (no backup of an earlier file)
        left = unmerge_hooks(current)
        if left is None and install.oldest_backup(hooks_json) is None:
            plan[hooks_json] = None
        else:
            plan[hooks_json] = left or json.dumps({"version": 1, "hooks": {}}, indent=2) + "\n"
    backups = sorted(p for d in {p.parent for p in plan} if d.exists() for p in d.glob("*.bak-agent-playbook-*"))
    if backups:
        notes.append(f"kept {len(backups)} backup file(s) (*.bak-agent-playbook-*); delete them yourself when sure")
    notes.append(f"kept {home / '.claude' / 'agent-playbook'} (logs, state, config): Claude Code shares it")
    notes.append("If you pasted the rules into Cursor's Customize -> Rules, delete them there yourself.")
    return plan, notes


def notes() -> list[str]:
    """Steps the user must do by hand after installing."""
    return [
        "Then in Cursor, once:",
        "1. Restart Cursor. In Customize -> Hooks the agent-playbook hooks should be listed; if Cursor asks to "
        "trust or enable them, allow it.",
        "2. Settings -> Agents -> Auto-Run: choose Allowlist. In Auto-review mode Cursor skips the approval "
        "prompt this hook asks for. Do not put broad commands (git, gh, npm, rm, Remove-Item) on the allowlist.",
        "3. Rules (untested part): Customize -> Rules should show agent-playbook. If it does not, open "
        "~/.cursor/plugins/local/agent-playbook/rules/agent-playbook.mdc (Windows: %USERPROFILE%\\.cursor\\...), "
        "copy everything below the second '---' line and paste it once into Customize -> Rules -> User Rules.",
        "4. Keep 'Include Third-Party Plugins, Skills, and Other Configs' on (Settings -> Agents): Cursor reads "
        "the skills from ~/.claude/skills through it.",
        "5. File edits, deletions and secret-file reads cannot show a prompt in Cursor. The agent stops and shows "
        "a code; to allow that one action once, send 'approve CODE' (or '批准 CODE') yourself.",
    ]


# --- hooks.json merge (Cursor's format: {"version": 1, "hooks": {event: [{command, ...}]}}) ------------

def load_hooks(text: str | None) -> dict:
    try:
        current = json.loads(text) if text and text.strip() else {}
    except ValueError as exc:
        raise SystemExit(f"~/.cursor/hooks.json is not plain JSON ({exc}); nothing was written. "
                         "Remove comments or trailing commas and retry.")
    if not isinstance(current, dict) or not isinstance(current.get("hooks", {}), dict) or any(
            not isinstance(v, list) for v in current.get("hooks", {}).values()):
        raise SystemExit("~/.cursor/hooks.json is not in Cursor's format (an object with a 'hooks' object of "
                         "lists); nothing was written.")
    return current


def strip_ours(current: dict) -> dict:
    import install
    hooks = current.get("hooks", {})
    for event in list(hooks):
        kept = [h for h in hooks[event] if not (isinstance(h, dict) and install.is_ours(h))]
        if kept or not hooks[event]:
            hooks[event] = kept
        else:
            del hooks[event]
    return current


def merge_hooks(existing: str | None, ours: dict) -> str:
    """Replace only our own entries; keep every other hook, event and key."""
    current = strip_ours(load_hooks(existing))
    current.setdefault("version", ours["version"])
    hooks = current.setdefault("hooks", {})
    for event, entries in ours["hooks"].items():
        hooks[event] = hooks.get(event, []) + entries
    return json.dumps(current, indent=2, ensure_ascii=False) + "\n"


def unmerge_hooks(existing: str) -> str | None:
    """hooks.json without our entries, or None when nothing but an empty shell would be left."""
    current = strip_ours(load_hooks(existing))
    if not current.get("hooks") and set(current) <= {"version", "hooks"}:
        return None
    return json.dumps(current, indent=2, ensure_ascii=False) + "\n"
