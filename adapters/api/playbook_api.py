"""Reference adapter: the playbook on the raw Claude API, without Claude Code.

The same text and the same checks as the Claude Code adapter:
  - system prompt: dist/global/rules/agent-playbook.md (core) plus the public profiles,
    plus any profile files you pass (for example your own profile file, ~/my-profile.md)
  - before every tool call: policy.classify (rules 2.3, 5.1); an "ask" goes to approve()
  - after a turn that changed something: report.check (rule 1.3); after every turn:
    report.ask_check (rule 2.2, numbered questions, as for headless Claude Code); one retry for both

Tools are Anthropic's bash and text editor. Commands run on this machine, in `root`,
one fresh shell per command, with secret-looking environment variables removed.
Run it in a container or VM, not next to data you care about.

    pip install anthropic
    python adapters/api/playbook_api.py --root PATH [--profile ~/my-profile.md] [--no-rules]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "hooks"))
import playbook_hook  # noqa: E402
import policy  # noqa: E402
import report  # noqa: E402

DEFAULT_MODEL = "claude-opus-5-5"
RULES_FILE = ROOT / "dist" / "global" / "rules" / "agent-playbook.md"
TOOLS = [{"type": "bash_20250124", "name": "bash"},
         {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}]
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|^ANTHROPIC_", re.I)
MAX_OUTPUT = 30_000
COMMAND_TIMEOUT = 120
TOOL_TOKENS = 3000  # upper guess for the two tool definitions
# USD per million tokens: input, output, cache read. Cache writes cost 1.25x input.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01)}
FALLBACK_PRICE = (5.0, 25.0, 0.50)  # refusal fallbacks may run on another model; priced at the highest Opus rate


def price(model: str) -> tuple[float, float, float]:
    return PRICES.get(model, FALLBACK_PRICE)


def find_bash() -> str | None:
    """POSIX: /bin/bash (or the default sh). Windows: Git Bash, because the policy parses POSIX
    shell syntax and would misread cmd.exe; WSL's System32 bash is a different system."""
    if os.name != "nt":
        return "/bin/bash" if os.path.exists("/bin/bash") else None
    candidates = [shutil.which("bash") or ""] + [
        os.path.join(os.environ.get(v, ""), "Git", "bin", "bash.exe") for v in ("ProgramFiles", "ProgramW6432")]
    for c in candidates:
        if c and os.path.exists(c) and "system32" not in c.lower() and "windowsapps" not in c.lower():
            return c
    raise RuntimeError("Git Bash not found. Install Git for Windows; the adapter will not run commands in cmd.exe.")


PUBLIC_PROFILE_FILES = [ROOT / "src" / "profiles" / f"{p}.md" for p in ("lang-zh-CN", "advisor")]


def system_prompt(profiles: list[str] | None = None, rules: bool = True) -> str:
    """Core plus the public profiles (what M4 measured), plus any profile files you pass.
    HTML comment lines are removed, as the byte budget counts them."""
    paths = ([RULES_FILE, *PUBLIC_PROFILE_FILES] if rules else []) + [Path(p) for p in profiles or []]
    parts = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        parts.append("".join(l for l in text.splitlines(keepends=True) if not l.startswith("<!--")).strip())
    return "\n\n".join(p for p in parts if p)


def scrubbed_env() -> dict[str, str]:
    """Drop secret-looking variables. GIT_CONFIG_COUNT/KEY_n/VALUE_n go together: dropping only
    the KEY_n ones (they match "KEY") would leave a count that breaks every git command."""
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k) and not k.startswith("GIT_CONFIG_")}


def ask_terminal(tool: str, tool_input: dict, reason: str) -> bool:
    print(f"\nagent-playbook rule 2.3: {reason}\n  {tool}: {json.dumps(tool_input, ensure_ascii=False)[:500]}")
    return input("Approve? [y/N] ").strip().lower() in ("y", "yes")


def as_hook_call(name: str, inp: dict) -> tuple[str, dict]:
    """Map an API tool call onto the Claude Code tool the policy knows."""
    if name == "bash":
        return "Bash", {"command": str(inp.get("command", ""))}
    path = str(inp.get("path", ""))
    command = inp.get("command")
    if command == "create":
        return "Write", {"file_path": path, "content": str(inp.get("file_text", ""))}
    if command in ("str_replace", "insert"):
        return "Edit", {"file_path": path, "new_string": str(inp.get("new_str") or inp.get("insert_text") or "")}
    return "Read", {"file_path": path}


def block_attr(block, name, default=None):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


@dataclass
class Turn:
    text: str
    stop_reason: str
    model: str
    events: list[dict]
    usage: dict
    usd: float = 0.0
    report_retry: bool = False
    fallback_ran: bool = False


@dataclass
class Session:
    root: str
    client: object = None
    model: str = DEFAULT_MODEL
    system: str = ""
    approve: object = ask_terminal
    enforce_report: bool = True    # one retry: rule 1.3 after a change, rule 2.2 after any turn
    plain_refusals: bool = False   # measurement: a declined call says only "declined", naming no rule
    fallbacks: bool = True         # server-side refusal fallback ("default" routing)
    effort: str = "high"
    max_tokens: int = 16000
    max_steps: int = 40
    budget_usd: float | None = None  # stop before a model call that could pass this
    spent_usd: float = 0.0
    messages: list = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    created: set = field(default_factory=set)

    def __post_init__(self):
        self.root = os.path.realpath(self.root)
        self.bash = find_bash()

    def worst_case_usd(self) -> float:
        """Upper bound for the next call: every input token uncached, the full max_tokens out."""
        pin, pout, _ = tuple(map(max, price(self.model), FALLBACK_PRICE)) if self.fallbacks else price(self.model)
        size = len(json.dumps(self.messages, default=lambda o: getattr(o, "__dict__", str(o))).encode("utf-8"))
        tokens_in = (size + len(self.system.encode("utf-8"))) / 3 + TOOL_TOKENS
        return (tokens_in * pin + self.max_tokens * pout) / 1e6

    # --- model calls -------------------------------------------------------------

    def create(self):
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, tools=TOOLS, messages=self.messages,
                      output_config={"effort": self.effort}, cache_control={"type": "ephemeral"})
        if self.system:
            kwargs["system"] = self.system
        if self.fallbacks:
            return self.client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.messages.create(**kwargs)

    def run(self, user_text: str) -> Turn:
        start = len(self.events)
        self.messages.append({"role": "user", "content": user_text})
        self.log({"type": "user", "text": user_text})
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        changed = retried = fallback_ran = False
        response, text, stop, usd = None, "", "max_steps", 0.0
        for _ in range(self.max_steps):
            if self.budget_usd is not None and self.spent_usd + self.worst_case_usd() > self.budget_usd:
                stop = "budget"
                self.log({"type": "budget_stop", "spent_usd": round(self.spent_usd, 4)})
                break
            response = self.create()
            cost, fell_back = add_usage(usage, getattr(response, "usage", None), self.model)
            usd += cost
            self.spent_usd += cost
            fallback_ran |= fell_back
            content = list(getattr(response, "content", []) or [])
            self.messages.append({"role": "assistant", "content": content})
            text = "".join(block_attr(b, "text", "") for b in content if block_attr(b, "type") == "text")
            if text:
                self.log({"type": "assistant", "text": text})
            stop = getattr(response, "stop_reason", "")
            if stop == "tool_use":
                results = []
                for block in content:
                    if block_attr(block, "type") == "tool_use":
                        result, did_change = self.call(block_attr(block, "name"), block_attr(block, "input") or {})
                        changed |= did_change
                        results.append({"type": "tool_result", "tool_use_id": block_attr(block, "id"), **result})
                self.messages.append({"role": "user", "content": results})
                continue
            if stop in ("end_turn", "stop_sequence") and self.enforce_report and not retried:
                reason = " ".join(r for r in (report.check(text) if changed else None,
                                              report.ask_check(text, asked=False, interactive=False)) if r)
                if reason:
                    retried = True
                    self.messages.append({"role": "user", "content": reason})
                    self.log({"type": "report_retry", "reason": reason})
                    continue
            break
        self.close_dangling_tool_calls(stop)
        return Turn(text=text, stop_reason=stop, model=getattr(response, "model", self.model),
                    events=self.events[start:], usage=usage, usd=round(usd, 4), report_retry=retried,
                    fallback_ran=fallback_ran)

    def close_dangling_tool_calls(self, stop: str) -> None:
        """A turn cut off mid tool call (max_tokens, refusal) would leave a tool_use with no
        result, and every later request would be rejected. Answer it, keeping history append-only."""
        last = self.messages[-1] if self.messages else {}
        if last.get("role") != "assistant":
            return
        ids = [block_attr(b, "id") for b in last["content"] if block_attr(b, "type") == "tool_use"]
        if ids:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i, "content": f"Not run: the response stopped ({stop}).",
                 "is_error": True} for i in ids]})

    # --- tools -------------------------------------------------------------------

    def call(self, name: str, inp: dict) -> tuple[dict, bool]:
        tool, tool_input = as_hook_call(name, inp)
        event = {"type": "tool", "tool": tool, "input": tool_input}
        try:
            if name != "bash":  # judge the file the editor will really touch, not a symlink to it
                tool_input["file_path"] = self.confine(tool_input["file_path"]) or tool_input["file_path"]
            ctx = playbook_hook.config(self.root)
            ctx.created = set(self.created)
            event["decision"], event["reason"] = policy.classify(tool, tool_input, ctx)
        except Exception as exc:  # like the hook: a policy error asks instead of allowing
            event["decision"], event["reason"] = policy.ASK, f"policy error: {exc}"
        if event["decision"] == policy.ASK:
            event["approved"] = bool(self.approve(tool, tool_input, event["reason"]))
            if not event["approved"]:
                event.update(ran=False, is_error=True)
                self.log(event)
                note = "" if self.plain_refusals else f" agent-playbook rule 2.3: {event['reason']}"
                return {"content": "Not run: the user declined." + note, "is_error": True}, False
        try:
            if tool == "Write" and (full := self.confine(tool_input["file_path"])) and not os.path.lexists(full):
                self.created.add(full)
            output = self.run_bash(inp) if name == "bash" else self.edit(inp)
            is_error = False
        except Exception as exc:  # any tool failure goes back to the model as an error result
            output, is_error = f"Error: {exc}", True
        event.update(ran=True, is_error=is_error, output=output[:2000])
        self.log(event)
        changed = not is_error and (tool in ("Write", "Edit") or
                                    tool == "Bash" and not playbook_hook.is_read_only(tool_input["command"]))
        return {"content": output[:MAX_OUTPUT] or "(no output)", "is_error": is_error}, changed

    def run_bash(self, inp: dict) -> str:
        if inp.get("restart"):
            return "Restarted. Each command runs in a fresh shell in the project root."
        command = str(inp.get("command", ""))
        argv = [self.bash, "-c", command] if self.bash else command
        # Output goes to a file, so a background job (cmd &) cannot hold the call open.
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(argv, shell=not self.bash, cwd=self.root, env=scrubbed_env(), stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
            try:
                code = proc.wait(timeout=COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait()
                code = f"killed after {COMMAND_TIMEOUT}s"
            out.seek(0)
            text = out.read().decode("utf-8", errors="replace")
        return f"{text}\n[exit code {code}]"

    def confine(self, path: str) -> str | None:
        full = os.path.realpath(os.path.join(self.root, os.path.expanduser(path)))
        return full if full == self.root or full.startswith(self.root + os.sep) else None

    def edit(self, inp: dict) -> str:
        path = self.confine(str(inp.get("path", "")))
        if path is None:
            raise ValueError(f"path is outside the project root {self.root}")
        command = inp.get("command")
        p = Path(path)
        if command == "view":
            if p.is_dir():
                return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            lo, hi = (inp.get("view_range") or [1, len(lines)])[:2]
            hi = len(lines) if hi in (-1, None) else hi
            return "\n".join(f"{n}\t{lines[n - 1]}" for n in range(max(lo, 1), min(hi, len(lines)) + 1))
        if command == "create":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(inp.get("file_text", "")), encoding="utf-8")
            return f"Created {inp.get('path')}"
        text = p.read_text(encoding="utf-8")
        if command == "str_replace":
            old = str(inp.get("old_str", ""))
            n = text.count(old) if old else 0
            if n != 1:
                raise ValueError(f"old_str found {n} times; it must match exactly once")
            p.write_text(text.replace(old, str(inp.get("new_str", "")), 1), encoding="utf-8")
            return "Replaced 1 occurrence"
        if command == "insert":
            lines = text.splitlines(keepends=True)
            at = int(inp.get("insert_line", 0))
            if not 0 <= at <= len(lines):
                raise ValueError(f"insert_line {at} is outside 0..{len(lines)}")
            new = str(inp.get("insert_text") or inp.get("new_str") or "")
            lines.insert(at, new if new.endswith("\n") else new + "\n")
            p.write_text("".join(lines), encoding="utf-8")
            return f"Inserted after line {at}"
        raise ValueError(f"unknown command {command!r}")

    # --- transcript ----------------------------------------------------------------

    def log(self, event: dict) -> None:
        self.events.append(event)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            for event in self.events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def add_usage(total: dict, usage, model: str) -> tuple[float, bool]:
    """Add one response's usage (per attempt when fallbacks ran); return (USD, fallback served it)."""
    if usage is None:
        return 0.0, False
    iterations = block_attr(usage, "iterations") or []
    usd = 0.0
    for part in iterations or [usage]:
        pin, pout, pcache = FALLBACK_PRICE if block_attr(part, "type") == "fallback_message" else price(model)
        n = {key: block_attr(part, key, 0) or 0 for key in total}
        for key in total:
            total[key] += n[key]
        usd += (n["input_tokens"] * pin + n["output_tokens"] * pout + n["cache_read_input_tokens"] * pcache
                + n["cache_creation_input_tokens"] * pin * 1.25) / 1e6
    return usd, any(block_attr(i, "type") == "fallback_message" for i in iterations)


KEY_VAR = "PB_ANTHROPIC_API_KEY"  # its own name, so a key set for measurement never bills Claude Code itself


def make_client():
    """Only the measurement key: with an explicit api_key the SDK reads no other credential."""
    key = os.environ.get(KEY_VAR)
    if not key:
        raise SystemExit(f"{KEY_VAR} is not set. Nothing was sent; no other key is used.")
    import anthropic  # imported here so tests and dry runs need no SDK
    return anthropic.Anthropic(api_key=key)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--root", required=True, help="project directory the agent works in")
    ap.add_argument("--profile", action="append", default=[], help="your own profile file, e.g. ~/my-profile.md (repeatable)")
    ap.add_argument("--no-rules", action="store_true", help="baseline: no playbook text")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--transcript", help="write the event log (JSONL) here on exit")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = Session(root=args.root, client=make_client(), model=args.model,
                      system=system_prompt(args.profile, rules=not args.no_rules))
    try:
        while True:
            try:
                prompt = input("\n> ")
            except EOFError:
                break
            if prompt.strip():
                turn = session.run(prompt)
                print(turn.text or f"[stopped: {turn.stop_reason}]")
    finally:
        if args.transcript:
            session.save(args.transcript)
    return 0


if __name__ == "__main__":
    sys.exit(main())
