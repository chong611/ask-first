"""Gemini CLI adapter for playbook_hook.py --client gemini.

Gemini CLI runs a command per hook event, with the event as JSON on stdin, and reads one JSON object
from stdout (docs/hooks/reference.md in github.com/google-gemini/gemini-cli). The events used, and the
Claude Code event each one becomes for the policy core:

  BeforeTool   -> PreToolUse        tier-2 policy; a tier-2 call is denied with a one-time approval code
  AfterTool    -> PostToolUse       remember that this turn changed something; pipe-exit advice
  BeforeAgent  -> UserPromptSubmit  start of a user turn; records "approve CODE" / "批准 CODE"
  AfterAgent   -> Stop              report check; "deny" makes Gemini retry the turn with our reason
  SessionStart -> SessionStart      Gemini has no post-compaction start, so the core adds nothing

A hook that crashes or times out lets the call through (it fails open), so every path here answers
with a JSON object and exit code 0, and nothing in this module raises.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402

# Gemini's source has a hook "ask": packages/core/src/scheduler/scheduler.ts turns hookDecision 'ask'
# into a forced confirmation dialog. It is undocumented and has not been tried live, so until a live
# test passes, tier-2 calls are denied with an approval code instead (playbook_hook.approval_code).
CAN_ASK = False
DEADLINE = 50  # seconds; BeforeTool times out at 60 s and Gemini then lets the call through

# run_shell_command runs PowerShell on Windows and bash elsewhere
# (packages/core/src/utils/shell-utils.ts, getShellConfiguration).
SHELL = "PowerShell" if os.name == "nt" else "Bash"

# Our AfterAgent "deny" makes Gemini send its reason back as a new prompt, which fires BeforeAgent again
# (packages/core/src/core/client.ts). That prompt is not the user's: it must not start a new turn or
# approve a code. Every reason we send starts with this mark, and a prompt that carries it is ignored.
MARK = "[agent-playbook check]"

# Tools whose AfterTool can change files or state (the PostToolUse matcher of the Claude Code install).
CHANGING_TOOLS = ("run_shell_command", "write_file", "replace", "save_memory")
AFTER_TOOL_MATCHER = "^(" + "|".join(CHANGING_TOOLS) + ")$|^mcp_"

_failed: str | None = None  # why normalize() could not read the last event, for render()


# --- Gemini event -> Claude Code-shaped event ----------------------------------------------------


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return value if isinstance(value, str) else ""


def _secret_or(default: str, *candidates: str) -> str:
    """The path the policy should judge: a candidate it reads as a secret file, else the default."""
    return next((c for c in candidates if c and policy.is_secret_file(c)), default)


def mcp_name(name: str, context) -> str | None:
    """mcp_<server>_<tool> -> mcp__<server>__<tool>, from mcp_context when Gemini sends it."""
    if isinstance(context, dict):
        server, tool = context.get("server_name"), context.get("tool_name")
        if isinstance(server, str) and isinstance(tool, str) and server and tool:
            return f"mcp__{server}__{tool}"
    if name.startswith("mcp_") and "_" in name[4:]:  # Gemini splits at the first "_" after mcp_ too
        server, _, tool = name[4:].partition("_")
        if server and tool:
            return f"mcp__{server}__{tool}"
    return None


def map_tool(name: str, args, context=None, cwd: str = "") -> tuple[str, dict, str]:
    """A Gemini tool call -> (Claude Code tool name, its input, the folder it runs in).

    Parameter names: packages/core/src/tools/definitions/base-declarations.ts. Only the inputs the
    policy reads are kept, so a reworded description does not change the approval code of a call.
    """
    args = args if isinstance(args, dict) else {}
    if name == "run_shell_command":
        tool_input = {"command": _text(args, "command")}
        folder = _text(args, "dir_path")
        if folder:
            tool_input["dir_path"] = folder  # the same command in another folder is another call
            full = folder if os.path.isabs(folder) else os.path.join(cwd, folder)
            cwd = full if os.path.isdir(full) else cwd
        return SHELL, tool_input, cwd
    if name == "write_file":
        return "Write", {"file_path": _text(args, "file_path"), "content": _text(args, "content")}, cwd
    if name == "replace":
        return "Edit", {k: _text(args, k) for k in ("file_path", "old_string", "new_string")}, cwd
    if name == "save_memory":  # older Gemini CLI: appends a fact to ~/.gemini/GEMINI.md
        return "Edit", {"file_path": "~/.gemini/GEMINI.md", "new_string": _text(args, "fact")}, cwd
    if name == "read_file":
        return "Read", {"file_path": _text(args, "file_path")}, cwd
    if name == "read_many_files":
        include = args.get("include")
        paths = [p for p in (include if isinstance(include, list) else [include]) if isinstance(p, str)]
        return "Read", {"file_path": _secret_or(paths[0] if paths else "", *paths)}, cwd
    if name in ("grep_search", "search_file_content"):
        folder = _text(args, "dir_path")
        return "Grep", {"pattern": _text(args, "pattern"),
                        "path": _secret_or(folder, _text(args, "include_pattern"), folder),
                        "output_mode": "files_with_matches" if args.get("names_only") is True else "content"}, cwd
    if name == "glob":
        return "Glob", {"pattern": _text(args, "pattern"), "path": _text(args, "dir_path")}, cwd
    if name == "ask_user":
        return "AskUserQuestion", {"questions": args.get("questions")}, cwd
    return mcp_name(name, context) or name, args, cwd


def normalize(raw: dict) -> dict | None:
    """Gemini's hook input -> a Claude Code-shaped event, or None to ignore it."""
    global _failed
    _failed = None
    try:
        return _normalize(raw)
    except Exception as exc:  # noqa: BLE001 - render() answers for a BeforeTool that could not be read
        _failed = f"{type(exc).__name__}: {exc}"[:300]
        return None


def _normalize(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("hook_event_name")
    cwd = raw.get("cwd") if isinstance(raw.get("cwd"), str) else ""
    sid = raw.get("session_id")
    event = {"session_id": "gemini-" + (sid if isinstance(sid, str) else ""), "cwd": cwd,
             "transcript_path": raw.get("transcript_path") if isinstance(raw.get("transcript_path"), str) else ""}
    if kind in ("BeforeTool", "AfterTool"):
        name = raw.get("tool_name") if isinstance(raw.get("tool_name"), str) else ""
        if kind == "AfterTool" and name not in CHANGING_TOOLS and not name.startswith("mcp_"):
            return None  # reads and searches change nothing
        tool, tool_input, cwd = map_tool(name, raw.get("tool_input"), raw.get("mcp_context"), cwd)
        return {**event, "cwd": cwd, "hook_event_name": "PreToolUse" if kind == "BeforeTool" else "PostToolUse",
                "tool_name": tool, "tool_input": tool_input}
    if kind == "BeforeAgent":
        prompt = raw.get("prompt") if isinstance(raw.get("prompt"), str) else ""
        if MARK in prompt:
            return None  # our own retry request, not a user message
        return {**event, "hook_event_name": "UserPromptSubmit", "prompt": prompt}
    if kind == "AfterAgent":
        # prompt_response is the text of the whole turn (every model message joined), not only the last
        message = raw.get("prompt_response") if isinstance(raw.get("prompt_response"), str) else ""
        return {**event, "hook_event_name": "Stop", "last_assistant_message": message,
                "stop_hook_active": raw.get("stop_hook_active") is True}
    if kind == "SessionStart":
        return {**event, "hook_event_name": "SessionStart", "source": str(raw.get("source") or "")}
    return None


# --- Claude Code-shaped outputs -> Gemini's answer --------------------------------------------------


def render(event: dict | None, outputs: list[dict], raw: dict | None) -> tuple[str, int]:
    """Our Claude-shaped outputs -> (Gemini's stdout text, exit code). Always one JSON object, exit 0."""
    try:
        answer = _render(event, outputs if isinstance(outputs, list) else [], raw)
    except Exception as exc:  # noqa: BLE001
        answer = _unsure(raw, type(exc).__name__)
    return json.dumps(answer, ensure_ascii=True), 0  # ASCII: a non-UTF-8 console cannot break it


def _unsure(raw, why: str | None) -> dict:
    """An adapter error on BeforeTool asks, as the core does for its own errors; other events go on."""
    if isinstance(raw, dict) and raw.get("hook_event_name") == "BeforeTool":
        reason = (f"agent-playbook rule 2.3: the Gemini adapter hit an internal error ({why}), "
                  "so it cannot vouch for this call")
        return {"decision": "ask", "reason": reason, "systemMessage": reason}
    return {}


CODE_IN_REASON = re.compile(r'^(.*?)\. This agent cannot show an approval prompt\..*"approve ([A-Z0-9]{4})"', re.S)


def for_user(reason: str) -> str:
    """The terminal line for the user: what was blocked and the code that lets it run once. The reason
    itself goes to the agent; the user sees the code here even if the agent does not pass it on."""
    m = CODE_IN_REASON.match(reason)
    if not m:
        return reason
    return f"{m.group(1)}. To let this exact call run once, reply \"批准 {m.group(2)}\" (or \"approve {m.group(2)}\")."


def _hso(output: dict) -> dict:
    hso = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    return hso if isinstance(hso, dict) else {}


def _render(event, outputs: list, raw) -> dict:
    if not isinstance(event, dict):
        return _unsure(raw, _failed) if _failed else {}
    kind = event.get("hook_event_name")
    if kind == "PreToolUse":
        for output in outputs:
            decision = _hso(output).get("permissionDecision")
            if decision in ("deny", "ask"):  # "ask" only on an internal error of the core (CAN_ASK is False)
                reason = str(_hso(output).get("permissionDecisionReason") or "agent-playbook rule 2.3")
                return {"decision": decision, "reason": reason, "systemMessage": for_user(reason)}
        # Never answer "allow": that could some day skip Gemini's own confirmation. The core's
        # pipe-exit advice is sent after the call instead, as BeforeTool has no added context.
        return {}
    if kind == "PostToolUse":
        if event.get("tool_name") in ("Bash", "PowerShell"):
            import report
            command = (event.get("tool_input") or {}).get("command")
            advice = report.pipe_hides_exit(command) if isinstance(command, str) else None
            if advice:
                return {"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": advice}}
        return {}
    if kind == "Stop":
        for output in outputs:
            if not isinstance(output, dict):
                continue
            text = output.get("reason") if output.get("decision") == "block" else _hso(output).get("additionalContext")
            if isinstance(text, str) and text:
                return {"decision": "deny", "reason": f"{MARK} {text}",
                        "systemMessage": "agent-playbook: the reply is sent back to be fixed. " + text[:300]}
        return {}
    if kind == "SessionStart":
        context = next((_hso(o).get("additionalContext") for o in outputs if _hso(o).get("additionalContext")), None)
        if isinstance(context, str):
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
        return {}
    return {}  # UserPromptSubmit: any decision on BeforeAgent would discard the user's message
