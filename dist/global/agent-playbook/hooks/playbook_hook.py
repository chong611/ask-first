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
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:  # a broken policy or report file must still end in fail_closed(), not in a crash the client lets through
    import policy  # noqa: E402
    import report  # noqa: E402
    IMPORT_ERROR: BaseException | None = None
except BaseException as exc:  # noqa: BLE001
    IMPORT_ERROR = exc

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


# --client NAME loads client_NAME.py, which translates another agent's events to Claude Code's
# shape (normalize) and our Claude-shaped answers back (render). Without --client: Claude Code.
CLIENT = None
CLIENT_CAN_ASK = True  # False: the client has no approval prompt, so "ask" becomes an approval code
OUTPUTS: list[dict] = []


def emit(obj: dict) -> None:
    if CLIENT is not None:
        OUTPUTS.append(obj)
        return
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")


# Clients that cannot show an approval prompt get a one-time code instead: the call is denied with
# the code, the agent asks the user in chat, and only the user's own next message ("approve K7Q2" or
# "批准 K7Q2", seen by UserPromptSubmit) lets that exact call through once.
APPROVE = re.compile(r"(?:approve|批准)\s*([A-Z0-9]{4})\b", re.I)
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def call_key(tool: str, tool_input: dict) -> str:
    import hashlib
    blob = json.dumps([tool, tool_input], sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def approval_code(sid: str, tool: str, tool_input: dict) -> str | None:
    """None when the user already approved this exact call (consumed now); otherwise its code."""
    import secrets
    state = load_state(sid)
    key = call_key(tool, tool_input)
    if key in state.get("approved", []):
        state["approved"].remove(key)
        save_state(sid, state)
        return None
    pending = state.setdefault("pending", {})
    code = next((c for c, k in pending.items() if k == key), None)
    if code is None:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
        pending[code] = key
        state["pending"] = dict(list(pending.items())[-20:])
        save_state(sid, state)
    return code


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
        elif not CLIENT_CAN_ASK:
            code = approval_code(sid, tool, tool_input)
            if code is None:
                log("tier2-approved", {"session": sid, "tool": tool, "client": CLIENT})
                return
            deny(reason + f". This agent cannot show an approval prompt. Ask the user, naming the exact action; "
                 f"if they reply \"approve {code}\" (or \"批准 {code}\"), retry the identical call once.")
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
    pending = state.get("pending", {})
    for code in APPROVE.findall(str(event.get("prompt") or "")):
        if code.upper() in pending:
            state["approved"] = (state.get("approved", []) + [pending.pop(code.upper())])[-20:]
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


def main(argv: list[str] | None = None) -> int:
    global CLIENT, CLIENT_CAN_ASK
    argv = sys.argv[1:] if argv is None else argv
    if "--client" in argv[:-1]:
        CLIENT = argv[argv.index("--client") + 1]
    if IMPORT_ERROR is not None:
        raise IMPORT_ERROR
    if CLIENT is not None:
        import importlib
        adapter = importlib.import_module(f"client_{CLIENT}")
        CLIENT_CAN_ASK = adapter.CAN_ASK
        OUTPUTS.clear()
        code = run()
        text, code = adapter.render(LAST_EVENT, OUTPUTS, LAST_RAW)
        write_once(text)
        return code
    return run()


# --- failing closed ------------------------------------------------------------------------------
# Codex and Gemini CLI let a tool call through when its hook crashes or times out. So a crash
# anywhere (even a broken policy.py or adapter) and a check about to run past the client's
# timeout both answer "deny" before-tool, in a form each client honours without our adapters.

BEFORE_TOOL = {"PreToolUse", "BeforeTool", "beforeShellExecution", "beforeMCPExecution", "preToolUse",
               "beforeReadFile"}
_DONE = threading.Event()
_LOCK = threading.Lock()
RAW_BYTES: bytes | None = None


def write_once(text: str) -> bool:
    """Print the answer unless the deadline already printed one; True if this call printed."""
    with _LOCK:
        if _DONE.is_set():
            return False
        _DONE.set()
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()
        return True


def fallback(event_name: str, reason: str) -> tuple[str, int]:
    """The client's own deny for a before-tool event; for any other event, nothing to say."""
    if event_name not in BEFORE_TOOL:
        return ("{}" if CLIENT in ("codex", "gemini", "cursor") else ""), 0
    reason = "agent-playbook rule 2.3: " + reason
    if CLIENT == "gemini":  # exit 2 denies even if the JSON is not read
        return json.dumps({"decision": "deny", "reason": reason, "systemMessage": reason}), 2
    if CLIENT == "cursor":  # exit 2 denies
        return json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}), 2
    decision = "deny" if CLIENT == "codex" else "ask"  # Claude Code can still ask the user
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                              "permissionDecisionReason": reason}}), 0


def decode(raw: bytes) -> str:
    """PowerShell 5.1 pipes add a BOM, and Cursor's preamble can add a second one."""
    return raw.decode("utf-8", errors="replace").lstrip("﻿")


def raw_event_name() -> str:
    global RAW_BYTES
    if RAW_BYTES is None:
        try:
            RAW_BYTES = sys.stdin.buffer.read()
        except (OSError, ValueError):
            RAW_BYTES = b""
    try:
        name = json.loads(decode(RAW_BYTES) or "null").get("hook_event_name")
    except (ValueError, AttributeError):
        return ""
    return name if isinstance(name, str) else ""


def fail_closed(exc: BaseException) -> int:
    name = raw_event_name()
    text, code = fallback(name, f"the policy hook failed ({type(exc).__name__}), so it cannot vouch for this call. "
                                "Retry once; if it fails again, tell the user.")
    try:
        sys.stderr.write(f"agent-playbook hook error: {type(exc).__name__}: {exc}"[:500] + "\n")
    except (OSError, ValueError):
        pass
    return code if write_once(text) else 0


def start_deadline(seconds: float, event_name: str) -> None:
    """Answer the client's deny just before its own timeout would let the call through."""
    def fire():
        text, code = fallback(event_name, f"the policy check took longer than {seconds:g}s, so it cannot vouch "
                                          "for this call. Retry once; if it happens again, tell the user.")
        if write_once(text):
            os._exit(code)
    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()


LAST_EVENT: dict | None = None  # the Claude-shaped event run() handled, for the client's render()
LAST_RAW: dict | None = None


def run() -> int:
    global LAST_EVENT, LAST_RAW, CLIENT_CAN_ASK, RAW_BYTES
    raw = RAW_BYTES = sys.stdin.buffer.read()
    try:
        event = json.loads(decode(raw) or "null")
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    if CLIENT is None and ("cursor_version" in event or "workspace_roots" in event):
        sys.stdout.write("{}\n")  # Cursor importing our Claude Code hooks: its own --client cursor hooks decide
        return 0
    if CLIENT is not None:
        import importlib
        adapter = importlib.import_module(f"client_{CLIENT}")
        LAST_RAW = event
        deadline = getattr(adapter, "DEADLINE", None)  # seconds, a margin below the client's hook timeout
        if deadline and event.get("hook_event_name") in BEFORE_TOOL:
            start_deadline(deadline, str(event.get("hook_event_name")))
        if hasattr(adapter, "can_ask"):  # some clients can prompt for some tools only
            CLIENT_CAN_ASK = bool(adapter.can_ask(event))
        event = adapter.normalize(event)
        if not isinstance(event, dict):
            return 0
        LAST_EVENT = event
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
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - anything else fails closed (see fail_closed)
        sys.exit(fail_closed(exc))
