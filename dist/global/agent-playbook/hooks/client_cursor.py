"""Cursor adapter for playbook_hook.py --client cursor. Cursor hooks: https://cursor.com/docs/hooks

Cursor event                 Claude Code event the core handles
  beforeShellExecution       PreToolUse PowerShell (Windows) or Bash   "ask" shows Cursor's prompt (Allowlist
  beforeMCPExecution         PreToolUse mcp__<server>__<tool>          run mode; Auto-review skips it)
  preToolUse Read, Delete,   PreToolUse Read, Bash "rm -- <path>",     Cursor does not enforce "ask" here, so
    Write and edit tools       Write (new file) or Edit                the core denies with an approval code
  postToolUse                PostToolUse                               this turn changed something
  beforeSubmitPrompt         UserPromptSubmit                          turn start; the user's "批准 CODE"
  afterAgentResponse         (none: the reply is kept for the stop check)
  stop                       Stop with the kept reply                  a block becomes followup_message
Other events are ignored. Session: conversation_id. cwd: the event's cwd, else workspace_roots[0].
Every answer is one JSON object holding only fields that event accepts; {} means no decision.
"""

from __future__ import annotations

import json
import os
import re
import shlex

CAN_ASK = False  # per event, see can_ask()
ASKS = {"beforeShellExecution", "beforeMCPExecution"}  # Cursor shows a prompt for "ask" only on these
GATES = ASKS | {"preToolUse"}  # permission hooks: run fail-closed, so they always answer
EDITS = ("Write", "Edit", "StrReplace", "MultiEdit", "ApplyPatch")  # tools/target_cursor.py matches these
MARK = "agent-playbook hook, not the user"
PREFIX = f"[{MARK}]"  # Cursor sends a follow-up as the next *user* message; this keeps it from counting as one
FOLLOWUPS_PER_TURN = 2  # the core blocks each Stop rule (1.3, 2.2) at most once per turn
REPLY_CHARS = 50_000  # the end of a reply is enough for the report check
PATH_KEYS = ("file_path", "path", "target_file", "filePath", "file", "notebook_path", "relative_workspace_path")
PATCH = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)


def can_ask(raw: dict) -> bool:
    return raw.get("hook_event_name") in ASKS


def normalize(raw: dict) -> dict | None:
    """Cursor's hook input -> a Claude Code-shaped event, or None to ignore it."""
    import playbook_hook
    name = raw.get("hook_event_name")
    sid = session(raw)
    base = {"session_id": sid, "cwd": workdir(raw)}
    if name == "beforeShellExecution":
        return {**base, "hook_event_name": "PreToolUse", "tool_name": shell(),
                "tool_input": {"command": str(raw.get("command") or "")}}
    if name == "beforeMCPExecution":
        tool = f"mcp__{raw.get('mcp_server_name') or 'unknown'}__{raw.get('tool_name') or ''}"
        return {**base, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": mcp_input(raw.get("tool_input"))}
    if name in ("preToolUse", "postToolUse"):
        call = tool_call(raw, base, post=name == "postToolUse")
        if call is None:
            return None
        return {**base, "hook_event_name": "PreToolUse" if name == "preToolUse" else "PostToolUse",
                "tool_name": call[0], "tool_input": call[1]}
    state = playbook_hook.load_state(sid)
    if name == "beforeSubmitPrompt":
        prompt = str(raw.get("prompt") or "")
        if MARK in prompt:
            return None  # our own stop follow-up: not a new turn, and never an approval
        state.pop("cursor_reply", None)
        state["cursor_followups"] = 0
        playbook_hook.save_state(sid, state)
        fixed = unmangled(prompt)
        return {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt if fixed == prompt else f"{prompt}\n{fixed}"}
    if name == "afterAgentResponse":
        state["cursor_reply"] = str(raw.get("text") or "")[-REPLY_CHARS:]
        playbook_hook.save_state(sid, state)
        return None
    if name == "stop":
        reply = state.pop("cursor_reply", "")
        playbook_hook.save_state(sid, state)
        if raw.get("status", "completed") != "completed" or state.get("cursor_followups", 0) >= FOLLOWUPS_PER_TURN:
            return None
        if not reply:
            playbook_hook.log("cursor", {"session": sid, "note": "stop without a recorded reply"})
            return None
        return {**base, "hook_event_name": "Stop", "last_assistant_message": reply,
                "stop_hook_active": bool(raw.get("loop_count") or state.get("cursor_followups"))}
    return None


def render(event: dict | None, outputs: list[dict], raw: dict | None) -> tuple[str, int]:
    """Our Claude-shaped outputs -> (Cursor's stdout text, exit code)."""
    if not isinstance(raw, dict):  # unreadable input: report a failure, so fail-closed gates block the call
        return "", 1
    name = raw.get("hook_event_name")
    decision, reason, notes = None, "", []
    for out in outputs:
        hso = out.get("hookSpecificOutput") if isinstance(out.get("hookSpecificOutput"), dict) else {}
        if hso.get("permissionDecision") in ("deny", "ask") and decision != "deny":
            decision, reason = hso["permissionDecision"], str(hso.get("permissionDecisionReason") or "")
        notes += [str(t) for t in (out.get("reason") if out.get("decision") == "block" else None,
                                   hso.get("additionalContext")) if t]
    answer: dict = {}
    if name in GATES and decision:
        if decision == "ask" and name not in ASKS:  # only the core's internal-error answer gets here
            decision, reason = "deny", reason + ". Cursor shows no approval prompt for this tool, so it is refused."
        answer = {"permission": decision, "user_message": reason, "agent_message": reason}  # deny may drop agent_message
    elif name == "beforeSubmitPrompt":
        answer = {"continue": True}
    elif name == "postToolUse" and event and event.get("tool_name") in ("Bash", "PowerShell"):
        import report  # rule 1.4: the Claude path gives this before the call; Cursor takes context only after
        if (advice := report.pipe_hides_exit(str(event["tool_input"].get("command", "")))):
            answer = {"additional_context": advice}
    elif name == "stop" and notes:
        answer = {"followup_message": followup(" ".join(notes), raw)}
    return json.dumps(answer, ensure_ascii=True), 0


# --- helpers ------------------------------------------------------------------------

def session(raw: dict) -> str:
    sid = raw.get("conversation_id") or raw.get("session_id")
    return f"cursor-{sid}" if isinstance(sid, str) and sid else "cursor-unknown"


def workdir(raw: dict) -> str:
    """The event's cwd (often empty), the shell's working_directory, then the workspace root."""
    args = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    roots = raw.get("workspace_roots") if isinstance(raw.get("workspace_roots"), list) else []
    for value in (raw.get("cwd"), args.get("working_directory"), *roots[:1],
                  os.environ.get("CURSOR_PROJECT_DIR"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if isinstance(value, str) and value.strip():
            path = re.sub(r"^/([A-Za-z]:[/\\])", r"\1", re.sub(r"^file://", "", value.strip()))  # /c:/x -> c:/x
            if os.path.isdir(path):
                return path
    return ""


def shell() -> str:
    return "PowerShell" if os.name == "nt" else "Bash"  # Cursor's agent shell on Windows is PowerShell


def mcp_input(value) -> dict:
    if isinstance(value, str):  # Cursor sends MCP arguments as a JSON string
        try:
            value = json.loads(value)
        except ValueError:
            return {"input": value}
    return value if isinstance(value, dict) else {"input": value}


def tool_call(raw: dict, base: dict, post: bool) -> tuple[str, dict] | None:
    tool = str(raw.get("tool_name") or "")
    args = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    if tool == "Shell":
        return shell(), {"command": str(args.get("command") or "")}
    if tool.startswith("MCP:"):  # no server name here; the policy reads the tool name
        return f"mcp__cursor__{tool[4:]}", mcp_input(raw.get("tool_input"))
    if post and tool == "Read":
        return None  # a read changes nothing
    calls = file_calls(tool, args, base["cwd"])
    if not calls:
        if tool in ("Read", "Delete", *EDITS):
            import playbook_hook  # keys only, never values: learn Cursor's real input shape
            playbook_hook.log("cursor", {"note": "no path found", "tool": tool, "keys": sorted(map(str, args))})
        return None
    return calls[0] if len(calls) == 1 or post else riskiest(calls, base)


def file_calls(tool: str, args: dict, cwd: str) -> list[tuple[str, dict]]:
    """Cursor's file tools as the Claude calls the policy knows. Input keys are not documented, so look widely."""
    from playbook_hook import strings
    texts = strings(args)
    paths = [args[k] for k in PATH_KEYS if isinstance(args.get(k), str) and args[k]]
    content = " ".join(t for t in texts if t not in paths)
    if tool == "Read":
        return [("Read", {"file_path": p}) for p in paths[:1]]
    if tool == "Delete":
        return [delete(p) for p in paths[:1]]
    if tool not in EDITS:
        return []
    patched = [(kind, p) for t in texts for kind, p in PATCH.findall(t)]
    if patched:  # one patch, several files: the approval binds to the whole patch
        return [delete(p) if kind == "Delete File" else write(p, content, cwd) for kind, p in patched]
    return [write(p, content, cwd) for p in paths[:1]]


def write(path: str, content: str, cwd: str) -> tuple[str, dict]:
    """Write for a new file (the core records it as the agent's), Edit for an existing one. Cursor's Write
    also covers small edits, and the policy's Write would treat each edit of a dirty file as an overwrite."""
    full = os.path.join(cwd, os.path.expanduser(path)) if cwd else os.path.expanduser(path)
    return ("Edit" if os.path.lexists(full) else "Write"), {"file_path": path, "content": content}


def delete(path: str) -> tuple[str, dict]:
    return "Bash", {"command": "rm -- " + shlex.quote(path.replace("\\", "/"))}


def riskiest(calls: list[tuple[str, dict]], base: dict) -> tuple[str, dict]:
    """The first call the policy would stop, carrying every path so an approval covers exactly this patch."""
    import playbook_hook
    import policy
    ctx = playbook_hook.config(base["cwd"] or os.getcwd(), base["session_id"])
    every = sorted({c[1].get("file_path") or c[1].get("command", "") for c in calls})
    for name, args in calls:
        if policy.classify(name, args, ctx)[0] == policy.ASK:
            return name, {**args, "patch_paths": every}
    return calls[0]


def unmangled(text: str) -> str:
    """Windows PowerShell 5.1 reads a payload file without -Encoding as the ANSI code page, so 批准 can
    arrive garbled; undo that when it decodes cleanly. Only the user's own prompt goes through here."""
    import locale
    for codec in dict.fromkeys(("cp1252", locale.getpreferredencoding(False))):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeError, LookupError):
            continue
        if fixed != text:
            return fixed
    return text


def followup(text: str, raw: dict) -> str:
    """The stop check's reason as Cursor's follow-up. It can quote the agent's reply, so strip anything the
    core would read as the user's approval; beforeSubmitPrompt also ignores messages carrying MARK."""
    import playbook_hook
    while playbook_hook.APPROVE.search(text):
        text = playbook_hook.APPROVE.sub("(approval phrase removed)", text)
    sid = session(raw)
    state = playbook_hook.load_state(sid)
    state["cursor_followups"] = state.get("cursor_followups", 0) + 1
    playbook_hook.save_state(sid, state)
    return f"{PREFIX} {text}"
