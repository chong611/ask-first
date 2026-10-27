import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "src" / "hooks"
sys.path.insert(0, str(HOOKS))

import policy  # noqa: E402
import report  # noqa: E402


def bash(command, **ctx):
    return policy.classify("Bash", {"command": command}, policy.Context(**ctx))


class Tier2ShellTest(unittest.TestCase):
    def test_asks(self):
        cases = [
            "git push --force origin main",
            "git push -f",
            "git -C . push --force-with-lease",
            "git push origin +main",
            "git reset --hard HEAD~1",
            "git clean -fdx",
            "git checkout -- src/app.py",
            "git commit --amend -m x",
            "git rebase -i HEAD~3",
            "gh pr create --fill",
            "gh pr merge 12",
            "sudo pip uninstall requests",
            "npm publish",
            "crontab -e",
            "curl -X POST https://hooks.slack.com/x -d '{}'",
            "bash -c 'git push --force'",
            "ls && git reset --hard",
            "FOO=1 env git push --delete origin old",
        ]
        for c in cases:
            with self.subTest(c):
                self.assertEqual(bash(c)[0], policy.ASK, c)

    def test_allows(self):
        cases = [
            "git status", "git push -u origin claude/x", "git commit -m 'fix'",
            "git checkout -b feature", "gh pr view 3", "pip install requests",
            "crontab -l", "curl https://example.com", "python3 -m pytest -q",
            "rm build/tmp.txt",  # not existing -> nothing to lose
            "echo hello",
        ]
        for c in cases:
            with self.subTest(c):
                self.assertEqual(bash(c)[0], policy.ALLOW, c)

    def test_branch_allowlist(self):
        ctx = {"allowed_branches": ("claude/x",)}
        self.assertEqual(bash("git push -u origin claude/x", **ctx)[0], policy.ALLOW)
        self.assertEqual(bash("git push origin main", **ctx)[0], policy.ASK)
        self.assertEqual(bash("git push", **ctx)[0], policy.ASK)


class UserDataTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = self.dir.name
        Path(self.cwd, "tracked.txt").write_text("a", encoding="utf-8")
        Path(self.cwd, "data").mkdir()
        Path(self.cwd, "data", "orders.db").write_text("x", encoding="utf-8")
        Path(self.cwd, "notes.txt").write_text("untracked", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def ctx(self, **kw):
        return policy.Context(cwd=self.cwd, tracked={"tracked.txt"}, **kw)

    def test_rm_untracked_asks_tracked_allows(self):
        ask = policy.classify("Bash", {"command": "rm notes.txt"}, self.ctx())
        allow = policy.classify("Bash", {"command": "rm tracked.txt"}, self.ctx())
        self.assertEqual((ask[0], allow[0]), (policy.ASK, policy.ALLOW))

    def test_rm_untracked_dir_asks(self):
        self.assertEqual(policy.classify("Bash", {"command": "rm -rf data"}, self.ctx())[0], policy.ASK)

    def test_write_over_untracked_or_declared_data(self):
        self.assertEqual(policy.classify("Write", {"file_path": "notes.txt", "content": "x"}, self.ctx())[0],
                         policy.ASK)
        self.assertEqual(policy.classify("Edit", {"file_path": "tracked.txt", "new_string": "y"}, self.ctx())[0],
                         policy.ALLOW)
        self.assertEqual(policy.classify("Write", {"file_path": "new.py", "content": "x"}, self.ctx())[0],
                         policy.ALLOW)

    def test_own_instructions_ask(self):
        for p in ("CLAUDE.md", "AGENTS.md", ".claude/settings.json", ".claude/hooks/x.py"):
            with self.subTest(p):
                self.assertEqual(policy.classify("Edit", {"file_path": p}, self.ctx())[0], policy.ASK)


class SecretTest(unittest.TestCase):
    def test_print_secret_files_and_env(self):
        for c in ("cat .env", "head -5 config/.env.local", "printenv", "echo $OPENAI_API_KEY"):
            with self.subTest(c):
                self.assertEqual(bash(c)[0], policy.ASK)
        for c in ("cat README.md", "env FOO=1 python x.py", "echo $HOME"):
            with self.subTest(c):
                self.assertEqual(bash(c)[0], policy.ALLOW)

    def test_writing_a_secret_value_asks(self):
        key = "sk-ant-" + "b" * 30
        d = policy.classify("Write", {"file_path": "x.py", "content": f"KEY='{key}'"}, policy.Context())
        self.assertEqual(d[0], policy.ASK)
        self.assertNotIn(key, d[1])

    def test_read_secret_file_asks(self):
        self.assertEqual(policy.classify("Read", {"file_path": "/repo/.env"}, policy.Context())[0], policy.ASK)


class McpTest(unittest.TestCase):
    def test_outward_mcp_and_configured_tools(self):
        self.assertEqual(policy.classify("mcp__github__create_pull_request", {}, policy.Context())[0], policy.ASK)
        self.assertEqual(policy.classify("mcp__github__get_me", {}, policy.Context())[0], policy.ALLOW)
        for local in ("mcp__ccd_turn__update_plan", "mcp__ccd_session__spawn_task", "mcp__x__update_todos",
                      "mcp__ccd_session_mgmt__send_message"):
            self.assertEqual(policy.classify(local, {}, policy.Context())[0], policy.ALLOW, local)
        for other in ("mcp__ccd_session_mgmt__delete_session", "mcp__ccd_session_mgmt__archive_session",
                      "mcp__slack__send_message"):  # Q94A frees one tool, not the server or the verb
            self.assertEqual(policy.classify(other, {}, policy.Context())[0], policy.ASK, other)
        ctx = policy.Context(ask_tools=("mcp__Google_Drive__*",))
        self.assertEqual(policy.classify("mcp__Google_Drive__search_files", {}, ctx)[0], policy.ASK)


GOOD_ZH = """结论在前。

**完成（2）**
- 改了 a.py：`pytest -q` 输出 12 passed
- 更新文档：打开 README 核对了第 3 节

**未完成（0）**
**未验证（1）**
- 真实 API 没跑
**发现未修（0）**
**等你决定（0）**
"""


class ReportTest(unittest.TestCase):
    def test_good_chinese_report(self):
        self.assertIsNone(report.check(GOOD_ZH))

    def test_good_english_report(self):
        msg = "Done (1)\n- ran tests: 3 passed\nNot done (0)\nUnverified: 0\nFound but not fixed (0)\nWaiting on you (0)\n"
        self.assertIsNone(report.check(msg))

    def test_missing_and_uncounted(self):
        reason = report.check("**完成**\n- x\n未完成（0）\n")
        self.assertIn("missing headings", reason)
        self.assertIn("without a count", reason)

    def test_done_count_must_match_items(self):
        self.assertIn("Done count 3", report.check(GOOD_ZH.replace("完成（2）", "完成（3）")))

    def test_not_done_is_not_confused_with_done(self):
        rep = report.parse(GOOD_ZH)
        self.assertEqual(rep.counts["Done"], 2)
        self.assertEqual(rep.counts["Not done"], 0)


class AskCheckTest(unittest.TestCase):
    WAIT = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- 要不要再补一个测试")
    WAIT_Q1 = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- Q1 要不要再补一个测试（已用提问框问）")

    def test_interactive_needs_ask_tool(self):
        self.assertIn("AskUserQuestion", report.ask_check(self.WAIT, asked=False, interactive=True))
        self.assertIsNone(report.ask_check(self.WAIT_Q1, asked=True, interactive=True, asked_ids={"Q1"}))
        self.assertIsNone(report.ask_check(GOOD_ZH, asked=False, interactive=True))  # 等你决定（0）
        self.assertIsNone(report.ask_check("答案是 4。", asked=False, interactive=True))  # no report

    def test_each_item_needs_an_id_asked_this_turn(self):  # Q90A
        two = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（2）**\n- Q1A 已选：请发一句话\n- 要不要上线")
        reason = report.ask_check(two, asked=True, interactive=True, asked_ids={"Q1"})
        self.assertIn("1 Waiting on you item(s)", reason)
        self.assertIn("要不要上线", reason)
        self.assertIn("Rule 2.2", report.ask_check(self.WAIT_Q1, asked=True, interactive=True, asked_ids={"Q2"}))
        self.assertIn("Rule 2.2", report.ask_check(self.WAIT, asked=True, interactive=True))  # asked, but no IDs

    def test_question_ids(self):
        self.assertEqual(report.question_ids("选Q1A要；Q12B/Q3、（Q40）"), {"Q1", "Q12", "Q3", "Q40"})
        self.assertEqual(report.question_ids("SQ1 Q1AB 2Q3 FAQ"), set())

    def test_prose_asks(self):  # Q89A
        for line in ("要不要我把它推送上去？", "你决定用哪个方案。", "选 A 还是 B？", "要现在修还是以后修？", "这样可以吗？",
                     "Should I push it?", "Do you want me to merge?", "It's up to you.", "请选择一个方案", "你决定吧。",
                     "需要我帮你推送吗？", "Do you prefer A or B?", "How would you like to proceed?", "那就由你来定。"):
            with self.subTest(line):
                self.assertTrue(report.prose_asks(line))
        for line in ("**等你决定（1）**", "- Q1 要不要上线？", "你说的「你决定」我记下了", "`should I`", "> 要不要我改？",
                     "我已经推送了。", "还是 10-24 的旧版", "为什么测试还是失败？因为路径写错了，已修好。",
                     "这是 bug 还是预期行为？是预期行为。", "已按你决定的方案修改。", "如果你决定不改，也可以。",
                     "打开 PowerShell，请选择以管理员身份运行。", "这样不好吗？", "这个函数用来判断要不要重试？",
                     "Because you didn't want me to touch the lockfile, I left it.", "Your call to fetch() returns None.",
                     "**等你决定：** 要不要上线 见下"):
            with self.subTest(line):
                self.assertEqual(report.prose_asks(line), [])

    def test_prose_ask_needs_a_question(self):  # Q89A
        msg = "改好了。要不要我顺便推送？"
        self.assertIn("leaves a decision", report.ask_check(msg, asked=False, interactive=True))
        self.assertIsNone(report.ask_check(msg, asked=True, interactive=True))
        self.assertIn("numbered question", report.ask_check(msg, asked=False, interactive=False))
        self.assertIsNone(report.ask_check("改好了。Q1 要不要推送？Q1A 推（推荐）/ Q1B 不推", asked=False, interactive=False))

    def test_headless_accepts_numbered_questions(self):
        self.assertIn("question IDs", report.ask_check(self.WAIT, asked=False, interactive=False))
        numbered = self.WAIT.replace("- 要不要再补一个测试", "- Q1 要不要开调查？Q1A 开（推荐）/ Q1B 不开")
        self.assertIsNone(report.ask_check(numbered, asked=False, interactive=False))
        self.assertIn("Rule 2.2", report.ask_check(numbered, asked=False, interactive=True))

    def test_interactive_client(self):
        for entry, want in (("claude-desktop", True), ("cli", True), ("sdk-cli", False), ("sdk-py", False), ("", False)):
            with self.subTest(entry):
                self.assertEqual(report.interactive_client({"CLAUDE_CODE_ENTRYPOINT": entry}), want)
        self.assertFalse(report.interactive_client({}))


class DeferralTest(unittest.TestCase):
    def test_hits(self):
        self.assertTrue(report.deferral_hits("这些改动下次再做。"))
        self.assertTrue(report.deferral_hits("I'll defer the cleanup."))

    def test_ignores_quotes_code_and_questions(self):
        self.assertEqual(report.deferral_hits("你说过「下次再做」不行。"), [])
        self.assertEqual(report.deferral_hits("`later` is a variable"), [])
        self.assertEqual(report.deferral_hits("Q3. 要不要下次再做？"), [])
        self.assertEqual(report.deferral_hits("从今以后都这样做"), [])


class DispatcherTest(unittest.TestCase):
    """Runs playbook_hook.py as Claude Code would: JSON on stdin."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.repo = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q", self.repo.name], check=True)
        self.env = {**os.environ, "AGENT_PLAYBOOK_HOME": self.home.name}
        self.env.pop("CLAUDE_CODE_ENTRYPOINT", None)  # each test states the client it simulates

    def tearDown(self):
        self.home.cleanup()
        self.repo.cleanup()

    def run_hook(self, event):
        event = {"session_id": "s1", "cwd": self.repo.name, **event}
        out = subprocess.run([sys.executable, str(HOOKS / "playbook_hook.py")], input=json.dumps(event),
                             capture_output=True, encoding="utf-8", env=self.env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if out.stdout.strip() else None

    def test_pre_tool_use_asks(self):
        out = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "git push --force"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_stop_requires_report_only_after_changes(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "答案是 4。"}))
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "edit"})
        Path(self.repo.name, "new.txt").write_text("x", encoding="utf-8")  # state changed this turn
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "改好了。"})
        self.assertEqual(out["decision"], "block")
        # Second stop in the same turn is not blocked again (each rule blocks once per turn).
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "改好了。"}))

    def test_good_report_passes_and_open_items_survive_compaction(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "edit"})
        self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}})
        msg = GOOD_ZH.replace("**未完成（0）**", "**未完成（1）**\n- 部署还没做")
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))
        out = self.run_hook({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertIn("部署还没做", out["hookSpecificOutput"]["additionalContext"])

    def test_pipe_advice_is_context_not_a_block(self):
        out = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "python3 -m pytest -q | tail -1"}})
        self.assertIn("pipefail", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_powershell_reminder_once_per_turn(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "steps?"})
        msg = "```powershell\ncd x && git push\n```"
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})
        self.assertIn("PowerShell", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))

    def test_bypass_mode_denies_unapproved_tier2(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "clean up the repo"})
        out = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "PowerShell",
                             "permission_mode": "bypassPermissions",
                             "tool_input": {"command": "git reset --hard"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        out = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "PowerShell",
                             "permission_mode": "default", "tool_input": {"command": "git reset --hard"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_waiting_items_need_a_question_this_turn(self):
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        msg = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- Q1 要不要再补一个测试")
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q"})
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})  # no change, still asked
        self.assertEqual(out["decision"], "block")
        self.assertIn("AskUserQuestion", out["reason"])
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q2"})
        self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": [{"question": "Q1 要不要再补一个测试？", "options": []}]}})
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q3"})  # the flag and IDs reset each prompt
        self.assertEqual(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})["decision"],
                         "block")

    def test_report_and_question_reasons_share_one_block(self):
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "edit"})
        self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}})
        msg = "改好了。\n**等你决定（1）**\n- 要不要上线"
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})
        self.assertIn("Rule 1.3", out["reason"])
        self.assertIn("Rule 2.2", out["reason"])
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))

    def test_another_hooks_continuation_is_still_checked(self):
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        msg = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- 要不要上线")
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q"})
        out = self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": msg})
        self.assertIn("Rule 2.2", out["reason"])
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True,
                                         "last_assistant_message": msg}))  # still one block per rule

    def test_an_item_without_an_asked_id_is_blocked(self):  # Q90A
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        msg = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（2）**\n- Q1A 已选：请发一句话\n- 要不要上线")
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q"})
        self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": [{"question": "Q1 实测吗？", "options": [{"label": "Q1A 要"}]}]}})
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})
        self.assertIn("要不要上线", out["reason"])

    def test_each_rule_blocks_once_per_turn(self):  # Q91A
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "edit"})
        self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}})
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "改好了。"})
        self.assertIn("Rule 1.3", out["reason"])
        self.assertNotIn("Rule 2.2", out["reason"])
        fixed = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- 要不要上线")  # report fixed, decision not asked
        out = self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": fixed})
        self.assertIn("Rule 2.2", out["reason"])
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True,
                                         "last_assistant_message": fixed}))  # each rule once: no loop

    def test_prose_ask_is_blocked_and_logged(self):  # Q89A
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q"})
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "查完了。要不要我顺便修掉？"})
        self.assertIn("Rule 2.2", out["reason"])
        log = Path(self.home.name, "logs", "ask-prose.jsonl").read_text(encoding="utf-8")
        self.assertIn("要不要我顺便修掉", log)
        self.assertIn('"blocked": true', log)
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True,
                                         "last_assistant_message": "查完了。要不要我顺便修掉？"}))
        self.assertEqual(len(Path(self.home.name, "logs", "ask-prose.jsonl").read_text(encoding="utf-8")
                             .splitlines()), 1)  # a stop after the 2.2 block is not checked or logged again

    def test_asked_ids_reset_each_prompt(self):  # Q90A
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        msg = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- Q1 要不要上线")
        ask = lambda q: self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                                       "tool_input": {"questions": [{"question": q, "options": []}]}})
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "t1"})
        ask("Q1 要上线吗？")
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "t2"})
        ask("Q2 要不要发布说明？")  # Q1 was asked last turn, not this one
        self.assertIn("carry no ID", self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})["reason"])

    def test_question_id_after_a_newline_is_recorded(self):  # Q90A
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q"})
        self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": [{"question": "背景说明。\nQ1 要上线吗？", "options": []}]}})
        msg = GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- Q1 要上线吗")
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))

    def test_state_from_before_q91a_keeps_its_block(self):
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        msg = "改好了。\n**等你决定（1）**\n- 要不要上线"
        state = Path(self.home.name, "state")
        state.mkdir(parents=True)
        Path(state, "s1.json").write_text(json.dumps({"blocked_this_turn": True}), encoding="utf-8")
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg}))
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "next"})
        self.assertIn("Rule 2.2", self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})["reason"])

    def test_powershell_reminder_does_not_skip_the_checks(self):
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        msg = "```powershell\ncd x && git push\n```\n" + GOOD_ZH.replace("**等你决定（0）**", "**等你决定（1）**\n- 要不要上线")
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "steps?"})
        out = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": msg})
        self.assertIn("Rule 2.2", out["reason"])
        self.assertIn("PowerShell", out["reason"])
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True,
                                         "last_assistant_message": msg}))

    def test_deferral_is_logged_not_blocked(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "q"})
        self.assertIsNone(self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "这个下次再做。"}))
        log = Path(self.home.name, "logs", "deferral.jsonl").read_text(encoding="utf-8")
        self.assertIn("下次再做", log)


if __name__ == "__main__":
    unittest.main()


class StepGatingAdviceTest(unittest.TestCase):
    def test_pipe_hides_exit(self):
        for c in ("python3 -m pytest -q | tail -1", "python tools/check.py | head -3",
                  "cd x\nnpm test | grep FAIL", "sh tests/live/run.sh | tail -3", "npm run test | tail",
                  "uv run pytest | tail", "PYTHONPATH=. pytest | head", "py -3 -m pytest | tail",
                  "timeout 60 pytest | tail", "npx tsc | head"):
            with self.subTest(c):
                self.assertIsNotNone(report.pipe_hides_exit(c))
        for c in ("set -o pipefail; pytest | tail -1", "pytest -q", "git log | head", "pytest && echo ok",
                  "cat tests/live/run.sh | head -60", "grep -n pytest README.md | head", "ls; pytest -q",
                  "pytest | tail; echo ${PIPESTATUS[0]}"):
            with self.subTest(c):
                self.assertIsNone(report.pipe_hides_exit(c))

    def test_powershell_and_and(self):
        bad = "Run:\n```powershell\ncd x && git push\n```\n"
        good = "Run:\n```powershell\ncd x\nif ($?) { git push }\n```\n```bash\ncd x && ls\n```\n"
        self.assertIsNotNone(report.powershell_and_and(bad))
        self.assertIsNone(report.powershell_and_and(good))


class HousekeepingTest(unittest.TestCase):
    """The hook's own files must not pile up on the user's computer."""

    def test_old_state_pruned_and_logs_rotate(self):
        import importlib
        with tempfile.TemporaryDirectory() as home:
            os.environ["AGENT_PLAYBOOK_HOME"] = home
            try:
                import playbook_hook
                importlib.reload(playbook_hook)
                state = Path(home, "state")
                state.mkdir()
                old, new = state / "old.json", state / "new.json"
                old.write_text("{}", encoding="utf-8")
                new.write_text("{}", encoding="utf-8")
                os.utime(old, (0, 0))
                playbook_hook.prune()
                self.assertFalse(old.exists())
                self.assertTrue(new.exists())
                logs = Path(home, "logs")
                logs.mkdir()
                Path(logs, "tier2.jsonl").write_text("x" * (playbook_hook.LOG_MAX_BYTES + 1), encoding="utf-8")
                playbook_hook.log("tier2", {"a": 1})
                self.assertTrue(Path(logs, "tier2.1.jsonl").exists())
                self.assertLess(Path(logs, "tier2.jsonl").stat().st_size, 100)
                playbook_hook.log("ask-prose", {"hits": ["\ud800 要不要我改？"]})  # must not raise
                self.assertIn("\\ud800", Path(logs, "ask-prose.jsonl").read_text(encoding="utf-8"))
                playbook_hook.save_state("s", {"open": {"Not done": ["\ud800"]}})
                self.assertEqual(playbook_hook.load_state("s")["open"]["Not done"], ["\ud800"])
            finally:
                del os.environ["AGENT_PLAYBOOK_HOME"]


class DeferralFalsePositiveTest(unittest.TestCase):
    def test_new_patterns_do_not_fire_on_neutral_text(self):
        for text in ("到时候你会看到结果。", "不急着回答，先查清楚。", "排队系统已经修好"):
            with self.subTest(text):
                self.assertEqual(report.deferral_hits(text), [])


def link_dir(target, link) -> None:
    """A directory link. On Windows a junction, which needs no admin rights or Developer Mode."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)


class PowerShellWriteTest(unittest.TestCase):
    """Q102A: PowerShell cmdlets that write agent instructions or settings ask, as `>` already did."""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name, "home")
        Path(self.home, ".claude", "rules").mkdir(parents=True)
        self.cwd = Path(self.tmp.name, "repo")
        self.cwd.mkdir()
        self.temp = Path(self.tmp.name, "temp")
        self.temp.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                           "TEMP": str(self.temp)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def cls(self, command, tool="PowerShell"):
        return policy.classify(tool, {"command": command}, policy.Context(cwd=str(self.cwd)))[0]

    def test_write_cmdlets_ask(self):
        for c in ("Add-Content -Path ~/.claude/CLAUDE.md -Value x",
                  "Add-Content -Path ~/.claude/rules/x.md -Value x",
                  "Set-Content -Path ~/.claude/settings.json -Value x",
                  "Out-File -FilePath ~/.claude/CLAUDE.md -InputObject x",
                  "sc $HOME\\.claude\\settings.json x",
                  "ac -LiteralPath $env:USERPROFILE\\.claude\\CLAUDE.md -Value x",
                  "'x' | Out-File ${env:USERPROFILE}/.claude/rules/x.md -Append",
                  "Get-Content a.txt | Tee-Object -FilePath ~/.claude/CLAUDE.md",
                  "'{}' | tee $HOME/.claude/settings.local.json",
                  "New-Item -Path ~/.claude/rules/x.md -Value x",
                  "New-Item -Path ~/.claude -Name settings.json -ItemType File -Value '{}'",
                  "ni ~/.claude/CLAUDE.md -Force",
                  "New-Item -ItemType SymbolicLink -Path ~/.claude/rules -Target D:/x",
                  "Copy-Item x.md -Destination ~/.claude/rules",
                  "cp x.md ~/.claude/rules/",
                  "copy settings.json $HOME/.claude",
                  "Copy-Item -Path:x.md -Destination:$env:USERPROFILE/.claude/CLAUDE.md",
                  "Move-Item new.json ~/.claude/settings.json",
                  "move a.md ~/.claude/rules",
                  "Rename-Item ~/.claude/x.json settings.json",
                  "Clear-Content ~/.claude/CLAUDE.md",
                  "SET-CONTENT -PATH ~/.CLAUDE/SETTINGS.JSON -VALUE x",
                  "Set-Content -Path (Join-Path $HOME '.claude\\settings.json') -Value x",
                  "[IO.File]::WriteAllText(\"$env:USERPROFILE\\.claude\\settings.json\", $s)",
                  "$fs = [System.IO.File]::Create(\"$HOME/.claude/rules/x.md\")",
                  "Set-Content CLAUDE.md x", "Add-Content .claude/settings.local.json x", "'x' | Out-File AGENTS.md",
                  "echo x > ${env:USERPROFILE}/.claude/CLAUDE.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ("powershell -Command \"Set-Content -Path ~/.claude/CLAUDE.md -Value x\"",
                  "cp x.md ~/.claude/rules", "echo x > ${HOME}/.claude/CLAUDE.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_ordinary_writes_allow(self):
        for c in ("Set-Content -Path notes.txt -Value 'see CLAUDE.md'",
                  "Set-Content -Path out.txt -Value CLAUDE.md",
                  "Out-File -FilePath log.txt -InputObject ~/.claude/settings.json",
                  "Copy-Item a.txt b.txt", "Get-Content ~/.claude/settings.json",
                  "New-Item -ItemType Directory -Force -Path ~/.claude/skills/x",
                  "Add-Content -Encoding utf8 -Path ~/.claude/agent-playbook/lessons-inbox.md -Value x",
                  "Add-Content -Encoding utf8 -Path $env:USERPROFILE\\.claude\\agent-playbook\\lessons-inbox.md -Value x",
                  "Add-Content -Path $HOME/.claude/agent-playbook/lessons-inbox.md -Value x"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ALLOW)
        # a script that only mentions a .NET write is data, not a call
        script = "cat > t.py <<'EOF'\ncase = '[IO.File]::WriteAllText(\"$env:USERPROFILE/.claude/settings.json\", $s)'\nEOF"
        self.assertEqual(self.cls(script, tool="Bash"), policy.ALLOW)

    def test_in_a_skill_folder_only_real_paths_count(self):  # seen in real sessions, 2026-10
        self.cwd = Path(self.home, ".claude", "skills", "s", "scripts")
        self.cwd.mkdir(parents=True)
        for c in ("'x' | Out-File \"$env:USERPROFILE/notes/count.txt\" -Append -Encoding utf8",
                  "Copy-Item \"$b/requirements.txt\" \"$b/.venv/requirements.installed\"",
                  "Copy-Item \"$a/requirements.lock\",\"$a/requirements.txt\" $b",
                  "New-Item -ItemType Directory $b"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ALLOW)
        self.assertEqual(self.cls("Set-Content -Path notes.txt -Value x"), policy.ASK)  # a file of the skill

    def test_review_bypasses_ask(self):  # from the independent review of Q102A
        Path(self.home, ".claude", "rules", "x.md").write_text("x", encoding="utf-8")
        Path(self.home, ".claude", "settings.json").write_text("{}", encoding="utf-8")
        for c in ("Set-Location $env:USERPROFILE/.claude; Set-Content settings.json x",
                  "Set-Content `\n -Path ~/.claude/settings.json -Value x",
                  "Set-Content -Path (\"$HOME/.claude/settings.json\") -Value x",
                  "Set-Content -Path @('~/.claude/CLAUDE.md', 'b.txt') -Value x",
                  "Set-Content -Path (Join-Path -ChildPath settings.json -Path $HOME/.claude) -Value x",
                  "Set-Content -Path ([IO.Path]::Combine($HOME, '.claude', 'settings.json')) -Value x",
                  "Set-Content -Path ~/.claude/rule?/x.md -Value x", "Set-Content ~/.claude/settings.jso? x",
                  "New-Item -ItemType Junction -Path cfg -Target $HOME/.claude; Set-Content cfg/settings.json x",
                  "$f = [IO.File]::Open(\"$HOME/.claude/settings.json\", 'Create')",
                  "$w = New-Object IO.StreamWriter(\"$HOME/.claude/CLAUDE.md\")",
                  "Expand-Archive skills.zip -DestinationPath ~/.claude/skills",
                  "Invoke-WebRequest https://example.com/r.md -OutFile ~/.claude/rules/r.md",
                  "robocopy src $HOME/.claude/skills /E", "Copy-Item -Recurse .claude ~",
                  "Invoke-Expression 'Set-Content ~/.claude/CLAUDE.md x'",
                  "Set-Content -n ~/.claude/CLAUDE.md x", "Copy-Item -cont x.md ~/.claude/rules"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ("powershell \"Set-Content ~/.claude/CLAUDE.md x\"",
                  "powershell -NoProfile -ExecutionPolicy Bypass -Com \"Set-Content ~/.claude/CLAUDE.md x\"",
                  "cp -t ~/.claude settings.json", "ln -s ~/.claude cfg"):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_home_spellings_in_cd(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"HOME": str(self.cwd)}):  # $HOME and %USERPROFILE% differ
            self.assertEqual(self.cls("cd ~/.claude; Set-Content settings.json x"), policy.ASK)
        with mock.patch.dict(os.environ):
            del os.environ["HOME"]  # the hook may run without HOME: PowerShell's $HOME is %USERPROFILE%
            self.assertEqual(self.cls("cd $HOME/.claude; Set-Content settings.json x"), policy.ASK)

    def test_review_over_asks_allow(self):  # from the independent review of Q102A
        self.cwd = Path(self.home, ".claude", "skills", "s", "scripts")
        self.cwd.mkdir(parents=True)
        for c in ("Add-Content -Encoding utf8 ~/.claude/agent-playbook/lessons-inbox.md "
                  "\"2026-10-26 | proj | x | add to ~/.claude/rules/agent-playbook.md\"",
                  "Set-Content -Path $env:TEMP/x.txt -Value x -ErrorAction SilentlyContinue",
                  "Out-File -FilePath $env:TEMP/x.txt -Width 200", "Set-Content $env:TEMP/x.txt hello",
                  "Copy-Item $env:TEMP/a $env:TEMP/b -Filter *.md", "'x' | Tee-Object -Variable out",
                  "Copy-Item ~/.claude/settings.json $env:TEMP/settings.json",
                  "Copy-Item -Recurse ~/.claude/skills/foo $env:TEMP/b",
                  "[IO.File]::Copy(\"$HOME/.claude/settings.json\", \"$env:TEMP/s.json\")",
                  "[IO.File]::WriteAllText(\"$env:TEMP/out.json\", [IO.File]::ReadAllText(\"$HOME/.claude/settings.json\"))",
                  "Select-String -Pattern '[IO.File]::WriteAllText(' -Path ~/.claude/rules/*.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ALLOW)
        self.assertEqual(self.cls("grep -rn '[IO.File]::WriteAllText' ~/.claude/skills", tool="Bash"), policy.ALLOW)

    def test_second_review_bypasses_ask(self):  # from the second independent review of Q102A
        Path(self.home, ".claude", "rules", "x.md").write_text("x", encoding="utf-8")
        Path(self.home, ".claude", "settings.json").write_text("{}", encoding="utf-8")  # a wildcard needs a match
        for c in ("Set-Content ~/*/settings.json x", "Set-Content ~/[.]claude/settings.json x",
                  "Set-Content ~/?claude/settings.json x", "Set-Location ~/.cl*; Set-Content settings.json x",
                  "Set-Location (Resolve-Path ~/.claude); Set-Content settings.json x",
                  "cd a; cd b; cd c; cd d; Set-Location ~/.claude; Set-Content settings.json x",
                  "New-Item -ItemType Junc -Path cfg -Target $HOME/.claude; Set-Content cfg/settings.json x",
                  "robocopy myclaude $HOME/.claude /E",
                  "Get-Item ~/.claude/rules/x.md | Set-Content -Value x",
                  "Get-ChildItem ~/.claude/rules | Add-Content -Value x",
                  "Get-Item ~/.claude/x.json | Rename-Item -NewName settings.json",
                  "Set-Content -Path (Resolve-Path ~/.claude/settings.json) -Value x",
                  "Copy-Item x.md -Destination (Resolve-Path ~/.claude/rules)",
                  "curl https://example.com/r.md -OutFile ~/.claude/rules/r.md",
                  "'Set-Content ~/.claude/CLAUDE.md x' | iex", "iex -C 'Set-Content ~/.claude/CLAUDE.md x'",
                  "Export-Csv -Path ~/.claude/settings.json -InputObject $x",
                  "cd $p; Set-Content .claude/settings.json x"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ("curl -sSLo ~/.claude/rules/r.md https://example.com/r.md",
                  "wget -O ~/.claude/CLAUDE.md https://example.com/c.md",
                  "powershell -i text -c \"Set-Content ~/.claude/CLAUDE.md x\""):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_commands_in_parentheses_still_run(self):  # (pwd) runs pwd: the target is not known
        docs = Path(self.tmp.name, "docs")
        docs.mkdir()
        Path(docs, "notes.txt").write_text("user data", encoding="utf-8")
        for c in ("Remove-Item -Recurse -Force (pwd)", "rm (ls)", "Remove-Item -Path (gci)"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        self.assertEqual(self.cls(f"Remove-Item -Recurse (Get-ChildItem {docs.as_posix()})"), policy.ASK)
        for c in ("New-Item -ItemType d ~/.claude/skills/x", "robocopy src dst /MIR /XD .claude"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ALLOW)

    def test_path_expressions_never_loosen_the_delete_check(self):  # third review: these asked before
        for d in ("src2", "docs"):
            Path(self.cwd, d).mkdir()
            Path(self.cwd, d, "f.txt").write_text("untracked", encoding="utf-8")
        for c in ('Remove-Item -Recurse -Force ("src2", "build")', 'Remove-Item -Recurse ("src2"),("build")',
                  'Remove-Item -Recurse -Force ("~"+"")', 'Remove-Item -Recurse ("."+"/")',
                  'Remove-Item -Recurse -Force docs/("x")', "Remove-Item -Recurse (Join-Path . docs -Resolve)",
                  "Remove-Item -Recurse ([IO.Path]::Combine('.', 'C:/Users'))",
                  "Set-Location $env:USERPROFILE; rm @('..')", "cd s*; Remove-Item docs,build",
                  "Remove-Item docs,src2", "Remove-Item docs, src2"):  # the hook follows more cds now
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)

    def test_third_review_bypasses_ask(self):  # from the third independent review of Q102A
        for c in ("Set-Content -Path a.txt, ~/.claude/settings.json x", "Add-Content notes.txt, ~/.claude/CLAUDE.md x",
                  "Set-Location -Path:$HOME/.claude; Set-Content settings.json x",
                  "Set-Location -LiteralPath:$HOME/.claude; Set-Content settings.json x",
                  "Set-Content '~/.claude/CLAUDE.md::$DATA' x", "Set-Content (Resolve-Path ~/.claude/rules).Path x",
                  "Set-Content -Path ($HOME + '/.claude/settings.json') -Value x",
                  "Start-BitsTransfer https://example.com/r.md -Destination ~/.claude/rules/r.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)

    def test_third_review_over_asks_allow(self):
        Path(self.cwd, "docs").mkdir()
        for c in ("Copy-Item CLAUDE.md $env:TEMP/claude-backup.md",
                  "Copy-Item ~/.claude/CLAUDE.md docs/claude-global.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ALLOW)
        self.cwd = Path(self.home, ".claude", "skills", "s")
        self.cwd.mkdir(parents=True)
        self.assertEqual(self.cls("Export-Csv -Path $env:TEMP/x.csv -NoTypeInformation"), policy.ALLOW)

    def test_odd_input_neither_crashes_nor_stalls(self):
        import time
        for c in ("$dirs = @($env:USERPROFILE,$env:APPDATA,$env:LOCALAPPDATA,$env:TEMP,$env:ProgramData,"
                  "$env:windir,$env:ProgramFiles | Where-Object { $_ })",
                  "Set-Content -Path C:/*/*/*/*/x.txt -Value x", "Set-Content -Path " + "a/" * 1200 + "x -Value x",
                  "Set-Content -Path a\0*b -Value x", "Set-Content (" + '"a",' * 3000 + ")"):
            with self.subTest(c[:40]):
                start = time.monotonic()
                self.assertIn(self.cls(c), (policy.ASK, policy.ALLOW))
                self.assertLess(time.monotonic() - start, 3)

    def test_other_checks_keep_their_cd_reading(self):  # fourth review: these asked before the change
        Path(self.cwd, "a.txt").write_text("committed", encoding="utf-8")
        Path(self.home, "notes.txt").write_text("user data", encoding="utf-8")
        clean = policy.Context(cwd=str(self.cwd), tracked={"a.txt"}, dirty=set())  # outside it: home, temp
        for c in ("cd ('..'); rm -rf .", "Set-Location (Resolve-Path ..); Remove-Item -Recurse -Force *",
                  "Push-Location (git rev-parse --show-toplevel); Remove-Item -Recurse -Force *"):
            with self.subTest(c):
                self.assertEqual(policy.classify("PowerShell", {"command": c}, clean)[0], policy.ASK)
        Path(self.cwd, "secrets.json").write_text("{}", encoding="utf-8")
        for tool, c in (("Bash", "cd a; cd b; cd c; cd d; cd e; cat secret*"),
                        ("PowerShell", "cd a; cd b; cd c; cd d; cd e; Get-Content secret*")):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool=tool), policy.ASK)
        for i in range(8):
            Path(self.cwd, f".d{i}").mkdir()
        Path(self.cwd, "work").mkdir()
        Path(self.cwd, "work", "data.txt").write_text("user data", encoding="utf-8")
        self.assertEqual(policy.classify("Bash", {"command": "cd *; rm -rf data.txt"}, clean)[0], policy.ASK)

    def test_fourth_review_bypasses_ask(self):
        Path(self.home, ".claude", "rules", "x.md").write_text("x", encoding="utf-8")
        for c in ("Set-Content -Path (Join-Path (Join-Path $HOME .claude) settings.json) -Value x",
                  "Set-Content -Path ([IO.Path]::Combine((Resolve-Path ~).Path, '.claude', 'settings.json')) -Value x",
                  "Get-Item ~/.claude/rules/x.md | Select-Object -First 1 | Set-Content -Value x",
                  "Start-Transcript -Path ~/.claude/rules/t.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ("wget -qO ~/.claude/CLAUDE.md https://example.com/c.md", "rsync -a src/ ~/.claude/skills/",
                  "unzip s.zip -d ~/.claude/skills", "dd if=x of=$HOME/.claude/CLAUDE.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_fourth_review_over_asks_allow(self):
        big = Path(self.cwd, "bigdata")
        big.mkdir()
        for i in range(5100):  # more than the wildcard search looks at
            Path(big, f"{i}.csv").touch()
        for tool, c in (("Bash", "cp bigdata/*.csv build/"), ("Bash", "sed -i 's/a/b/' bigdata/*.csv"),
                        ("PowerShell", "Set-Content -Path bigdata/*.csv -Value x")):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool=tool), policy.ALLOW)
        Path(self.home, ".claude", "agent-playbook").mkdir()
        self.assertEqual(self.cls("cd ~/.claude/agent-playbook; Add-Content lessons-inbox.md x"), policy.ALLOW)

    def test_long_input_stays_linear(self):  # fourth review: these took 7 to 37 s
        import time
        for c in ('Set-Content ("' + " " * 100_000 + '") x', "tee " + "a:" * 50_000 + "/x",
                  "Set-Content " + "a, " * 33_000 + "x"):
            with self.subTest(c[:30]):
                start = time.monotonic()
                self.cls(c)
                self.assertLess(time.monotonic() - start, 3)

    def test_a_backtick_joins_lines_only_for_the_write_check(self):  # fifth review
        Path(self.cwd, "data").mkdir()
        Path(self.cwd, "data", "f.txt").write_text("untracked", encoding="utf-8")
        for c in ("Write-Output a ` \nRemove-Item -Recurse -Force data", "# clean up `\nRemove-Item -Recurse -Force data",
                  "Write-Output a ` \ngit push --force", "# note `\nSet-Content ~/.claude/CLAUDE.md x",
                  "Set-Content `\n -Path ~/.claude/settings.json -Value x"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)

    def test_fifth_review_bypasses_ask(self):
        Path(self.cwd, "data").mkdir()
        Path(self.cwd, "data", "f.txt").write_text("untracked", encoding="utf-8")
        for c in ("$null = New-Item -Force ~/.claude/rules/x.md", "$r = Copy-Item x.md ~/.claude/CLAUDE.md -PassThru",
                  "$null = Remove-Item -Recurse -Force data", "$x = git push --force",
                  ". Set-Content -Path ~/.claude/settings.json -Value x", "Rename-Item ~/.claude claude-off",
                  "Move-Item ~/.claude ~/x", "subst Q: $HOME/.claude",
                  "New-PSDrive -Name Q -PSProvider FileSystem -Root ~/.claude"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ('powershell -File x.ps1 -Command "Remove-Item data -Recurse"',  # read as before: asks
                  'powershell -NoProfile -EncodedCommand ZQBjAGgAbwA= -Command "Remove-Item data -Recurse"',
                  'iex "bash -c "python tools/install.py --yes""'):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ("mv ~/.claude ~/x", 'powershell /c "Set-Content ~/.claude/CLAUDE.md x"',
                  "curl -o$HOME/.claude/rules/x.md https://example.com/x.md",
                  "wget -O$HOME/.claude/CLAUDE.md https://example.com/c.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_fifth_review_over_asks_allow(self):
        Path(self.cwd, "CLAUDE.md").write_text("x", encoding="utf-8")
        Path(self.home, ".claude", "settings.json").write_text("{}", encoding="utf-8")
        for tool, c in (("PowerShell", "cp ~/.claude/rules $env:TEMP/x"), ("Bash", "cp -r ~/.claude/rules /tmp/x"),
                        ("Bash", "cp * /tmp/x"), ("PowerShell", "copy ~/.claude/settings.json b.json")):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool=tool), policy.ALLOW)
        # the check from before Q102A judges every cp argument, and it is kept whole: this still asks
        self.assertEqual(self.cls("cp ~/.claude/settings.json b.json"), policy.ASK)

    def test_long_arrays_and_wildcards_stay_fast(self):  # fifth review: 11 to 40 s
        import time
        big = Path(self.cwd, "big")
        for i in range(10):
            Path(big, f"d{i}").mkdir(parents=True)
        for i in range(4990):
            Path(big, f"f{i}").touch()
        for tool, c in (("PowerShell", "Copy-Item -Path " + ",".join(f"a{i}" for i in range(300)) + " -Destination "
                         + ",".join(f"d{i}/" for i in range(300)) + "; Remove-Item -Recurse -Force data"),
                        ("Bash", "cp " + " ".join(f"--target-directory=t{i}/" for i in range(200)) + " "
                         + " ".join(f"x{i}" for i in range(200)) + "; git push --force"),
                        ("PowerShell", "New-Item -Path " + ",".join(f"a{i}" for i in range(200)) + " -Name "
                         + ",".join(f"n{i}" for i in range(200))),
                        ("PowerShell", "Set-Content -Path " + ", ".join(f"big/*/q{i}" for i in range(200)) + " -Value x"),
                        ("PowerShell", "Set-Content -Path " + ", ".join(f"big/*q{i}" for i in range(1000)) + " -Value x")):
            with self.subTest(c[:40]):
                start = time.monotonic()
                self.cls(c, tool=tool)
                self.assertLess(time.monotonic() - start, 3)

    def test_sixth_review_cases_ask(self):
        Path(self.cwd, "data").mkdir()
        Path(self.cwd, "data", "f.txt").write_text("untracked", encoding="utf-8")
        Path(self.home, ".claude", "skills").mkdir()
        link_dir(Path(self.home, ".claude"), Path(self.cwd, "cl"))
        for c in ("cp -r src ~/.claude/skills/x", "cp -r ./src ~/.claude/skills/", "cp -v a ~/.claude/rules/x.md",
                  "Copy-Item -r src ~/.claude/skills/x", "cpi -u src ~/.claude/skills/x", "cp -r src cl/skills/z",
                  "Copy-Item src -r ~/.claude/skills/x", "Start-Transcript -include ~/.claude/rules/t.md",
                  "Expand-Archive -v a.zip ~/.claude/skills", "Set-Content \u2013Path ~/.claude/rules/y.md \u2013Value x",
                  "cd ~/.claude/rules; 'x' > $f", "cd ~/.claude/rules; tee $f",
                  "cd ~/.claude/skills; cp -r ~/myskill ."):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ('pwsh -NoProfile -WorkingDirectory /c -Command "git push --force"', 'pwsh -wd /c -c "rm -rf data"',
                  'powershell /c "x" -c "cat .env"', "powershell -NoProfile \"sh -c 'echo hi'\" > ~/.claude/CLAUDE.md",
                  "powershell /c \"bash -c 'echo hi'\" > ~/.claude/CLAUDE.md",
                  "powershell -Com \"bash -c 'echo hi'\" >> ~/.claude/settings.json", "cd ~/.claude/rules; tee $f"):
            with self.subTest(c):
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_sixth_review_crash_speed_and_inbox(self):
        import time
        Path(self.cwd, "data").mkdir()
        Path(self.cwd, "data", "f.txt").write_text("untracked", encoding="utf-8")
        for tool, c in (("Bash", "cp -t x y -t"), ("PowerShell", "ln -t x y -t")):
            with self.subTest(c):
                self.assertIn(self.cls(c, tool=tool), (policy.ASK, policy.ALLOW))  # no crash
        for c in ("Set-Content C:/Windows/System32/*/" + "a/" * 2000 + "x x; Remove-Item -Recurse -Force data",
                  "Set-Content " + "+".join(["$env:USERPROFILE"] * 3000) + " x; Remove-Item -Recurse -Force data",
                  "Set-Content @(" + ",".join(["$env:USERPROFILE"] * 2000) + ") x; Remove-Item -Recurse -Force data"):
            with self.subTest(c[:40]):
                start = time.monotonic()
                self.assertEqual(self.cls(c), policy.ASK)
                self.assertLess(time.monotonic() - start, 8)
        Path(self.home, ".claude", "agent-playbook").mkdir()
        self.assertEqual(self.cls("cd ~/.claude/agent-playbook; ac ./lessons-inbox.md x"), policy.ALLOW)

    def test_seventh_review_cases_ask(self):
        Path(self.cwd, "sub").mkdir()
        Path(self.cwd, "sub", "f.txt").write_text("untracked", encoding="utf-8")
        link_dir(Path(self.home, ".claude"), Path(self.cwd, "cfg"))
        unc = "//?/" + Path(self.cwd).as_posix() if os.name == "nt" else Path(self.cwd).as_posix()
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        Path(outside, ".env").write_text("SECRET=1", encoding="utf-8")
        cds = "; ".join(f"cd {Path(self.tmp.name, f'n{i}').as_posix()}" for i in range(14))
        for tool, c in (("PowerShell", f"echo x > {unc}/cfg/settings.json"), ("Bash", f"cp a.md '{unc}/cfg/rules/z.md'"),
                        ("PowerShell", ". tools/install.py --yes"), ("Bash", "$x = tools/install.py --yes"),
                        ("PowerShell", "$null = ./tools/install.py --yes"),
                        ("PowerShell", f"{cds}; iex 'cd {Path(self.tmp.name, 'zz').as_posix()}'; "
                                       f"cd {outside.as_posix()}; cat .e*"),
                        ("Bash", "env (x)"),
                        ("PowerShell", "$t = @'\ndon't\n'@\nSet-Content -Path ~/.claude/CLAUDE.md -Value $t"),
                        ("PowerShell", "$t = @'\ndon't\n'@\nRemove-Item -Recurse -Force sub"),
                        ("PowerShell", 'cd ~/.claude; [IO.File]::WriteAllText("$PWD/settings.json", $s)'),
                        ("PowerShell", "Set-Content FileSystem::" + Path(self.home).as_posix() + "/.claude/settings.json x"),
                        ("PowerShell", "fsutil hardlink create hl ~/.claude/settings.json"),
                        ("PowerShell", "[IO.Compression.ZipFile]::ExtractToDirectory('a.zip', \"$HOME/.claude/skills\")"),
                        ("PowerShell", '&("sc") ~/.claude/settings.json x')):
            with self.subTest(c[:60]):
                self.assertEqual(self.cls(c, tool=tool), policy.ASK)
        inp = {"file_path": f"{unc}/cfg/rules/new.md", "content": "x"}
        self.assertEqual(policy.classify("Write", inp, policy.Context(cwd=str(self.cwd)))[0], policy.ASK)

    def test_seventh_review_speed_and_crash(self):
        import time
        for tool, c in (("PowerShell", "tee " + " ".join(f"a{i}" for i in range(100_000))),
                        ("PowerShell", "Remove-Item a\0*"), ("Bash", "rm a\0*")):
            with self.subTest(c[:30]):
                start = time.monotonic()
                self.assertIn(self.cls(c, tool=tool), (policy.ASK, policy.ALLOW))  # no crash
                self.assertLess(time.monotonic() - start, 8)

    def test_eighth_review_cases(self):
        import time
        for c in ("Add-Content -Filter '' ~/.claude/rules/x.md x", "Set-Content -Stream '' ~/.claude/CLAUDE.md x",
                  "Set-Content -Exclude '' ~/.claude/settings.json '{}'", "Out-File -InputObject '' ~/.claude/CLAUDE.md"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        for c in ("# @'\n" * 20_000 + "git push --force", "powershell -c \"" + "; ".join(f"x{i}" for i in range(10_000)) + '"'):
            with self.subTest(c[:30]):
                start = time.monotonic()
                self.cls(c)
                self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(self.cls("# @'\n" * 20_000 + "git push --force"), policy.ASK)
        skill = Path(self.home, ".claude", "skills", "foo")
        skill.mkdir(parents=True)
        inp = {"file_path": "$X/notes.md", "content": "x"}  # asked before Q102A: kept
        self.assertEqual(policy.classify("Write", inp, policy.Context(cwd=str(skill)))[0], policy.ASK)

    def test_a_here_string_after_an_opener_with_no_close_is_cut(self):  # ninth review
        c = "$s = '@\"\n'\n$t = @'\ndon't\n'@\nSet-Content -Path ~/.claude/CLAUDE.md -Value $t"
        self.assertEqual(self.cls(c), policy.ASK)

    def test_schtasks_query_only_lists(self):  # Q145A: a real `$t = schtasks /query ...` seen in use
        for c in ("$t = schtasks /query /fo CSV /v", "schtasks /Query /tn x"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ALLOW)
        for c in ("schtasks /create /tn x /tr y", "schtasks /delete /tn x /f", "$t = schtasks /change /tn x /disable",
                  "schtasks /run /tn x", "schtasks /query /create /tn x /tr y", "schtasks -query /tn x -delete"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
                self.assertEqual(self.cls(c, tool="Bash"), policy.ASK)

    def test_many_folders_and_copies_stay_fast(self):  # the hook has 10 s; a timeout blocks the call
        import time
        for command in ("cd a; cd b; cd c; cd d; " + "; ".join(f"Copy-Item a{i} b{i} c{i} d{i} e{i} f{i} g{i} h{i}"
                                                              for i in range(100)),
                        "cd a; cd b; cd c; cd d; " + "; ".join(f"cp a{i} b{i} c{i} d{i} e{i} f{i} g{i} h{i}"
                                                              for i in range(50)),
                        "Set-Content (Join-Path " + " " * 5000 + "x"):
            with self.subTest(command[:40]):
                start = time.monotonic()
                self.assertEqual(self.cls(command), policy.ALLOW)
                self.assertLess(time.monotonic() - start, 3)

    def test_text_windows_cannot_resolve_does_not_crash(self):  # WinError 1113 on some text with ':' and CJK
        from unittest import mock

        def unresolvable(*args, **kwargs):
            raise OSError(1113, "No mapping for the Unicode character exists in the target multi-byte code page")
        with mock.patch.object(policy.os.path, "realpath", unresolvable):
            self.assertEqual(self.cls("sed -i \"s/a/{'m': 1}, # 报告/\" t.py", tool="Bash"), policy.ALLOW)
            self.assertEqual(self.cls("Set-Content -Path ~/.claude/CLAUDE.md -Value x"), policy.ASK)

    def test_write_through_a_link_judges_the_real_path(self):
        link_dir(Path(self.home, ".claude"), Path(self.cwd, "cfg"))
        for c in ("Set-Content -Path cfg/settings.json -Value x", "Copy-Item x.md cfg/rules",
                  "Add-Content cfg\\CLAUDE.md x"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)

    def test_inbox_in_a_linked_folder_asks_in_every_home_spelling(self):
        link_dir(Path(self.home, ".claude", "rules"), Path(self.home, ".claude", "agent-playbook"))
        for c in ("Add-Content -Encoding utf8 -Path ~/.claude/agent-playbook/lessons-inbox.md -Value x",
                  "Add-Content -Path $HOME/.claude/agent-playbook/lessons-inbox.md -Value x",
                  "Add-Content -Path $env:USERPROFILE\\.claude\\agent-playbook\\lessons-inbox.md -Value x"):
            with self.subTest(c):
                self.assertEqual(self.cls(c), policy.ASK)
        self.assertEqual(self.cls("echo x >> $HOME/.claude/agent-playbook/lessons-inbox.md", tool="Bash"),
                         policy.ASK)
