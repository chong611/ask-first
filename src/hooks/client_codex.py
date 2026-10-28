"""OpenAI Codex adapter for playbook_hook.py --client codex.

Codex runs command hooks from ~/.codex/hooks.json with a JSON event on stdin, close to Claude Code's
shape. Sources, read 2026-10-28: https://learn.chatgpt.com/docs/hooks and github.com/openai/codex
(codex-rs/hooks/src/engine/output_parser.rs, codex-rs/hooks/schema/generated/*.json,
codex-rs/core/src/tools/hook_names.rs, codex-rs/core/src/tools/handlers/apply_patch.rs). Differences:

  - PreToolUse cannot ask: Codex treats "ask" as a failed hook and runs the call. So CAN_ASK is False and
    the core denies with a one-time approval code instead; render() never emits "ask".
  - Shell commands arrive as tool_name "Bash" with tool_input.command. On Windows Codex runs them in
    PowerShell (codex-rs/shell-command/src/shell_detect.rs), so they are judged as the PowerShell tool.
  - File edits arrive as tool_name "apply_patch" with the patch text in tool_input.command (also when the
    model runs `apply_patch <<'EOF'` through the shell). Each file the patch touches becomes the call the
    policy knows: Add File -> Write, Update File -> Edit, Move to -> Edit of the old path + Write of the new
    one, Delete File -> Write with empty content (so a deletion is judged like an overwrite).
  - Stop accepts only decision/reason and the common fields, so extra context becomes a systemMessage.
  - permission_mode is dropped: in every mode the approval code is the only way to approve a tier-2 call.
  - A subagent's events (agent_id set) keep their own state, and its prompts (written by the parent agent,
    not the user) approve nothing.
  - write_stdin types into a terminal that exec_command left open, and Codex sends no PreToolUse for it
    (codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs), so those keystrokes skip every check.
    Codex accepts typed text only in a session started with tty=true (process_manager.rs: "stdin is closed
    for this session"). Two layers close that: the installer sets features.unified_exec_tty = false (no
    tty sessions at all), and this adapter denies a command that would open an interactive shell or REPL
    (the hook input does not carry the tty flag, so the command text is what can be judged). If a Codex
    version ever sends write_stdin to the hook, its text is judged as a shell command.
"""

from __future__ import annotations

import json
import os
import re
import time

CAN_ASK = False
DEADLINE = 25  # seconds: playbook_hook.py denies before Codex's 30 s PreToolUse timeout would let the call run

EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart"}
COPIED = ("hook_event_name", "session_id", "cwd", "prompt", "last_assistant_message", "stop_hook_active",
          "source", "tool_name", "tool_input", "tool_response")
DENY_EVENT = "CodexDeny"  # our own event: render() denies the call with event["reason"]

PATCH_START = "*** Begin Patch"
PATCH_LINE = re.compile(r"^\s*\*\*\* (Add File|Delete File|Update File|Move to): (.+?)\s*$")
SHELL_PATCH = re.compile(r"""^\s*(?:cd\s+("[^"]*"|'[^']*'|\S+)\s*&&\s*)?(?:apply_patch|applypatch)\b""")


# --- apply_patch -----------------------------------------------------------------

def parse_patch(text: str) -> list[dict]:
    """Codex's patch format -> [{"op": add|delete|update, "path", "move_to", "removed", "added"}]."""
    start = text.find(PATCH_START)
    if start < 0:
        return []
    ops: list[dict] = []
    for line in text[start:].splitlines()[1:]:
        if line.strip() == "*** End Patch":
            break
        m = PATCH_LINE.match(line)
        if m and m.group(1) == "Move to":
            if ops:
                ops[-1]["move_to"] = m.group(2)
        elif m:
            ops.append({"op": m.group(1).split()[0].lower(), "path": m.group(2), "move_to": None,
                        "removed": [], "added": []})
        elif ops and line.startswith(("+", "-")):
            ops[-1]["added" if line[0] == "+" else "removed"].append(line[1:])
    return ops


def patch_calls(ops: list[dict]) -> list[tuple[str, dict]]:
    """Each file change as the Claude-style call the policy judges."""
    calls = []
    for op in ops:
        added, removed = "\n".join(op["added"]), "\n".join(op["removed"])
        if op["op"] == "add":
            calls.append(("Write", {"file_path": op["path"], "content": added}))
        elif op["op"] == "delete":
            calls.append(("Write", {"file_path": op["path"], "content": "", "delete": True}))
        else:
            calls.append(("Edit", {"file_path": op["path"], "old_string": removed, "new_string": added}))
            if op["move_to"]:
                calls.append(("Write", {"file_path": op["move_to"], "content": added}))
    return calls


def patch_event(event: dict, patch: str) -> dict:
    """One PreToolUse event for the core. A patch touching one file becomes that file's call. With several,
    each is judged here first; the one needing approval is passed on (the core then denies it with a code),
    and a patch with two or more such changes is denied with a request to split it, so that each change
    gets its own approval. tool_input keeps the whole patch, so an approval covers only this exact patch."""
    ops = parse_patch(patch)
    calls = patch_calls(ops)
    if not calls:
        return event  # not a patch the policy can read; Codex rejects malformed patches itself
    pick = calls[0]
    if len(calls) > 1:
        import playbook_hook as core  # the same config the core uses: data paths, tracked files, created files

        sid = str(event.get("session_id") or "")
        ctx = core.config(event["cwd"], sid)
        risky, deadline = [], time.monotonic() + 8  # the hook's own timeout is 30 s
        for tool, tool_input in calls:
            if time.monotonic() > deadline:
                return {**event, "hook_event_name": DENY_EVENT, "reason": (
                    "this patch changes too many files to check in time (tier-2: cannot verify). "
                    "Apply it as several smaller patches.")}
            decision, reason = core.policy.classify(tool, tool_input, ctx)
            if decision == core.policy.ASK:
                risky.append(((tool, tool_input), wording(tool_input, reason)))
        if len(risky) > 1:
            return {**event, "hook_event_name": DENY_EVENT, "reason": (
                f"this patch makes {len(risky)} changes that each need the user's approval: "
                + "; ".join(r for _, r in risky)
                + ". Apply them as separate patches, one file each, so the user can approve each one.")}
        if risky:
            pick = risky[0][0]
        else:  # all allowed: remember the files this patch creates, as the core does for one Write
            made = [op["path"] for op in ops if op["op"] == "add"] + [op["move_to"] for op in ops if op["move_to"]]
            created = [os.path.abspath(os.path.join(event["cwd"], os.path.expanduser(p))) for p in made]
            created = [p for p in created if not os.path.lexists(p)]
            if created:
                state = core.load_state(sid)
                state["created"] = sorted(set(state.get("created", [])) | set(created))[-500:]
                core.save_state(sid, state)
    tool, tool_input = pick
    return {**event, "tool_name": tool, "tool_input": {**tool_input, "patch": patch}}


def wording(tool_input: dict, reason: str) -> str:
    """A deletion is judged as an overwrite with nothing; say what it really does."""
    return reason.replace("overwrites a file", "deletes a file") if tool_input.get("delete") else reason


# --- interactive sessions (write_stdin) --------------------------------------------

POSIX_SHELLS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "mksh", "ash", "csh", "tcsh", "nu", "xonsh", "elvish"}
PYTHON = re.compile(r"(?:python|pypy)[\d.]*|py|ipython[\d.]*|bpython")
SCRIPT_LANGS = {"ruby", "perl", "php", "lua", "luajit", "julia", "r", "scala", "clojure", "clj", "erl", "racket",
                "guile", "sbcl", "ocaml", "swipl", "groovysh", "kotlinc", "deno", "bun"}
EVAL_FLAGS = {"-c", "-e", "-m", "-r", "-p", "-E", "--eval", "--print", "--version", "-V", "--help", "-h"}  # code given
DB_CLIENTS = {"sqlite3", "psql", "mysql", "mariadb", "mongosh", "mongo", "redis-cli", "cqlsh", "sqlcmd", "duckdb"}
DB_EVAL = {"-c", "--command", "-e", "--execute", "-f", "--file", "--eval", "-cmd", "-Q", "-q", "-i", "--version",
           "--help"}  # a query or a file of queries given: runs it and exits
ALWAYS = {"irb", "ghci", "jshell", "tmux", "screen", "byobu", "telnet", "ftp", "vim", "vi", "nvim", "nano", "emacs",
          "micro", "enter-pssession", "etsn", "bc", "dc", "gdb", "lldb"}
WRAPPERS = {"env", "nohup", "time", "exec", "command", "builtin", "nice", "stdbuf", "timeout", "caffeinate"}
SSH_VALUE = {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R",
             "-S", "-W", "-w"}
SUDO_VALUE = {"-u", "-g", "-p", "-C", "-D", "-r", "-t", "-U", "-h"}


def program(word: str) -> str:
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith((".exe", ".cmd", ".bat", ".com")) else name


def session_opener(words: list[str], depth: int = 0) -> str | None:
    """The program this simple command starts as an interactive session (a shell, REPL or remote login that
    reads further commands from the terminal), or None for a one-shot command."""
    while words and (re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]) or program(words[0]) in WRAPPERS):
        words = words[1:]
        while words and words[0].startswith("-"):
            words = words[1:]
        if words and re.fullmatch(r"[\d.]+[smhd]?", words[0]):  # timeout 5 bash
            words = words[1:]
    if not words:
        return None
    name, args = program(words[0]), words[1:]
    low = [a.lower() for a in args]
    if name in ("sudo", "doas"):
        rest, i = [], 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in SUDO_VALUE else 1
        rest = args[i:]
        if not rest:
            return name if any(a in ("-i", "-s", "--login", "--shell") for a in low) else None
        return session_opener(rest, depth)
    if name in ALWAYS:
        return name
    if name in POSIX_SHELLS:
        if any(re.fullmatch(r"-[a-z]*c[a-z]*", a) for a in args):  # -c, -lc, -ic: one command, then exit
            inner = next((args[i + 1] for i, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-z]*c[a-z]*", a)), "")
            return interactive_in(inner, depth + 1)
        skip = {i + 1 for i, a in enumerate(args) if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file")}
        script = [a for i, a in enumerate(args) if not a.startswith(("-", "+")) and i not in skip]
        return name if not script or "-s" in args or "-i" in args else None
    if name in ("pwsh", "powershell"):
        if any(a.startswith("-noe") for a in low):  # -NoExit keeps it open after the command
            return name
        for i, a in enumerate(low):
            if a in ("-c", "-command", "-cwa", "-commandwithargs") or a.startswith("-com"):
                return interactive_in(" ".join(args[i + 1:]), depth + 1) if args[i + 1:] != ["-"] else name
            if a in ("-f", "-file", "-e", "-ec", "-enc", "-encodedcommand") or a.startswith(("-fi", "-enc")):
                return None
        values = {"-executionpolicy", "-ex", "-ep", "-workingdirectory", "-wd", "-inputformat", "-outputformat",
                  "-of", "-if", "-windowstyle", "-w", "-version", "-v", "-configurationname", "-psconsolefile"}
        skip = {i + 1 for i, a in enumerate(low) if a in values}
        rest = [a for i, a in enumerate(args) if not a.startswith("-") and i not in skip]
        return interactive_in(" ".join(rest), depth + 1) if rest else name
    if name == "cmd":
        if "/k" in low:
            return name
        for i, a in enumerate(low):
            if a in ("/c", "/r"):
                return interactive_in(" ".join(args[i + 1:]), depth + 1)
        return name
    if name == "wsl":
        values = {"-d", "--distribution", "-u", "--user", "--cd"}
        skip = {i + 1 for i, a in enumerate(low) if a in values}
        rest = [a for i, a in enumerate(args) if i not in skip and a not in values]
        if rest[:1] in (["-e"], ["--exec"], ["--"]):
            return session_opener(rest[1:], depth) if rest[1:] else name
        rest = [a for a in rest if not a.startswith("-") and a != "~"]
        return session_opener(rest, depth) if rest else name
    if PYTHON.fullmatch(name) or name in ("node", "nodejs") or name in SCRIPT_LANGS:
        if "-i" in args or "--interactive" in low or (name == "php" and "-a" in args):
            return name
        if name in ("deno", "bun"):
            return name if not args or args[0] == "repl" else None
        if any(a in EVAL_FLAGS or (a.startswith("--") and a.split("=")[0] in EVAL_FLAGS) for a in args):
            return None
        values = {"-W", "-X", "--check-hash-based-pycs", "--require", "--import", "--loader"}
        skip = {i + 1 for i, a in enumerate(args) if a in values}
        script = [a for i, a in enumerate(args) if not a.startswith("-") and i not in skip]
        return None if script else name
    if name in DB_CLIENTS:
        if any(a.split("=")[0] in DB_EVAL for a in args):
            return None
        positional = [a for a in args if not a.startswith("-")]
        if name == "sqlite3" or name == "duckdb":
            return None if len(positional) >= 2 else name
        if name == "redis-cli":
            values = {"-h", "-p", "-a", "-n", "-u", "--user", "--pass"}
            skip = {i + 1 for i, a in enumerate(args) if a in values}
            return None if [a for i, a in enumerate(args) if not a.startswith("-") and i not in skip] else name
        return name
    if name == "ssh":
        i, positional = 0, []
        while i < len(args):
            if args[i] in SSH_VALUE:
                i += 2
                continue
            if not args[i].startswith("-"):
                positional.append(args[i])
            i += 1
        return name if len(positional) <= 1 else None
    if name == "sftp":
        return None if "-b" in args else name
    if name == "su":
        return None if any(a in ("-c", "--command") for a in low) else name
    if name in ("docker", "podman", "nerdctl"):
        positional = [a for a in args if not a.startswith("-")]
        sub = positional[0] if positional else ""
        if sub == "attach" or (sub in ("run", "exec", "start", "container") and any(
                a in ("--interactive", "--attach") or re.fullmatch(r"-[a-z]*[ia][a-z]*", a) for a in args)):
            return f"{name} {sub}"
        return None
    if name in ("kubectl", "oc"):
        positional = [a for a in args if not a.startswith("-")]
        if positional[:1] in (["exec"], ["attach"], ["run"]) and any(
                a in ("--stdin", "--tty") or re.fullmatch(r"-[a-z]*[it][a-z]*", a) for a in args):
            return f"{name} {positional[0]}"
    return None


def interactive_in(command: str, depth: int = 0) -> str | None:
    """The first program in a shell command line that would start an interactive session. Commands that
    read stdin from a pipe, a file or a heredoc are not interactive: the terminal is not their input."""
    if depth > 3 or not command.strip():
        return None
    import policy  # the core's quote-aware tokenizer (the same folder)

    text, _ = policy.split_heredocs(command)
    if os.name == "nt":
        text = text.replace("\\", "/")  # PowerShell: a backslash is a path separator, not an escape
    for line in policy.logical_lines(text):
        segment: list[str] = []
        piped = False
        for tok in policy.tokenize(line) + [";"]:
            if tok in policy.SEPARATORS:
                if segment and not piped and not any(t.startswith("<") for t in segment):
                    if (found := session_opener(segment, depth)):
                        return found
                segment, piped = [], tok in ("|", "|&")
            else:
                segment.append(tok)
    return None


def interactive_reason(found: str) -> str:
    return (f"`{found}` would start an interactive session. Codex's write_stdin can then type commands into it "
            "that no hook checks (tier-2: cannot verify them). Run one-shot commands instead: a script file, "
            "`python -c \"...\"`, `bash -c \"...\"`, `powershell -Command \"...\"`. If the user needs an "
            "interactive session, they can open it themselves.")


# --- normalize / render ----------------------------------------------------------

def normalize(raw: dict) -> dict | None:
    """Codex's hook input -> a Claude Code-shaped event, or None to ignore it."""
    name = raw.get("hook_event_name")
    if name not in EVENTS:
        return None
    event = {k: raw[k] for k in COPIED if k in raw}
    if not isinstance(event.get("cwd"), str) or not os.path.isdir(event["cwd"]):
        event["cwd"] = os.getcwd()
    if raw.get("agent_id"):  # a subagent: its own turn state and approval codes
        event["session_id"] = f"{raw.get('session_id') or ''}.agent.{raw['agent_id']}"
        if name == "UserPromptSubmit":
            event["prompt"] = ""  # written by the parent agent: it must not approve a code
    if name not in ("PreToolUse", "PostToolUse"):
        return event
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "write_stdin" and name == "PreToolUse":  # not sent by Codex today; if it is, judge the keystrokes
        typed = str(tool_input.get("chars") or tool_input.get("input") or "")
        if not typed.strip("\r\n\x03\x04"):
            return event  # a poll, Enter or Ctrl-C/Ctrl-D: nothing new to run
        tool, tool_input = "Bash", {"command": typed.strip("\r\n")}
        event = {**event, "tool_name": tool, "tool_input": tool_input}
    command = str(tool_input.get("command") or "")
    if tool == "Bash" and name == "PreToolUse":
        try:
            found = "exec_command with tty=true" if tool_input.get("tty") is True else interactive_in(command)
        except Exception as exc:  # noqa: BLE001 - a command that cannot be read is not waved through
            return {**event, "hook_event_name": DENY_EVENT, "reason": (
                f"the policy hook could not read this command ({type(exc).__name__}), so it cannot vouch for it.")}
        if found:
            return {**event, "hook_event_name": DENY_EVENT, "reason": interactive_reason(found)}
    if tool == "Bash" and PATCH_START in command and (m := SHELL_PATCH.match(command)):
        tool = "apply_patch"  # `apply_patch <<'EOF'` run through the shell: Codex applies it as a patch
        if m.group(1):
            event["cwd"] = os.path.normpath(os.path.join(event["cwd"], m.group(1).strip("'\"")))
    if tool == "apply_patch":
        if name == "PostToolUse":
            return {**event, "tool_name": "apply_patch"}
        try:
            return patch_event(event, command)
        except Exception as exc:  # noqa: BLE001 - an unreadable patch must not pass unchecked
            return {**event, "hook_event_name": DENY_EVENT, "reason": (
                f"the policy hook could not read this patch ({type(exc).__name__}), so it cannot vouch for it. "
                "Apply it as several smaller patches, or ask the user to make the change.")}
    if tool == "Bash" and os.name == "nt":
        event["tool_name"] = "PowerShell"
    return event


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def render(event: dict | None, outputs: list[dict], raw: dict | None) -> tuple[str, int]:
    """Our Claude-shaped outputs -> (codex's stdout text, exit code). Codex rejects JSON with fields it does
    not know for the event, and a rejected PreToolUse answer lets the call run, so only known fields go out."""
    name = (event or {}).get("hook_event_name")
    if name == DENY_EVENT:
        return dump(deny("agent-playbook rule 2.3: " + str(event.get("reason")))), 0
    reasons, contexts, block = [], [], []
    for out in outputs:
        hso = out.get("hookSpecificOutput") if isinstance(out.get("hookSpecificOutput"), dict) else {}
        if hso.get("permissionDecision") in ("deny", "ask"):  # "ask" only comes from an internal error
            reasons.append(wording((event or {}).get("tool_input") or {},
                                   str(hso.get("permissionDecisionReason") or "agent-playbook rule 2.3")))
        if hso.get("additionalContext"):
            contexts.append(str(hso["additionalContext"]))
        if out.get("decision") == "block":
            block.append(str(out.get("reason") or "agent-playbook: the reply is not finished"))
    if name == "PreToolUse":
        if reasons and (raw or {}).get("agent_id"):  # the user never sees a subagent's turn
            reasons.append("You are a subagent, so the user cannot approve this here: stop and report the "
                           "blocked action to the main agent, which can ask the user and run it.")
        if reasons:
            return dump(deny(" ".join(reasons))), 0
        if contexts:
            return dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "additionalContext": "\n".join(contexts)}}), 0
    elif name == "Stop":
        if block:
            return dump({"decision": "block", "reason": " ".join(block)}), 0
        if contexts:  # Stop has no additionalContext in Codex: show it to the user instead
            return dump({"systemMessage": "\n".join(contexts)}), 0
    elif name in ("SessionStart", "UserPromptSubmit", "PostToolUse") and contexts:
        return dump({"hookSpecificOutput": {"hookEventName": name, "additionalContext": "\n".join(contexts)}}), 0
    return "", 0


def dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=True)  # ASCII-only, as the core writes for Claude Code
