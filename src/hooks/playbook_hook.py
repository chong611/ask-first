"""Claude Code hook entry point: python3 playbook_hook.py < event.json

One script for every event, so install needs one path. Events handled:
  PreToolUse        tier-2 policy -> permission prompt ("ask")         rule 2.3, 5.1
  PostToolUse       remember that this turn changed something           rule 1.3
  UserPromptSubmit  start-of-turn git snapshot
  Stop              require the report on turns that changed something; rule 1.3
                    log deferral phrases (log-only, Q42)
  InstructionsLoaded record which instruction files loaded and why      Q41
  SessionStart      after compaction, restate open items as facts       Q41

Config (lists merge): <project>/.agent-playbook/config.json and
~/.claude/agent-playbook/config.json, keys: allowed_branches, data_paths, ask_tools.
State: ~/.claude/agent-playbook/state/<session_id>.json; logs in ../logs/.
Exit code is always 0; decisions go in JSON on stdout (ASCII-only, so a non-UTF-8
console cannot break it). Any internal error on PreToolUse answers "ask" with the
error as the reason, so a bug never silently allows a tier-2 action and never
locks the user out of every tool either. Errors on other events are swallowed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import policy  # noqa: E402
import report  # noqa: E402

HOME = Path(os.environ.get("AGENT_PLAYBOOK_HOME", Path.home() / ".claude" / "agent-playbook"))
READ_ONLY_NAMES = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "echo", "which", "file", "stat", "du",
                   "df", "tree", "less", "more", "sort", "uniq", "diff", "cd", "true", "printf", "date", "whoami",
                   "env", "type", "realpath", "dirname", "basename", "jq", "cut", "tr", "nl", "column"}
READ_ONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog",
                 "remote", "config", "grep", "reflog", "cat-file", "ls-tree", "fetch"}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def is_read_only(command: str) -> bool:
    for cmd in policy.split_commands(command):
        if any(tok in (">", ">>") for tok in cmd):
            return False
        sub = policy.git_args(cmd)
        if sub is not None:
            if not sub or sub[0] not in READ_ONLY_GIT and not (sub[0] == "branch" and all(
                    a in ("-a", "-r", "-v", "-vv", "--list", "--show-current") for a in sub[1:])):
                return False
            if sub[0] == "config" and not any(a in ("--get", "--list", "-l", "--get-all") for a in sub[1:]):
                return False
            continue
        if Path(cmd[0]).name not in READ_ONLY_NAMES or (Path(cmd[0]).name == "find" and "-delete" in cmd):
            return False
    return True


def config(cwd: str, session_id: str = "") -> policy.Context:
    merged = {"allowed_branches": [], "data_paths": [], "ask_tools": []}
    for path in (Path(cwd) / ".agent-playbook" / "config.json", HOME / "config.json"):
        data = load_json(path, {})
        for key in merged:
            merged[key] += [v for v in data.get(key, []) if v not in merged[key]]
    return policy.Context(
        cwd=cwd,
        allowed_branches=tuple(merged["allowed_branches"]),
        data_paths=tuple(merged["data_paths"]),
        ask_tools=tuple(merged["ask_tools"]),
        tracked=git_tracked(cwd),
        created=set(load_state(session_id).get("created", [])) if session_id else set(),
    )


def git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return policy.decode(out.stdout) if out.returncode == 0 else None


def git_tracked(cwd: str) -> set[str] | None:
    out = git(cwd, "ls-files", "-z")  # -z: no quoting of non-ASCII names
    return None if out is None else {p for p in out.split("\0") if p}


def git_snapshot(cwd: str) -> str | None:
    head = git(cwd, "rev-parse", "HEAD") or ""
    status = git(cwd, "status", "--porcelain")
    return None if status is None else head + status


def state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return HOME / "state" / f"{safe}.json"


def load_state(session_id: str) -> dict:
    return load_json(state_path(session_id), {})


def save_state(session_id: str, state: dict) -> None:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8", errors="backslashreplace")


STATE_MAX_AGE_DAYS = 30
LOG_MAX_BYTES = 1_000_000


def prune(now: float | None = None) -> None:
    """Keep the hook's own files bounded: drop session state older than 30 days."""
    cutoff = (now or time.time()) - STATE_MAX_AGE_DAYS * 86400
    for path in (HOME / "state").glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def log(name: str, record: dict) -> None:
    """Append to logs/<name>.jsonl; past 1 MB it rotates to <name>.1.jsonl (one old copy kept)."""
    path = HOME / "logs" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(f"{name}.1.jsonl"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8", errors="backslashreplace") as fh:  # a lone surrogate must not stop a check
        fh.write(json.dumps({"ts": int(time.time()), **record}, ensure_ascii=False) + "\n")


def strings(value) -> list[str]:
    """Every string inside a tool input, so text after a newline is read as text (not an escaped \\n)."""
    if isinstance(value, str):
        return [value]
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return [s for v in items for s in strings(v)]


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")


# --- events --------------------------------------------------------------------

NO_PROMPT_MODES = {"bypassPermissions", "dontAsk"}


def deny(reason: str) -> None:
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "agent-playbook rule 2.3: " + reason,
    }})


def ask(reason: str) -> None:
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": "agent-playbook rule 2.3: " + reason,
    }})


def on_pre_tool_use(event: dict) -> None:
    sid = str(event.get("session_id") or "")
    ctx = config(event["cwd"], sid)
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "AskUserQuestion":  # rule 2.2: the Stop check needs the questions asked this turn
        state = load_state(sid)
        state["asked_this_turn"] = True
        asked = report.question_ids("\n".join(strings(tool_input)))
        state["asked_ids"] = sorted(set(state.get("asked_ids", [])) | asked)
        save_state(sid, state)
    decision, reason = policy.classify(tool, tool_input, ctx)
    if decision == policy.ALLOW and tool == "Write":
        path = str(tool_input.get("file_path") or "")
        full = os.path.abspath(os.path.join(event["cwd"], os.path.expanduser(path)))
        if path and not os.path.lexists(full):  # the agent is creating it: later edits are its own work
            state = load_state(sid)
            state["created"] = sorted(set(state.get("created", [])) | {full})[-500:]
            save_state(sid, state)
    if decision == policy.ASK:
        mode = str(event.get("permission_mode") or "")
        log("tier2", {"session": sid, "tool": tool, "reason": reason, "mode": mode})
        if mode in NO_PROMPT_MODES:  # these modes approve an "ask" silently, so the only safe answer is no
            deny(reason + f". This session runs in {mode} mode, which shows no approval prompt. Tell the user; "
                 "they can switch to Manual or Auto mode to approve it, or run it themselves.")
        else:
            ask(reason)
    elif tool in ("Bash", "PowerShell") and (advice := report.pipe_hides_exit(str(tool_input.get("command", "")))):
        log("advice", {"session": sid, "advice": "pipe-exit"})
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": advice}})


def on_post_tool_use(event: dict) -> None:
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool in ("Bash", "PowerShell") and is_read_only(str(tool_input.get("command", ""))):
        return
    if tool.startswith("mcp__") and not policy.OUTWARD_WORDS.search(tool.split("__")[-1]):
        return
    sid = event.get("session_id", "")
    state = load_state(sid)
    state["changed"] = True
    save_state(sid, state)


def on_user_prompt_submit(event: dict) -> None:
    prune()  # once per prompt is often enough and keeps tool calls fast
    sid = event.get("session_id", "")
    state = load_state(sid)
    state["changed"] = False
    state["snapshot"] = git_snapshot(event.get("cwd", "."))
    state["blocked_rules"] = []
    state.pop("blocked_this_turn", None)  # pre-Q91A key
    state["ps_reminded"] = False
    state["asked_this_turn"] = False
    state["asked_ids"] = []
    save_state(sid, state)


def zh_profile() -> bool:
    """The lang-zh-CN profile is in the global rules (the project core carries no profiles)."""
    try:
        return "# profile: lang-zh-CN" in (HOME.parent / "rules" / "agent-playbook.md").read_text(encoding="utf-8")
    except OSError:
        return False


def on_stop(event: dict) -> None:
    sid = event.get("session_id", "")
    message = event.get("last_assistant_message") or ""
    state = load_state(sid)

    hits = report.deferral_hits(message)
    if hits:
        log("deferral", {"session": sid, "hits": hits})
    if zh_profile() and (english := report.mostly_english(message)):
        log("language", {"session": sid, "latin": english[0], "cjk": english[1]})  # Q134: log-only for a week

    snapshot = git_snapshot(event.get("cwd", "."))
    changed = state.get("changed") or (snapshot is not None and snapshot != state.get("snapshot"))
    parsed = report.parse(message)
    if parsed.counts:
        state["open"] = {k: parsed.items.get(k, []) for k in ("Not done", "Waiting on you")}
    save_state(sid, state)

    ps_advice = report.powershell_and_and(message)
    remind = ps_advice and not state.get("ps_reminded") and not event.get("stop_hook_active")
    if remind:
        state["ps_reminded"] = True  # once per turn; it continues the turn so the steps get fixed
        log("advice", {"session": sid, "advice": "powershell-and-and"})
    # Each rule blocks at most once per turn (Q91A), so there is no loop. A continuation by the
    # reminder or another Stop hook (stop_hook_active) is still checked (Q85).
    blocked = set(state.get("blocked_rules", [])) | ({"1.3", "2.2"} if state.get("blocked_this_turn") else set())
    checks = {"1.3": lambda: report.check(message) if changed else None,
              "2.2": lambda: report.ask_check(message, bool(state.get("asked_this_turn")), report.interactive_client(),
                                              set(state.get("asked_ids", [])))}
    reasons = {rule: r for rule, check in checks.items() if rule not in blocked and (r := check())}
    prose = report.prose_asks(message) if "2.2" not in blocked else []
    if prose:  # Q89A: one line per checked stop, so the false-block rate can be measured
        log("ask-prose", {"session": sid, "hits": prose, "asked": bool(state.get("asked_this_turn")),
                          "blocked": "2.2" in reasons})
    if reasons:
        state["blocked_rules"] = sorted(blocked | set(reasons))
        save_state(sid, state)
        emit({"decision": "block", "reason": " ".join(list(reasons.values()) + ([ps_advice] if remind else []))})
    elif remind:
        save_state(sid, state)
        emit({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": ps_advice}})


def on_instructions_loaded(event: dict) -> None:
    sid = event.get("session_id", "")
    state = load_state(sid)
    loads = state.setdefault("loads", [])
    loads.append({"file": event.get("file_path"), "reason": event.get("load_reason")})
    state["loads"] = loads[-50:]
    save_state(sid, state)


def on_session_start(event: dict) -> None:
    if event.get("source") != "compact":
        return
    state = load_state(event.get("session_id", ""))
    open_items = state.get("open") or {}
    lines = []
    for section, items in open_items.items():
        if items:
            lines.append(f"Open items recorded before compaction, {section}:")
            lines += [f"- {item}" for item in items[:20]]
    core_reloaded = any(
        l.get("reason") == "compact" and str(l.get("file", "")).endswith(("AGENTS.md", "CLAUDE.md", "agent-playbook.md"))
        for l in state.get("loads", []))
    lines.append("agent-playbook core reload after compaction: "
                 + ("observed." if core_reloaded else "not observed by the hook yet."))
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}})


HANDLERS = {
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": on_post_tool_use,
    "UserPromptSubmit": on_user_prompt_submit,
    "Stop": on_stop,
    "InstructionsLoaded": on_instructions_loaded,
    "SessionStart": on_session_start,
}


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        event = json.loads(raw.decode("utf-8-sig", errors="replace") or "null")  # PowerShell 5.1 pipes add a BOM
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    if not isinstance(event.get("cwd"), str) or not os.path.isdir(event["cwd"]):
        event["cwd"] = os.getcwd()
    name = event.get("hook_event_name")
    handler = HANDLERS.get(name) if isinstance(name, str) else None
    if not handler:
        return 0
    try:
        handler(event)
    except Exception as exc:  # noqa: BLE001 - a hook must never crash the session
        try:
            log("errors", {"event": name, "error": f"{type(exc).__name__}: {exc}"[:500]})
        except Exception:  # noqa: BLE001
            pass
        if name == "PreToolUse":
            ask(f"the policy hook hit an internal error ({type(exc).__name__}), so it cannot vouch for this call")
    return 0


if __name__ == "__main__":
    sys.exit(main())
