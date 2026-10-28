"""Gemini CLI support: the hook adapter (src/hooks/client_gemini.py) and the install target
(tools/target_gemini.py). No network: the hook runs as a subprocess with --client gemini.

The event payloads follow Gemini CLI's hook input and output schemas:
  https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
  https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/hooks/types.ts
and its tool parameters:
  https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/definitions/base-declarations.ts
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "hooks"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tools"))
import build  # noqa: E402
import client_gemini  # noqa: E402
import install  # noqa: E402
import policy  # noqa: E402
import target_gemini  # noqa: E402

CODE = re.compile(r'"approve ([A-Z0-9]{4})"')
SHELL = "PowerShell" if os.name == "nt" else "Bash"


def gemini_event(name, cwd, **fields):
    """The fields every Gemini hook input carries (reference.md, "Base input schema")."""
    return {"session_id": "6f1c2a9e-0d1b-4c51-9a63-1f7d2b0c8e11", "transcript_path": "", "cwd": cwd,
            "hook_event_name": name, "timestamp": "2026-10-28T09:15:00.000Z", **fields}


# --- the hook, run as Gemini CLI runs it ------------------------------------------------------------


class GeminiHookTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cwd = tempfile.mkdtemp()
        for path in (self.home, self.cwd):
            self.addCleanup(shutil.rmtree, path, True)
        subprocess.run(["git", "init", "-q", self.cwd], check=True, capture_output=True)

    def hook(self, raw):
        data = raw if isinstance(raw, bytes) else json.dumps(raw).encode()
        out = subprocess.run([sys.executable, str(SRC / "playbook_hook.py"), "--client", "gemini"], input=data,
                             capture_output=True, timeout=60, cwd=self.cwd,
                             env={**os.environ, "AGENT_PLAYBOOK_HOME": self.home})
        self.assertEqual(out.returncode, 0, out.stderr.decode(errors="replace")[-500:])
        answer = json.loads(out.stdout)  # one JSON object, nothing else on stdout
        self.assertIsInstance(answer, dict)
        return answer

    def tool(self, name, tool_input, after=False, **fields):
        return self.hook(gemini_event("AfterTool" if after else "BeforeTool", self.cwd, tool_name=name,
                                      tool_input=tool_input, **fields))

    def shell(self, command):
        return self.tool("run_shell_command", {"command": command, "description": "run it"})

    def prompt(self, text):
        return self.hook(gemini_event("BeforeAgent", self.cwd, prompt=text))

    def reply(self, text, retry=False):
        return self.hook(gemini_event("AfterAgent", self.cwd, prompt="fix the typo", prompt_response=text,
                                      stop_hook_active=retry))

    def code_of(self, answer):
        self.assertEqual(answer.get("decision"), "deny", answer)
        code = CODE.search(answer["reason"]).group(1)
        self.assertIn(f"批准 {code}", answer["systemMessage"])  # the user sees the code in the terminal
        return code

    def test_force_push_is_denied_with_an_approval_code(self):
        answer = self.shell("git push --force origin main")
        self.code_of(answer)
        self.assertIn("rule 2.3", answer["reason"])

    def test_harmless_calls_get_no_decision(self):
        self.assertEqual(self.shell("git status"), {})
        self.assertEqual(self.tool("read_file", {"file_path": "README.md"}), {})
        self.assertEqual(self.tool("write_file", {"file_path": os.path.join(self.cwd, "notes.txt"),
                                                  "content": "hello"}), {})

    def test_writes_to_gemini_settings_and_context_are_denied_with_a_code(self):
        settings = os.path.join(self.home, ".gemini", "settings.json")
        self.code_of(self.tool("write_file", {"file_path": settings, "content": "{}"}))
        self.code_of(self.tool("replace", {"file_path": os.path.join(self.cwd, "GEMINI.md"), "old_string": "a",
                                           "new_string": "b", "instruction": "edit"}))
        self.code_of(self.shell("echo {} > ~/.gemini/settings.json"))
        self.code_of(self.tool("write_file", {"file_path": os.path.join(
            self.home, ".gemini", "extensions", "agent-playbook", "hooks", "hooks.json"), "content": "{}"}))

    def test_user_approval_lets_the_identical_call_through_once(self):
        code = self.code_of(self.shell("git push --force origin main"))
        self.assertEqual(self.prompt(f"好的，批准 {code}"), {})  # BeforeAgent never answers with a decision
        self.assertEqual(self.shell("git push --force origin main"), {})
        self.code_of(self.shell("git push --force origin main"))  # used up

    def test_an_approval_inside_our_own_retry_prompt_approves_nothing(self):
        code = self.code_of(self.shell("git push --force origin main"))
        self.assertEqual(self.prompt(f"{client_gemini.MARK} approve {code}"), {})
        self.code_of(self.shell("git push --force origin main"))

    def test_missing_report_after_a_change_sends_the_reply_back_once(self):
        self.prompt("fix the typo in notes.txt")
        path = os.path.join(self.cwd, "notes.txt")
        Path(path).write_text("fixed\n", encoding="utf-8")
        self.assertEqual(self.tool("write_file", {"file_path": path, "content": "fixed\n"}, after=True,
                                   tool_response={"llmContent": "ok", "returnDisplay": "ok"}), {})
        answer = self.reply("Fixed the typo.")
        self.assertEqual(answer["decision"], "deny")  # AfterAgent deny = Gemini retries with the reason
        self.assertTrue(answer["reason"].startswith(client_gemini.MARK))
        self.assertIn("Rule 1.3", answer["reason"])
        # Gemini sends the reason back as a prompt, which fires BeforeAgent; that is not a new user turn
        self.assertEqual(self.prompt(answer["reason"]), {})
        self.assertEqual(self.reply("Fixed the typo. Still no report.", retry=True), {})  # once per turn

    def test_our_retry_prompt_does_not_restart_the_turn_so_a_rule_blocks_once(self):
        self.prompt("check the branch")
        answer = self.reply("查完了。要不要我推送？")  # a decision left in the text (rule 2.2)
        self.assertIn("Rule 2.2", answer["reason"])
        self.assertEqual(self.prompt(answer["reason"]), {})  # Gemini's retry fires BeforeAgent again
        self.assertEqual(self.reply("查完了。要不要我推送？", retry=True), {})  # no second block, no loop
        self.prompt("and now?")  # a real user message starts a new turn: the rule applies again
        self.assertIn("Rule 2.2", self.reply("要不要我推送？")["reason"])

    def test_a_reply_with_the_report_passes(self):
        self.prompt("fix the typo")
        path = os.path.join(self.cwd, "notes.txt")
        Path(path).write_text("fixed\n", encoding="utf-8")
        self.tool("write_file", {"file_path": path, "content": "fixed\n"}, after=True)
        report = ("Fixed.\n\nDone · 1\n- notes.txt typo fixed; checked by reading the file\n\nNot done · 0\n\n"
                  "Unverified · 0\n\nFound but not fixed · 0\n\nWaiting on you · 0\n")
        self.assertEqual(self.reply(report), {})

    def test_mcp_tools_that_act_outside_are_denied_with_a_code(self):
        context = {"server_name": "github", "tool_name": "create_pull_request", "command": "github-mcp-server"}
        self.code_of(self.tool("mcp_github_create_pull_request", {"title": "x"}, mcp_context=context))
        self.assertEqual(self.tool("mcp_github_get_issue", {"issue_number": 1},
                                   mcp_context={"server_name": "github", "tool_name": "get_issue"}), {})

    def test_pipe_exit_advice_comes_after_the_call(self):
        answer = self.tool("run_shell_command", {"command": "python -m unittest discover 2>&1 | tail -5"},
                           after=True, tool_response={"llmContent": "OK"})
        self.assertIn("exit status", answer["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(answer["hookSpecificOutput"]["hookEventName"], "AfterTool")

    def test_malformed_input_still_answers_with_json_and_exit_0(self):
        for raw in (b"", b"not json", b"[1, 2]", b"null", b'{"hook_event_name": 5}',
                    b'{"hook_event_name": "BeforeTool", "tool_name": 5, "tool_input": "x"}',
                    b'{"hook_event_name": "BeforeTool", "tool_name": "run_shell_command", "tool_input": {"command": 7}}',
                    b'{"hook_event_name": "AfterAgent", "prompt_response": ["x"], "stop_hook_active": "yes"}',
                    b'{"hook_event_name": "PreCompress", "trigger": "auto"}'):
            with self.subTest(raw=raw):
                self.hook(raw)
        bom = b"\xef\xbb\xbf" + json.dumps(gemini_event("BeforeTool", self.cwd, tool_name="run_shell_command",
                                                        tool_input={"command": "git reset --hard"})).encode()
        self.code_of(self.hook(bom))  # PowerShell 5.1 pipes add a BOM


# --- the adapter, in-process -------------------------------------------------------------------------


class MapToolTest(unittest.TestCase):
    def test_tools_map_to_the_names_and_inputs_the_policy_reads(self):
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd, True)
        os.mkdir(os.path.join(cwd, "sub"))
        m = client_gemini.map_tool
        self.assertEqual(m("run_shell_command", {"command": "ls", "description": "list"}, cwd=cwd),
                         (SHELL, {"command": "ls"}, cwd))
        self.assertEqual(m("run_shell_command", {"command": "ls", "dir_path": "sub"}, cwd=cwd),
                         (SHELL, {"command": "ls", "dir_path": "sub"}, os.path.join(cwd, "sub")))
        self.assertEqual(m("write_file", {"file_path": "a", "content": "b"}), ("Write", {"file_path": "a", "content": "b"}, ""))
        self.assertEqual(m("replace", {"file_path": "a", "old_string": "x", "new_string": "y", "instruction": "i"}),
                         ("Edit", {"file_path": "a", "old_string": "x", "new_string": "y"}, ""))
        self.assertEqual(m("save_memory", {"fact": "f"})[:2], ("Edit", {"file_path": "~/.gemini/GEMINI.md", "new_string": "f"}))
        self.assertEqual(m("read_file", {"file_path": "a.py"})[:2], ("Read", {"file_path": "a.py"}))
        self.assertEqual(m("read_many_files", {"include": ["a.py", ".env"]})[:2], ("Read", {"file_path": ".env"}))
        self.assertEqual(m("grep_search", {"pattern": "KEY", "include_pattern": ".env", "dir_path": "."})[1]["path"], ".env")
        self.assertEqual(m("grep_search", {"pattern": "x", "dir_path": "src", "names_only": True})[1],
                         {"pattern": "x", "path": "src", "output_mode": "files_with_matches"})
        self.assertEqual(m("glob", {"pattern": "*.py", "dir_path": "src"})[:2], ("Glob", {"pattern": "*.py", "path": "src"}))
        self.assertEqual(m("ask_user", {"questions": [{"question": "Q1?"}]})[0], "AskUserQuestion")
        self.assertEqual(m("mcp_my_server_create_issue", {}, {"server_name": "my_server", "tool_name": "create_issue"})[0],
                         "mcp__my_server__create_issue")
        self.assertEqual(m("mcp_github_merge_pull_request", {})[0], "mcp__github__merge_pull_request")
        self.assertEqual(m("web_fetch", {"prompt": "x"})[:2], ("web_fetch", {"prompt": "x"}))
        self.assertEqual(m("write_file", "not a dict")[:2], ("Write", {"file_path": "", "content": ""}))


class NormalizeRenderTest(unittest.TestCase):
    def test_events_become_claude_code_events(self):
        n = client_gemini.normalize
        stop = n(gemini_event("AfterAgent", ".", prompt="p", prompt_response="r", stop_hook_active=True))
        self.assertEqual((stop["hook_event_name"], stop["last_assistant_message"], stop["stop_hook_active"]),
                         ("Stop", "r", True))
        self.assertTrue(stop["session_id"].startswith("gemini-"))
        self.assertEqual(n(gemini_event("BeforeAgent", ".", prompt="hi"))["hook_event_name"], "UserPromptSubmit")
        self.assertIsNone(n(gemini_event("BeforeAgent", ".", prompt=client_gemini.MARK + " Rule 1.3")))
        self.assertIsNone(n(gemini_event("AfterTool", ".", tool_name="read_file", tool_input={})))  # changes nothing
        self.assertEqual(n(gemini_event("AfterTool", ".", tool_name="replace", tool_input={}))["tool_name"], "Edit")
        self.assertEqual(n(gemini_event("SessionStart", ".", source="startup"))["source"], "startup")
        self.assertIsNone(n(gemini_event("BeforeModel", ".")))
        self.assertIsNone(n(["not", "a", "dict"]))

    def test_render_answers(self):
        r = client_gemini.render

        def deny(decision):
            return [{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                            "permissionDecisionReason": "agent-playbook rule 2.3: x"}}]
        pre, stop = {"hook_event_name": "PreToolUse"}, {"hook_event_name": "Stop"}
        self.assertEqual(json.loads(r(pre, deny("deny"), {})[0])["decision"], "deny")
        self.assertEqual(json.loads(r(pre, deny("ask"), {})[0])["decision"], "ask")  # the core's own error path
        self.assertEqual(r(pre, [], {}), ("{}", 0))  # never "allow"
        self.assertEqual(r({"hook_event_name": "UserPromptSubmit"}, deny("deny"), {}), ("{}", 0))
        answer = json.loads(r(stop, [{"decision": "block", "reason": "Rule 1.3: x"}], {})[0])
        self.assertEqual((answer["decision"], answer["reason"]), ("deny", client_gemini.MARK + " Rule 1.3: x"))
        advice = [{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "use if ($?)"}}]
        self.assertEqual(json.loads(r(stop, advice, {})[0])["decision"], "deny")
        context = [{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "open items"}}]
        self.assertEqual(json.loads(r({"hook_event_name": "SessionStart"}, context, {})[0])
                         ["hookSpecificOutput"]["additionalContext"], "open items")
        self.assertEqual(r(pre, ["junk", None, 3], {}), ("{}", 0))
        text, code = r(pre, deny("deny"), {})
        self.assertEqual(code, 0)
        self.assertTrue(text.isascii())

    def test_an_unreadable_before_tool_asks(self):
        client_gemini._failed = "TypeError: boom"
        self.addCleanup(setattr, client_gemini, "_failed", None)
        answer = json.loads(client_gemini.render(None, [], {"hook_event_name": "BeforeTool"})[0])
        self.assertEqual(answer["decision"], "ask")
        self.assertEqual(client_gemini.render(None, [], {"hook_event_name": "AfterAgent"}), ("{}", 0))

    def test_gemini_files_count_as_own_instructions(self):
        ctx = policy.Context(cwd=tempfile.gettempdir(), tracked=set())
        write = lambda p: policy.classify("Write", {"file_path": p, "content": "x"}, ctx)[0]  # noqa: E731
        for path in ("~/.gemini/settings.json", "~/.gemini/policies/my.toml", "~/.gemini/GEMINI.md", "proj/GEMINI.md",
                     "~/.gemini/extensions/agent-playbook/hooks/hooks.json", "~/.gemini/trustedFolders.json",
                     "/etc/gemini-cli/settings.json", "C:/ProgramData/gemini-cli/policies/a.toml",
                     "proj/.gemini/commands/x.toml", "proj/.agents/skills/x/SKILL.md", "~/.claude/settings.json"):
            with self.subTest(path=path):
                self.assertEqual(write(path), policy.ASK)
        for path in ("notes.md", "~/.gemini/tmp/proj/plans/plan.md", "src/gemini_client.py"):
            with self.subTest(path=path):
                self.assertEqual(write(path), policy.ALLOW)


# --- the install target -----------------------------------------------------------------------------


def run_install(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = install.main(list(args))
    return code, out.getvalue()


class TargetBuildTest(unittest.TestCase):
    def test_outputs_stay_under_dist_gemini(self):
        out = target_gemini.outputs(build)
        self.assertTrue(all(rel.startswith("gemini/") for rel in out))
        ext = "gemini/extensions/agent-playbook/"
        manifest = json.loads(out[ext + "gemini-extension.json"])
        self.assertEqual((manifest["name"], manifest["version"], manifest["contextFileName"]),
                         ("agent-playbook", build.VERSION, "GEMINI.md"))
        self.assertIn("End of agent-playbook core.", out[ext + "GEMINI.md"])
        self.assertEqual({rel.split("/")[-2] for rel in out if rel.endswith("SKILL.md")}, set(build.skills()))

    def test_policy_file_asks_for_every_claude_code_ask_rule(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11")
        rules = tomllib.loads(target_gemini.policy_toml(build.ASK_RULES))["rule"]
        self.assertTrue(all(r["decision"] == "ask_user" and r["priority"] == 999 for r in rules))
        shell = next(r for r in rules if r["toolName"] == "run_shell_command")
        for rule in build.ASK_RULES:
            if rule.startswith("Bash("):
                self.assertIn(rule[5:-3], shell["commandPrefix"])
        github = next(r for r in rules if r.get("mcpName") == "github")
        self.assertEqual(github["toolName"], ["create_pull_request", "merge_pull_request"])

    def test_hooks_json_runs_this_python_for_four_events(self):
        template = target_gemini.outputs(build)[target_gemini.DIST_HOOKS]
        win = json.loads(target_gemini.hooks_json(template, "C:/Py thon/python.exe", windows=True))
        posix = json.loads(target_gemini.hooks_json(template, "/usr/bin/python3", windows=False))
        self.assertEqual(set(win["hooks"]), {"BeforeTool", "AfterTool", "BeforeAgent", "AfterAgent"})
        command = win["hooks"]["BeforeTool"][0]["hooks"][0]["command"]
        self.assertEqual(command, "& 'C:/Py thon/python.exe' "
                                  '"${extensionPath}${/}hooks${/}playbook_hook.py" --client gemini')
        self.assertTrue(posix["hooks"]["AfterAgent"][0]["hooks"][0]["command"].startswith("/usr/bin/python3 "))
        self.assertEqual(win["hooks"]["BeforeTool"][0]["hooks"][0]["timeout"], 60000)  # milliseconds
        matcher = re.compile(win["hooks"]["AfterTool"][0]["matcher"])
        self.assertTrue(all(matcher.search(t) for t in ("write_file", "replace", "run_shell_command", "mcp_x_y")))
        self.assertFalse(any(matcher.search(t) for t in ("read_file", "glob", "write_todos")))
        self.assertTrue(target_gemini.is_our_hooks(target_gemini.hooks_json(template, "python")))


class TargetInstallTest(unittest.TestCase):
    USER_SETTINGS = {"model": {"name": "gemini-2.5-pro"}, "security": {"auth": {"selectedType": "gemini-api-key"}},
                     "context": {"fileName": "GEMINI.md"}, "hooks": {"BeforeTool": [{"matcher": "x", "hooks": [
                         {"type": "command", "command": "my-check"}]}]}}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.gemini = self.home / ".gemini"
        self.settings = self.gemini / "settings.json"
        self.ext = self.gemini / "extensions" / "agent-playbook"

    def plan(self, profiles=()):
        return target_gemini.plan_global(self.home, build.outputs(), Path(sys.executable).as_posix(), list(profiles))

    def write_settings(self, data):
        self.gemini.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(data, indent=4), encoding="utf-8")

    def test_install_keeps_other_settings_and_a_second_plan_is_a_no_op(self):
        self.write_settings(self.USER_SETTINGS)
        install.apply(self.plan(["lang-zh-CN"]))
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["context"]["fileName"], ["GEMINI.md", "AGENTS.md"])
        self.assertEqual({k: v for k, v in data.items() if k != "context"},
                         {k: v for k, v in self.USER_SETTINGS.items() if k != "context"})
        self.assertIn("# profile: lang-zh-CN", (self.ext / "GEMINI.md").read_text(encoding="utf-8"))
        self.assertTrue((self.ext / "hooks" / "playbook_hook.py").exists())
        self.assertTrue((self.ext / "hooks" / "client_gemini.py").exists())
        self.assertTrue((self.ext / "skills" / "pb-status" / "SKILL.md").exists())
        self.assertTrue((self.gemini / "policies" / "agent-playbook.toml").exists())
        self.assertEqual(target_gemini.installed_profiles(self.home), ["lang-zh-CN"])
        self.assertEqual(install.diff(self.plan(["lang-zh-CN"])), [])

    def test_settings_that_need_nothing_are_not_rewritten(self):
        data = {"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}, "ui": {"theme": "x"}}
        self.write_settings(data)
        self.assertNotIn(self.settings, self.plan())
        install.apply(self.plan())
        install.apply(target_gemini.plan_uninstall(self.home)[0])
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), data)  # the user's own names stay

    def test_settings_with_comments_are_left_alone_with_a_note(self):
        self.gemini.mkdir(parents=True)
        self.settings.write_text('{\n  // my comment\n  "ui": {}\n}\n', encoding="utf-8")
        plan = self.plan()
        self.assertNotIn(self.settings, plan)
        self.assertTrue(any("not plain JSON" in n for n in target_gemini.notes()))

    def test_uninstall_removes_only_ours(self):
        self.write_settings(self.USER_SETTINGS)
        other_ext = self.gemini / "extensions" / "other" / "gemini-extension.json"
        other_policy = self.gemini / "policies" / "mine.toml"
        for path in (other_ext, other_policy):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
        install.apply(self.plan(["advisor"]))
        plan, notes = target_gemini.plan_uninstall(self.home)
        self.assertNotIn(other_ext, plan)
        self.assertNotIn(other_policy, plan)
        install.apply(plan)
        self.assertFalse(self.ext.exists())
        self.assertFalse((self.gemini / "policies" / "agent-playbook.toml").exists())
        self.assertTrue(other_ext.exists() and other_policy.exists())
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), self.USER_SETTINGS)
        self.assertEqual(target_gemini.plan_uninstall(self.home)[0], {})

    def test_uninstall_keeps_a_file_the_user_changed(self):
        install.apply(self.plan())
        skill = self.ext / "skills" / "pb-status" / "SKILL.md"
        skill.write_text("my own version\n", encoding="utf-8")
        plan, notes = target_gemini.plan_uninstall(self.home)
        self.assertNotIn(skill, plan)
        self.assertTrue(any(str(skill) in n for n in notes))

    def test_settings_created_by_the_install_go_with_it(self):
        install.apply(self.plan())
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),
                         {"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}})
        install.apply(target_gemini.plan_uninstall(self.home)[0])
        self.assertFalse(self.settings.exists())

    def test_cli_previews_first_then_installs_and_uninstalls(self):
        home = str(self.home)
        code, text = run_install("global", "--client", "gemini", "--home", home)
        self.assertEqual(code, 0, text)
        self.assertIn("Nothing written", text)
        self.assertFalse(self.gemini.exists())
        self.assertEqual(run_install("global", "--client", "gemini", "--home", home, "--yes")[0], 0)
        self.assertIn("already up to date", run_install("global", "--client", "gemini", "--home", home)[1])
        self.assertEqual(run_install("uninstall", "global", "--client", "gemini", "--home", home, "--yes")[0], 0)
        self.assertFalse(self.ext.exists())


if __name__ == "__main__":
    unittest.main()
