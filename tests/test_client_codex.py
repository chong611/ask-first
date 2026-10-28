"""OpenAI Codex support: the hook adapter (src/hooks/client_codex.py) and the install target (tools/target_codex.py).

Payload and answer shapes follow Codex's own schemas, read 2026-10-28:
  https://learn.chatgpt.com/docs/hooks
  https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated  (*.command.input/output.schema.json)
  https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/hook_names.rs  (Bash, apply_patch)
  https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/apply_patch.rs  (tool_input.command)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "hooks"
sys.path.insert(0, str(ROOT / "tools"))
import build  # noqa: E402
import install  # noqa: E402
import target_codex  # noqa: E402

# Fields Codex accepts in each event's answer (output schemas above, additionalProperties: false). Anything
# else, or permissionDecision "ask", makes Codex fail the hook, and a failed PreToolUse hook lets the call run.
ANSWER_FIELDS = {
    "PreToolUse": ({"continue", "decision", "hookSpecificOutput", "reason", "stopReason", "suppressOutput",
                    "systemMessage"},
                   {"additionalContext", "hookEventName", "permissionDecision", "permissionDecisionReason",
                    "updatedInput"}),
    "PostToolUse": ({"continue", "decision", "hookSpecificOutput", "reason", "stopReason", "suppressOutput",
                     "systemMessage"}, {"additionalContext", "hookEventName", "updatedMCPToolOutput"}),
    "UserPromptSubmit": ({"continue", "decision", "hookSpecificOutput", "reason", "stopReason", "suppressOutput",
                          "systemMessage"}, {"additionalContext", "hookEventName"}),
    "Stop": ({"continue", "decision", "reason", "stopReason", "suppressOutput", "systemMessage"}, set()),
    "SessionStart": ({"continue", "hookSpecificOutput", "stopReason", "suppressOutput", "systemMessage"},
                     {"additionalContext", "hookEventName"}),
}
CODE = re.compile(r'approve ([A-Z0-9]{4})"')


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd,
                   capture_output=True, check=True, timeout=60)


class CodexHook(unittest.TestCase):
    """playbook_hook.py --client codex run as Codex runs it: a JSON event on stdin, the answer on stdout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hooks = self.tmp / "hooks"
        self.hooks.mkdir()
        for f in SRC.glob("*.py"):
            shutil.copy2(f, self.hooks / f.name)
        self.state = self.tmp / "state-home"
        self.home = self.tmp / "home"  # the user's home folder, as ~ expands inside the hook
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "AGENTS.md").write_text("my own lines\n", encoding="utf-8")
        self.cwd = self.tmp / "repo"
        self.cwd.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self):
        return {**os.environ, "AGENT_PLAYBOOK_HOME": str(self.state), "HOME": str(self.home),
                "USERPROFILE": str(self.home)}

    def run_hook(self, payload):
        out = subprocess.run([sys.executable, str(self.hooks / "playbook_hook.py"), "--client", "codex"],
                             input=json.dumps(payload).encode(), capture_output=True, timeout=60, env=self.env())
        self.assertEqual(out.returncode, 0, out.stderr.decode(errors="replace")[-800:])
        text = out.stdout.decode().strip()
        if not text:
            return None
        answer = json.loads(text)
        self.assert_codex_accepts(payload["hook_event_name"], answer)
        return answer

    def assert_codex_accepts(self, event, answer):
        top, specific = ANSWER_FIELDS[event]
        self.assertLessEqual(set(answer), top, answer)
        hso = answer.get("hookSpecificOutput")
        if hso is not None:
            self.assertLessEqual(set(hso), specific, answer)
            self.assertEqual(hso["hookEventName"], event)
            self.assertNotEqual(hso.get("permissionDecision"), "ask")

    def base(self, event, **fields):
        # Every Codex event carries these (input schemas); turn_id on turn-scoped events.
        return {"session_id": "019a0c4e-codex-session", "transcript_path": None, "cwd": str(self.cwd),
                "hook_event_name": event, "model": "gpt-5-codex", "permission_mode": "default",
                "turn_id": "turn-1", **fields}

    def pre(self, tool_name, tool_input, **fields):
        return self.base("PreToolUse", tool_name=tool_name, tool_input=tool_input, tool_use_id="call_1", **fields)

    def bash(self, command, **fields):
        return self.run_hook(self.pre("Bash", {"command": command}, **fields))

    def patch(self, body, **fields):
        return self.run_hook(self.pre("apply_patch", {"command": f"*** Begin Patch\n{body}*** End Patch\n"},
                                      **fields))

    def prompt(self, text, **fields):
        return self.run_hook(self.base("UserPromptSubmit", prompt=text, **fields))

    def denied_code(self, answer):
        self.assertIsNotNone(answer, "expected a deny")
        hso = answer["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny", answer)
        m = CODE.search(hso["permissionDecisionReason"])
        self.assertIsNotNone(m, hso["permissionDecisionReason"])
        return m.group(1)

    # --- the five cases the spec names -------------------------------------------------------

    def test_force_push_is_denied_with_an_approval_code(self):
        code = self.denied_code(self.bash("git push --force origin main"))
        self.assertEqual(len(code), 4)

    def test_harmless_command_gets_no_decision(self):
        self.assertIsNone(self.bash("git status"))

    def test_patch_editing_codex_agents_md_is_denied_with_a_code(self):
        agents = (self.home / ".codex" / "AGENTS.md").as_posix()
        answer = self.patch(f"*** Update File: {agents}\n@@\n-my own lines\n+other lines\n")
        self.denied_code(answer)
        self.assertIn("AGENTS.md", answer["hookSpecificOutput"]["permissionDecisionReason"])

    def test_stop_after_a_change_without_the_report_blocks(self):
        self.prompt("fix the typo")
        self.run_hook(self.base("PostToolUse", tool_name="apply_patch", tool_use_id="call_1",
                                tool_input={"command": "*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch\n"},
                                tool_response="Success. Updated the following files:\nA a.txt"))
        answer = self.run_hook(self.base("Stop", stop_hook_active=False, last_assistant_message="Fixed it."))
        self.assertEqual(answer["decision"], "block")
        self.assertIn("missing headings", answer["reason"])

    def test_user_approval_lets_the_identical_call_through_once(self):
        code = self.denied_code(self.bash("git push --force origin main"))
        self.assertEqual(self.denied_code(self.bash("git push --force origin main")), code)  # same call, same code
        self.prompt(f"好的，批准 {code}")
        self.assertIsNone(self.bash("git push --force origin main"))  # allowed: no decision
        self.denied_code(self.bash("git push --force origin main"))  # used up

    # --- more of what Codex sends ------------------------------------------------------------

    def test_approved_patch_goes_through_once(self):
        agents = (self.home / ".codex" / "AGENTS.md").as_posix()
        body = f"*** Update File: {agents}\n@@\n-my own lines\n+other lines\n"
        code = self.denied_code(self.patch(body))
        self.prompt(f"approve {code}")
        self.assertIsNone(self.patch(body))
        self.denied_code(self.patch(body))

    def test_new_file_in_the_project_is_allowed(self):
        self.assertIsNone(self.patch("*** Add File: notes/plan.md\n+# Plan\n"))

    def test_deleting_a_file_git_cannot_restore_is_denied(self):
        (self.cwd / "data.csv").write_text("a,b\n", encoding="utf-8")  # untracked: the only copy
        answer = self.patch("*** Delete File: data.csv\n")
        self.denied_code(answer)
        self.assertIn("deletes a file git cannot restore: data.csv",
                      answer["hookSpecificOutput"]["permissionDecisionReason"])

    def test_secret_in_a_patch_is_denied(self):
        self.denied_code(self.patch("*** Add File: config.py\n+API_KEY = 'sk-" + "a" * 40 + "'\n"))

    def test_patch_with_two_approvals_needed_must_be_split(self):
        (self.cwd / "data.csv").write_text("a,b\n", encoding="utf-8")
        agents = (self.home / ".codex" / "AGENTS.md").as_posix()
        answer = self.patch(f"*** Delete File: data.csv\n*** Update File: {agents}\n@@\n-my own lines\n+x\n")
        reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("separate patches", reason)
        self.assertIn("deletes a file git cannot restore: data.csv", reason)
        self.assertNotRegex(reason, CODE)  # nothing to approve as a whole

    def test_patch_with_one_approval_needed_names_that_change(self):
        git(self.cwd, "init", "-q")
        (self.cwd / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(self.cwd, "add", "app.py")
        git(self.cwd, "commit", "-q", "-m", "init")
        agents = (self.home / ".codex" / "AGENTS.md").as_posix()
        body = f"*** Update File: app.py\n@@\n-x = 1\n+x = 2\n*** Update File: {agents}\n@@\n-my own lines\n+x\n"
        answer = self.patch(body)
        code = self.denied_code(answer)
        self.assertIn("AGENTS.md", answer["hookSpecificOutput"]["permissionDecisionReason"])
        self.prompt(f"批准 {code}")
        self.assertIsNone(self.patch(body))

    def test_files_a_patch_creates_count_as_the_agents_own(self):
        self.assertIsNone(self.patch("*** Add File: a.txt\n+1\n*** Add File: b.txt\n+2\n"))
        (self.cwd / "a.txt").write_text("1\n", encoding="utf-8")  # Codex applied the patch
        (self.cwd / "b.txt").write_text("2\n", encoding="utf-8")
        self.assertIsNone(self.patch("*** Delete File: b.txt\n"))  # not user data: this session made it

    def test_apply_patch_run_through_the_shell_is_judged_as_a_patch(self):
        command = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: AGENTS.md\n+new rules\n*** End Patch\nEOF\n"
        self.denied_code(self.bash(command))

    @unittest.skipUnless(os.name == "nt", "Codex runs shell commands in PowerShell on Windows only")
    def test_windows_commands_are_judged_as_powershell(self):
        target = str(self.home / ".codex" / "AGENTS.md")  # unquoted backslashes: a shell escape in Bash
        self.assertNotIn(" ", target)
        self.denied_code(self.bash(f"Set-Content -Path {target} -Value x"))

    def test_bypass_mode_still_gets_an_approval_code(self):
        self.denied_code(self.bash("git push --force origin main", permission_mode="bypassPermissions"))

    def test_a_subagent_cannot_approve_its_own_call(self):
        sub = {"agent_id": "019a0c4f-sub", "agent_type": "default"}
        answer = self.bash("git push --force origin main", **sub)
        code = self.denied_code(answer)
        self.assertIn("subagent", answer["hookSpecificOutput"]["permissionDecisionReason"])
        self.prompt(f"approve {code}", **sub)  # the parent agent wrote this, not the user
        self.denied_code(self.bash("git push --force origin main", **sub))

    def test_mcp_tool_that_acts_outside_is_denied(self):
        self.denied_code(self.run_hook(self.pre("mcp__github__create_pull_request", {"title": "x"})))

    def test_stop_extra_context_becomes_a_system_message(self):
        self.prompt("how do I run it?")
        message = "Run these:\n```powershell\ncd app && python main.py\n```\n"
        answer = self.run_hook(self.base("Stop", stop_hook_active=False, last_assistant_message=message))
        self.assertEqual(set(answer), {"systemMessage"})
        self.assertIn("PowerShell", answer["systemMessage"])

    def test_session_start_after_compaction_adds_context(self):
        answer = self.run_hook({k: v for k, v in self.base("SessionStart", source="compact").items()
                                if k != "turn_id"})
        self.assertIn("compaction", answer["hookSpecificOutput"]["additionalContext"])

    def test_events_we_do_not_handle_get_no_answer(self):
        self.assertIsNone(self.run_hook(self.base("SubagentStop", stop_hook_active=False, agent_id="a",
                                                  agent_type="default", last_assistant_message="x")))
        self.assertIsNone(self.run_hook(self.base("PermissionRequest", tool_name="Bash",
                                                  tool_input={"command": "git push --force"})))

    # --- write_stdin: typing into an open terminal skips PreToolUse ----------------------------

    def interactive_denied(self, answer):
        self.assertIsNotNone(answer, "expected a deny")
        reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("write_stdin", reason)
        self.assertNotRegex(reason, CODE)  # no code: the user opens interactive sessions themselves

    def test_command_that_opens_an_interactive_shell_is_denied(self):
        for command in ("bash", "powershell -NoExit", "python", "cmd /k", "docker exec -it web sh", "cd app && node"):
            with self.subTest(command=command):
                self.interactive_denied(self.bash(command))

    def test_one_shot_commands_still_pass(self):
        for command in ("python script.py", "python -c \"print(1)\"", "bash -lc \"ls\"", "git status",
                        "echo 1 | python"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))

    def test_a_terminal_request_is_denied(self):
        self.interactive_denied(self.run_hook(self.pre("Bash", {"command": "npm test", "tty": True})))

    def test_write_stdin_text_is_judged_as_a_command(self):
        """Codex sends no PreToolUse for write_stdin today (write_stdin.rs); if a version does, the typed text
        is checked like any command."""
        typed = {"session_id": 3, "chars": "git push --force origin main\n", "yield_time_ms": 250}
        self.denied_code(self.run_hook(self.pre("write_stdin", typed)))
        self.assertIsNone(self.run_hook(self.pre("write_stdin", {"session_id": 3, "chars": ""})))  # a poll


class CodexInteractive(unittest.TestCase):
    """client_codex.interactive_in(): which commands open a session that write_stdin could drive."""

    OPENS = ["bash", "sh", "zsh -i", "bash -l", "powershell", "pwsh -NoExit", "pwsh -NoProfile",
             "powershell -NoExit -Command Get-Date", "cmd", "cmd /k", "python", "python3 -i script.py", "py -3.12",
             r"C:\Py\python.exe" if os.name == "nt" else "/usr/bin/python3", "ipython", "node", "node -i", "deno",
             "deno repl", "bun repl", "irb", "ruby", "perl", "php -a", "R", "sqlite3 app.db", "psql", "mysql -u root",
             "redis-cli", "mongosh", "ssh host", "ssh -p 22 user@host", "telnet h", "sftp host", "su", "sudo -i",
             "sudo bash", "sudo -u root sh", "env FOO=1 bash", "timeout 5 python", "exec zsh",
             "docker run -it ubuntu bash", "docker exec -it web sh", "kubectl exec -it pod -- sh", "tmux",
             "vim notes.txt", "wsl", "wsl -d Ubuntu", "cd app && node", "git status; python", "bash -c python",
             "bash -lc 'cd x && bash'", "powershell -Command python", "cmd /c python", "(bash)",
             "Enter-PSSession srv", "& 'C:/Program Files/PowerShell/7/pwsh.exe'", "python -",
             "powershell -Command -"]
    ONE_SHOT = ["git status", "python script.py", "python -c 'print(1)'", "python -m pytest -q", "py -3 tool.py",
                "node app.js", "node -e 'console.log(1)'", "bash -lc 'ls -la'", "bash script.sh", "sh -c 'echo hi'",
                "powershell -NoProfile -Command Get-Date", "pwsh -File build.ps1", "powershell Get-ChildItem",
                "cmd /c dir", "echo 1 | python", "Get-Content x.py | python", "python < in.txt",
                "cat <<EOF | python\nprint(1)\nEOF", "python <<'EOF'\nprint(1)\nEOF", "ssh host ls -la",
                "docker run --rm alpine echo hi", "docker ps", "kubectl get pods", "sqlite3 app.db 'select 1'",
                "psql -c 'select 1'", "mysql -e 'select 1'", "redis-cli ping", "sudo apt-get update",
                "deno run x.ts", "npm test", "Get-ChildItem -Recurse", "wsl ls", "wsl -e ls", "ruby -e 'p 1'",
                "perl script.pl", "nohup python worker.py", "python --version", "node --version",
                "apply_patch <<'EOF'\n*** Begin Patch\n*** End Patch\nEOF"]

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SRC))
        import client_codex
        cls.client = client_codex

    def test_session_openers(self):
        for command in self.OPENS:
            with self.subTest(command=command):
                self.assertTrue(self.client.interactive_in(command))

    def test_one_shot_commands(self):
        for command in self.ONE_SHOT:
            with self.subTest(command=command):
                self.assertIsNone(self.client.interactive_in(command))

    def test_deadline_is_inside_the_hook_timeout(self):
        self.assertLess(self.client.DEADLINE, target_codex.EVENTS["PreToolUse"][1])


class CodexRender(unittest.TestCase):
    """render() on its own: Codex's answer for each of our Claude-shaped outputs."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SRC))
        import client_codex
        cls.client = client_codex

    def test_ask_becomes_deny(self):
        ask = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                      "permissionDecisionReason": "agent-playbook rule 2.3: internal error"}}
        text, code = self.client.render({"hook_event_name": "PreToolUse"}, [ask], {})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_no_output_means_no_answer(self):
        for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart"):
            self.assertEqual(self.client.render({"hook_event_name": event}, [], {}), ("", 0))
        self.assertEqual(self.client.render(None, [], None), ("", 0))

    def test_parse_patch(self):
        ops = self.client.parse_patch("*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n"
                                      "*** Delete File: c.py\n*** Add File: d.py\n+*** Update File: not.py\n"
                                      "*** End Patch\n")
        self.assertEqual([(o["op"], o["path"], o["move_to"]) for o in ops],
                         [("update", "a.py", "b.py"), ("delete", "c.py", None), ("add", "d.py", None)])
        self.assertEqual(ops[2]["added"], ["*** Update File: not.py"])  # content, not a header


CONFIG = (b'model = "gpt-5-codex"\n\n[plugins."code-review@openai-bundled"]\nenabled = true\n\n[features]\n'
          b"js_repl = false\n\n[mcp_servers.node_repl]\nargs = []\ncommand = 'C:\\tools\\node_repl.exe'\n")


class CodexConfig(unittest.TestCase):
    """config.toml gets one marked line, unified_exec_tty = false; taking it out gives back every byte."""

    def round_trip(self, text, after_header=True):
        import tomllib
        new, note = target_codex.config_with_tty_off(text)
        self.assertIsNone(note)
        settings = tomllib.loads(new)
        self.assertIs(settings["features"]["unified_exec_tty"], False)
        before = tomllib.loads(text or "")
        self.assertEqual({k: v for k, v in settings.items() if k != "features"},
                         {k: v for k, v in before.items() if k != "features"})
        self.assertEqual(target_codex.config_without_ours(new), text or "")  # byte for byte
        self.assertEqual(target_codex.config_with_tty_off(new), (None, None))  # a second install adds nothing
        return new

    def test_line_goes_under_the_existing_features_table(self):
        text = CONFIG.decode()
        new = self.round_trip(text)
        self.assertEqual(new.replace(target_codex.TTY_LINE + "\n", ""), text)  # one line added, nothing else
        self.assertIn("[features]\n" + target_codex.TTY_LINE + "\njs_repl = false", new)

    def test_crlf_file_keeps_crlf(self):
        new = self.round_trip(CONFIG.decode().replace("\n", "\r\n"))
        self.assertIn(target_codex.TTY_LINE + "\r\n", new)
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_new_features_table_when_there_is_none(self):
        self.round_trip('model = "gpt-5-codex"\n')
        self.round_trip('model = "gpt-5-codex"')  # no line break at the end
        self.round_trip("[features]")  # header is the last line, no line break
        self.round_trip("")
        self.round_trip(None)

    def test_the_users_own_setting_is_left_alone(self):
        self.assertEqual(target_codex.config_with_tty_off("[features]\nunified_exec_tty = false\n"), (None, None))
        new, note = target_codex.config_with_tty_off("[features]\nunified_exec_tty = true\n")
        self.assertIsNone(new)
        self.assertIn("WARNING", note)

    def test_files_it_cannot_edit_safely_are_left_alone(self):
        for text in ("features.js_repl = false\n", "features = { js_repl = false }\n", "[features\nbroken",
                     'x = """\n[features]\n"""\n'):
            with self.subTest(text=text):
                new, note = target_codex.config_with_tty_off(text)
                self.assertIsNone(new)
                self.assertIn("yourself", note)


class CodexTarget(unittest.TestCase):
    """tools/target_codex.py on a temporary home that already has the user's own Codex files."""

    USER_LINES = "第一行：我的个人规则。\n第二行。\n第三行。\n第四行，没有换行结尾。"
    FOREIGN = {"description": "my hooks", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python my_guard.py", "timeout": 5}]}]}}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.codex = self.home / ".codex"
        (self.codex / "rules").mkdir(parents=True)
        (self.codex / "AGENTS.md").write_bytes(self.USER_LINES.encode())  # LF, as the owner's files are
        (self.codex / "hooks.json").write_bytes(json.dumps(self.FOREIGN, indent=4).encode())
        (self.codex / "config.toml").write_bytes(CONFIG)
        for name, text in (("auth.json", "{}"), ("rules/default.rules", 'prefix_rule(pattern = ["ls"], decision = "allow")\n')):
            (self.codex / name).write_text(text, encoding="utf-8")
        self.out = build.outputs()
        self.python = Path(sys.executable).as_posix()

    @staticmethod
    def apply_keeping_line_breaks(plan):
        """install.apply, but each file is written with exactly the line breaks in the plan (Path.write_text
        on Windows turns every \\n into \\r\\n; install.py is to be fixed for that, see the report)."""
        from unittest import mock
        real = Path.write_text

        def write_text(self, data, encoding=None, errors=None, newline=None):
            return real(self, data, encoding=encoding, errors=errors, newline="")
        with mock.patch.object(Path, "write_text", write_text):
            return install.apply(plan)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, profiles=("lang-zh-CN",)):
        return target_codex.plan_global(self.home, self.out, self.python, list(profiles))

    def hooks(self):
        return json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))

    def test_install_keeps_the_users_text_and_hooks_and_is_idempotent(self):
        plan = self.plan()
        for untouchable in ("auth.json", "rules/default.rules"):
            self.assertNotIn(self.codex / untouchable, plan)
        self.assertEqual(plan[self.codex / "config.toml"],
                         CONFIG.decode().replace("[features]\n", "[features]\n" + target_codex.TTY_LINE + "\n"))
        self.assertTrue(any("config.toml" in line for line in install.diff(plan)))  # shown before --yes
        install.apply(plan)
        agents = (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents.startswith(self.USER_LINES))
        block = install.core_of(agents)
        self.assertIn("# agent-playbook core", block)
        self.assertIn("# profile: lang-zh-CN", block)  # profiles sit inside the block, so they leave with it
        hooks = self.hooks()["hooks"]
        self.assertEqual(self.hooks()["description"], "my hooks")
        self.assertEqual(hooks["PreToolUse"][0], self.FOREIGN["hooks"]["PreToolUse"][0])
        self.assertEqual(set(hooks), {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart"})
        ours = hooks["PreToolUse"][1]["hooks"][0]
        self.assertTrue(install.is_ours(ours))
        self.assertIn(f'"{self.python}"' if os.name == "nt" else self.python, ours["command"])
        self.assertTrue(ours["command"].endswith("agent-playbook/hooks/playbook_hook.py\" --client codex")
                        if os.name == "nt" else ours["command"].endswith("--client codex"))
        self.assertTrue((self.codex / "agent-playbook/hooks/client_codex.py").exists())
        self.assertTrue((self.home / ".agents/skills/pb-status/SKILL.md").exists())
        rules = (self.codex / "rules/agent-playbook.rules").read_text(encoding="utf-8")
        self.assertIn('pattern = ["git", "push", "--force"]', rules)
        self.assertEqual(install.diff(self.plan()), [])  # a second run changes nothing

    def test_uninstall_gives_back_every_file_byte_for_byte(self):
        before = {p: p.read_bytes() for p in self.codex.rglob("*") if p.is_file()}
        self.apply_keeping_line_breaks(self.plan())
        (self.codex / "agent-playbook/hooks/__pycache__").mkdir()
        (self.codex / "agent-playbook/hooks/__pycache__/policy.cpython-312.pyc").write_bytes(b"x")
        plan, notes = target_codex.plan_uninstall(self.home)
        self.apply_keeping_line_breaks(plan)
        after = {p: p.read_bytes() for p in self.codex.rglob("*") if p.is_file() and ".bak-agent-playbook" not in p.name}
        self.assertEqual(after, before)  # config.toml, AGENTS.md, hooks.json, auth.json, default.rules: all identical
        self.assertFalse((self.codex / "agent-playbook").exists())
        self.assertFalse((self.home / ".agents/skills").exists())
        self.assertEqual(target_codex.plan_uninstall(self.home)[0], {})  # nothing left to remove
        self.assertTrue(any("backup" in n for n in notes))

    def test_fresh_home_install_then_uninstall_leaves_nothing_of_ours(self):
        for name in ("AGENTS.md", "hooks.json", "config.toml"):
            (self.codex / name).unlink()
        install.apply(self.plan(profiles=()))
        self.assertTrue((self.codex / "AGENTS.md").read_text(encoding="utf-8").startswith("<!-- agent-playbook:core"))
        self.assertIn(target_codex.TTY_LINE, (self.codex / "config.toml").read_text(encoding="utf-8"))
        install.apply(target_codex.plan_uninstall(self.home)[0])
        for name in ("AGENTS.md", "hooks.json", "config.toml"):
            self.assertFalse((self.codex / name).exists(), name)

    def test_uninstall_keeps_later_changes_to_config_toml(self):
        self.apply_keeping_line_breaks(self.plan())
        config = self.codex / "config.toml"
        trusted = '\n[hooks.state."user:pre_tool_use:0:0"]\ntrusted_hash = "sha256:abc"\n'  # Codex records trust
        config.write_bytes(config.read_bytes().replace(b'gpt-5-codex', b'gpt-5.5') + trusted.encode())
        self.apply_keeping_line_breaks(target_codex.plan_uninstall(self.home)[0])
        self.assertEqual(config.read_bytes(), CONFIG.replace(b'gpt-5-codex', b'gpt-5.5') + trusted.encode())

    def test_update_keeps_one_block(self):
        install.apply(self.plan())
        install.apply(self.plan(profiles=("lang-zh-CN", "advisor")))
        agents = (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents.count("<!-- agent-playbook:core"), 1)
        self.assertIn("# profile: advisor", install.core_of(agents))
        self.assertEqual(len(self.hooks()["hooks"]["Stop"]), 1)
        install.apply(target_codex.plan_uninstall(self.home)[0])
        self.assertEqual((self.codex / "AGENTS.md").read_text(encoding="utf-8"), self.USER_LINES)

    def test_broken_hooks_json_stops_before_writing(self):
        (self.codex / "hooks.json").write_text("{ // my comment\n}", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.plan()

    def test_override_file_is_warned_about(self):
        (self.codex / "AGENTS.override.md").write_text("team rules\n", encoding="utf-8")
        self.plan()
        self.assertTrue(any("AGENTS.override.md" in n for n in target_codex.notes()))

    def test_installed_hook_command_runs_as_codex_runs_it(self):
        """The exact hooks.json command string, through %COMSPEC% /C "..." (Windows) or $SHELL -lc (elsewhere),
        as codex-rs/hooks/src/engine/command_runner.rs runs it."""
        install.apply(self.plan())
        command = self.hooks()["hooks"]["PreToolUse"][1]["hooks"][0]["command"]
        payload = {"session_id": "s", "transcript_path": None, "cwd": str(self.tmp), "hook_event_name": "PreToolUse",
                   "model": "gpt-5-codex", "permission_mode": "default", "turn_id": "t", "tool_name": "Bash",
                   "tool_input": {"command": "git push --force origin main"}, "tool_use_id": "c"}
        env = {**os.environ, "AGENT_PLAYBOOK_HOME": str(self.tmp / "state")}
        if os.name == "nt":
            args = f'{os.environ.get("COMSPEC", "cmd.exe")} /C "{command}"'  # passed verbatim, as raw_arg does
        else:
            args = [os.environ.get("SHELL", "/bin/sh"), "-lc", command]
        out = subprocess.run(args, input=json.dumps(payload).encode(), capture_output=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stderr.decode(errors="replace")[-800:])
        answer = json.loads(out.stdout)
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
