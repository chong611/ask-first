"""Regression tests from an independent review of the M2 milestone.
Each asserts the correct behaviour; all failed against the first M2 commit."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "src" / "hooks"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import mkdtemp  # noqa: E402
import install  # noqa: E402
import policy  # noqa: E402
import report  # noqa: E402


def link_dir(target, link) -> None:
    """A directory link. On Windows a junction, which needs no admin rights or Developer Mode."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)


def can_link_files() -> bool:
    """Whether this OS and filesystem can create file symlinks (Windows needs admin rights or Developer Mode)."""
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(os.path.join(d, "target"), os.path.join(d, "link"))
        except OSError:
            return False
    return True


CAN_LINK_FILES = can_link_files()
ASK, ALLOW = policy.ASK, policy.ALLOW


def bash(command, **ctx):
    return policy.classify("Bash", {"command": command}, policy.Context(**ctx))[0]


class GitRepo(unittest.TestCase):
    """A real repo: committed app.py (with uncommitted edits), committed 笔记.md, untracked files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        run = lambda *a: subprocess.run(a, cwd=self.d, capture_output=True, check=True)
        run("git", "init", "-q")
        Path(self.d, "app.py").write_text("v1", encoding="utf-8")
        Path(self.d, "笔记.md").write_text("a", encoding="utf-8")
        run("git", "add", ".")
        run("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
        Path(self.d, "app.py").write_text("uncommitted work", encoding="utf-8")
        out = subprocess.run(["git", "ls-files"], cwd=self.d, capture_output=True, encoding="utf-8").stdout
        self.ctx = policy.Context(cwd=self.d, tracked=set(out.splitlines()))

    def tearDown(self):
        self.tmp.cleanup()

    def cls(self, tool, **inp):
        return policy.classify(tool, inp, self.ctx)[0]


# --- 1. bypasses -----------------------------------------------------------------

class NewlineBypass(unittest.TestCase):
    def test_multiline_commands_are_split(self):
        # shlex treats "\n" as whitespace, so line 2 is glued onto line 1's argv.
        for c in ("cd repo\ngit push --force origin main", "echo start\ngh pr create --fill",
                  "python3 -m pytest -q\ngit reset --hard", "git status\ncrontab -e"):
            with self.subTest(c):
                self.assertEqual(bash(c), ASK)


class ShellGrammarBypass(unittest.TestCase):
    def test_subshell_group_control_flow_substitution(self):
        for c in ("(git push -f)", "{ git push -f; }", "if true; then git push -f; fi",
                  "for b in x; do git push -f; done", "echo $(gh pr create --fill)", "eval git push -f",
                  "bash -lc 'git push -f'", "bash -c -- 'git push -f'"):
            with self.subTest(c):
                self.assertEqual(bash(c), ASK)


class GitBypass(unittest.TestCase):
    def test_inline_alias(self):
        self.assertEqual(bash("git -c alias.p='push --force' p origin main"), ASK)

    def test_discarding_forms(self):
        for c in ("git restore src/a.py", "git checkout -f main", "git switch --discard-changes main",
                  "git worktree remove --force wt", "git reflog expire --expire=now --all"):
            with self.subTest(c):
                self.assertEqual(bash(c), ASK)


class GhCurlBypass(unittest.TestCase):
    def test_gh_writes(self):
        for c in ("gh api repos/o/r/pulls -f title=x",          # -f implies POST
                  "gh api -XPOST repos/o/r/pulls", "gh api --method=POST repos/o/r/pulls",
                  "gh api -X POST repos/o/r/issues/1/comments -f body=x",
                  "gh issue comment 1 --body x", "gh pr comment 1 --body x", "gh pr review --approve 1",
                  "gh gist create f --public", "gh secret set FOO", "gh repo edit --visibility public"):
            with self.subTest(c):
                self.assertEqual(bash(c), ASK)

    def test_curl_wget_writes(self):
        for c in ("curl -sX POST https://api.x", "curl --json '{}' https://api.x",
                  "curl -T f https://api.x", "wget --post-data=x https://api.x"):
            with self.subTest(c):
                self.assertEqual(bash(c), ASK)


class DeleteBypass(GitRepo):
    def test_rm_dot_git(self):
        self.assertEqual(self.cls("Bash", command="rm -rf .git"), ASK)

    def test_rm_tracked_file_with_uncommitted_changes(self):
        self.assertEqual(self.cls("Bash", command="rm app.py"), ASK)

    def test_tilde_var_and_glob_targets(self):
        home, other = mkdtemp(), mkdtemp()
        Path(home, "photos").mkdir()
        Path(home, "photos", "p.jpg").write_text("", encoding="utf-8")
        # Bash takes ~ from $HOME; PowerShell, and Python on Windows, take it from %USERPROFILE%.
        cases = [({"HOME": home}, ("rm -rf ~/photos", "rm -rf $HOME/photos")),
                 ({"HOME": other, "USERPROFILE": home}, ("rm -rf ~/photos", "cd ~ && rm -rf photos"))]
        old = os.environ.copy()
        try:
            for env, commands in cases:
                os.environ.update(env)
                for c in commands:
                    with self.subTest(c, **env):
                        self.assertEqual(self.cls("Bash", command=c), ASK)
        finally:
            os.environ.clear()
            os.environ.update(old)
        Path(self.d, "untracked.db").write_text("x", encoding="utf-8")
        self.assertEqual(self.cls("Bash", command="rm -f *.db"), ASK)

    def test_xargs_find_inline_scripts_powershell(self):
        for c in ("ls | xargs rm -rf", "find . -name '*.db' -delete",
                  "python3 -c \"import shutil; shutil.rmtree('data')\""):
            with self.subTest(c):
                self.assertEqual(self.cls("Bash", command=c), ASK)
        docs = mkdtemp()  # data outside the project, in this platform's own path form
        Path(docs, "notes.txt").write_text("user data", encoding="utf-8")
        self.assertEqual(self.cls("PowerShell", command=f"Remove-Item -Recurse -Force '{docs}'"), ASK)
        if os.name != "nt":  # a Windows path seen from Linux or macOS cannot be checked, so it asks
            self.assertEqual(self.cls("PowerShell", command="Remove-Item -Recurse -Force C:\\Users\\u\\Documents"), ASK)


class WriteBypass(GitRepo):
    def test_write_over_existing_file_outside_cwd(self):
        other = mkdtemp()
        Path(other, "budget.xlsx").write_text("user data", encoding="utf-8")
        self.assertEqual(self.cls("Write", file_path=f"{other}/budget.xlsx", content="x"), ASK)

    def test_global_hook_code_and_config_are_self_instructions(self):
        for p in ("/home/u/.claude/agent-playbook/hooks/policy.py", "/home/u/.claude/agent-playbook/config.json",
                  "/home/u/.claude/agent-playbook/state/s1.json", "/home/u/.claude.json",
                  f"{self.d}/.mcp.json", f"{self.d}/.claude/agents/x.md", f"{self.d}/.claude/commands/x.md"):
            with self.subTest(p):
                self.assertEqual(self.cls("Write", file_path=p, content="x"), ASK)

    def test_symlink_to_settings(self):
        home = mkdtemp()
        Path(home, ".claude").mkdir()
        Path(home, ".claude", "settings.json").write_text("{}", encoding="utf-8")
        link_dir(Path(home, ".claude"), Path(self.d, "cfg"))
        self.ctx.tracked.add("cfg/settings.json")  # a committed link: git "can restore" the link, not the target
        inp = {"file_path": f"{self.d}/cfg/settings.json", "content": "{}"}
        decision, reason = policy.classify("Write", inp, self.ctx)
        self.assertEqual(decision, ASK)
        self.assertIn("agent instructions or settings", reason)  # not only "overwrites a file outside the project"

    @unittest.skipUnless(CAN_LINK_FILES, "cannot create file symlinks here (Windows: admin rights or Developer Mode)")
    def test_file_symlink_to_settings(self):  # the file's own name is the disguise; above, it keeps its name
        home = mkdtemp()
        Path(home, ".claude").mkdir()
        Path(home, ".claude", "settings.json").write_text("{}", encoding="utf-8")
        os.symlink(Path(home, ".claude", "settings.json"), Path(self.d, "cfg.json"))
        self.ctx.tracked.add("cfg.json")  # a committed symlink: git "can restore" the link, not the target
        inp = {"file_path": f"{self.d}/cfg.json", "content": "{}"}
        decision, reason = policy.classify("Write", inp, self.ctx)
        self.assertEqual(decision, ASK)
        self.assertIn("agent instructions or settings", reason)

    def test_case_insensitive_instruction_file(self):  # macOS/Windows default filesystems
        self.assertEqual(self.cls("Write", file_path=f"{self.d}/claude.md", content="x"), ASK)


class McpBypass(unittest.TestCase):
    def test_outward_tools(self):
        for t in ("mcp__github__enable_pr_auto_merge", "mcp__github__pull_request_review_write",
                  "mcp__github__add_reply_to_pull_request_comment", "mcp__github__actions_run_trigger",
                  "mcp__Google_Drive__update_file", "mcp__claude-code-remote__create_trigger", "RemoteTrigger"):
            with self.subTest(t):
                self.assertEqual(policy.classify(t, {}, policy.Context())[0], ASK)


class SecretBypass(unittest.TestCase):
    def test_print_secret(self):
        for c in ("env", "env | grep KEY", "printenv ANTHROPIC_API_KEY", "base64 .env",
                  "python3 -c \"print(open('.env').read())\""):
            with self.subTest(c):
                self.assertEqual(bash(c), ASK)

    def test_read_credential_files(self):
        for p in ("/home/u/.claude/.credentials.json", "/home/u/.config/gh/hosts.yml", "/home/u/.npmrc",
                  "/home/u/.git-credentials", "C:\\proj\\.env"):
            with self.subTest(p):
                self.assertEqual(policy.classify("Read", {"file_path": p}, policy.Context())[0], ASK)

    def test_grep_tool_on_env(self):
        d = policy.classify("Grep", {"pattern": ".", "path": "/r/.env", "output_mode": "content"}, policy.Context())
        self.assertEqual(d[0], ASK)


class InstallFlagAbbrev(unittest.TestCase):
    def test_argparse_prefix_of_yes(self):
        self.assertEqual(bash("python3 tools/install.py global --ye"), ASK)


# --- 2. over-asking ----------------------------------------------------------------

class OverAsk(GitRepo):
    def test_edit_new_file_agent_created(self):
        # Write new.py (allowed), then Edit it: asks on every edit until committed.
        Path(self.d, "new_module.py").write_text("created by the agent in this task", encoding="utf-8")
        self.assertEqual(self.cls("Edit", file_path=f"{self.d}/new_module.py", old_string="a", new_string="b"),
                         ALLOW)

    def test_edit_tracked_chinese_filename(self):
        # git ls-files quotes non-ASCII paths ("\347\254\224...") unless -z is used.
        # sys.executable, not "python3": on Windows that is often a Store stub that runs nothing.
        out = subprocess.run([sys.executable, str(HOOKS / "playbook_hook.py")], capture_output=True, encoding="utf-8",
                             input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": self.d,
                                               "session_id": "t", "tool_input": {"file_path": f"{self.d}/笔记.md",
                                                                                 "old_string": "a", "new_string": "b"}}),
                             env={**os.environ, "AGENT_PLAYBOOK_HOME": mkdtemp()})
        self.assertEqual((out.returncode, out.stdout.strip()), (0, ""), out.stderr)

    def test_cleanup_of_build_artifacts(self):
        for p in ("build", ".pytest_cache", "node_modules"):
            Path(self.d, p).mkdir()
            Path(self.d, p, "f").write_text("", encoding="utf-8")
        self.assertEqual(self.cls("Bash", command="rm -rf build .pytest_cache node_modules"), ALLOW)

    def test_local_api_and_example_env(self):
        for c in ("curl -X POST http://127.0.0.1:8000/api/orders -d '{}'", "cat .env.example"):
            with self.subTest(c):
                self.assertEqual(self.cls("Bash", command=c), ALLOW)
        self.assertEqual(self.cls("Read", file_path=f"{self.d}/.env.example"), ALLOW)

    def test_push_head_to_allowed_branch(self):
        subprocess.run(["git", "checkout", "-qb", "claude/x"], cwd=self.d, check=True)
        ctx = policy.Context(cwd=self.d, allowed_branches=("claude/x",), tracked=self.ctx.tracked)
        self.assertEqual(policy.classify("Bash", {"command": "git push -u origin HEAD"}, ctx)[0], ALLOW)


# --- 3. hook robustness ----------------------------------------------------------

class HookRobustness(unittest.TestCase):
    def setUp(self):
        self.home = mkdtemp()

    def run_hook(self, stdin, env=None, cwd=None):
        return subprocess.run([sys.executable, str(HOOKS / "playbook_hook.py")], input=stdin, capture_output=True,
                              env={**os.environ, "AGENT_PLAYBOOK_HOME": self.home, **(env or {})}, timeout=60,
                              cwd=cwd)

    def test_malformed_input_never_crashes(self):
        # A crash on PreToolUse + onFailure:block blocks the tool; a crash should degrade to "ask", not exit 1.
        for raw in (b"", b"not json", b"[1]", b"null",
                    b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":"x","cwd":"/"}',
                    b'{"hook_event_name":"PreToolUse","tool_name":null,"tool_input":{},"cwd":"/"}',
                    b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"},"cwd":null}',
                    b'{"hook_event_name":"PreToolUse","tool_name":"MultiEdit","tool_input":{"file_path":"/x","edits":["a"]},"cwd":"/"}'):
            with self.subTest(raw):
                self.assertEqual(self.run_hook(raw).returncode, 0)

    def test_windows_cp1252_stdin_with_chinese_content(self):
        ev = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": "/tmp", "session_id": "s",
              "tool_input": {"file_path": "/tmp/zz/a.md", "content": "根据数据"}}
        out = self.run_hook(json.dumps(ev, ensure_ascii=False).encode("utf-8"), env={"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(out.returncode, 0, out.stderr[-300:])

    def test_utf8_bom_stdin_still_decides(self):
        # Windows PowerShell 5.1 prefixes piped text with a UTF-8 BOM; it must not turn a decision into silence.
        ev = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.home, "session_id": "s",
              "permission_mode": "default", "tool_input": {"command": "git push --force origin main"}}
        out = self.run_hook(b"\xef\xbb\xbf" + json.dumps(ev).encode() + b"\r\n")
        self.assertIn('"permissionDecision": "ask"', out.stdout.decode(), out.stderr[-300:])

    def test_repo_with_non_utf8_filename(self):
        d = mkdtemp()
        subprocess.run(["git", "init", "-q", d], check=True)
        # Straight into the index (as a clone from Linux would): Windows cannot create this name on disk.
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=d, input=b"", capture_output=True,
                              check=True).stdout.strip()
        subprocess.run(["git", "update-index", "--index-info"], cwd=d, input=b"100644 " + blob + b"\tbad\xff.txt\n",
                       check=True)
        subprocess.run(["git", "config", "core.quotePath", "false"], cwd=d, check=True)
        ev = {"hook_event_name": "PreToolUse", "tool_name": "Read", "cwd": d, "tool_input": {"file_path": f"{d}/x"}}
        out = self.run_hook(json.dumps(ev).encode())
        self.assertEqual((out.returncode, out.stdout), (0, b""), out.stderr)  # else every tool call asks

    def test_stop_after_read_only_pipeline_does_not_demand_report(self):
        repo = mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        base = {"session_id": "ro", "cwd": repo}
        for ev in ({"hook_event_name": "UserPromptSubmit", "prompt": "how many commits?"},
                   {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "git log --oneline | wc -l"}}):
            self.run_hook(json.dumps({**base, **ev}).encode())
        out = self.run_hook(json.dumps({**base, "hook_event_name": "Stop",
                                        "last_assistant_message": "There are 12 commits."}).encode())
        self.assertEqual(out.stdout.strip(), b"")

    def test_stop_flags_state_changes_hidden_from_snapshot(self):
        # git snapshot is HEAD + status: deleting a branch or running find -delete on ignored files leaves it equal.
        repo = mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        base = {"session_id": "bd", "cwd": repo}
        for ev in ({"hook_event_name": "UserPromptSubmit", "prompt": "clean up"},
                   {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "git branch -D old"}}):
            self.run_hook(json.dumps({**base, **ev}).encode())
        out = self.run_hook(json.dumps({**base, "hook_event_name": "Stop",
                                        "last_assistant_message": "Cleaned up."}).encode())
        self.assertIn(b"block", out.stdout)


class PostToolUseMatcher(unittest.TestCase):
    def test_mcp_and_powershell_changes_are_recorded(self):
        import build
        matcher = json.loads(build.settings_fragment("x"))["hooks"]["PostToolUse"][0]["matcher"]
        import re
        for tool in ("mcp__Google_Drive__update_file", "PowerShell"):
            with self.subTest(tool):
                self.assertTrue(re.search(matcher, tool))


class ReportPerformance(unittest.TestCase):
    def test_whitespace_lines_are_linear(self):
        # 300 whitespace-only lines take ~6 s (cubic backtracking); Stop runs parse twice, timeout is 15 s.
        t = time.time()
        report.check("   \n" * 300)
        self.assertLess(time.time() - t, 0.5)


# --- 4. install.py -------------------------------------------------------------------

class InstallDataLoss(unittest.TestCase):
    def setUp(self):
        self.repo = Path(mkdtemp())
        (self.repo / ".claude").mkdir()

    def write_settings(self, obj):
        (self.repo / ".claude" / "settings.json").write_text(json.dumps(obj), encoding="utf-8")

    def settings(self):
        return json.loads((self.repo / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_keeps_unrelated_hooks(self):
        self.write_settings({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                          "command": "python3 ~/code/agent-playbook-fork/lint.py"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"},
                                {"type": "command", "command": "python3 /x/agent-playbook/old.py"}]}]}})
        install.apply(install.plan_project(self.repo))
        cmds = [h["command"] for es in self.settings()["hooks"].values() for e in es for h in e["hooks"]]
        self.assertIn("python3 ~/code/agent-playbook-fork/lint.py", cmds)
        self.assertIn("notify-send done", cmds)

    def test_backup_of_original_survives_second_install(self):
        original = '{"model": "opus"}'
        (self.repo / ".claude" / "settings.json").write_text(original, encoding="utf-8")
        install.apply(install.plan_project(self.repo))
        s = self.settings()
        s["hooks"]["PreToolUse"][-1]["hooks"][0]["timeout"] = 99  # any later change, e.g. an upgrade
        self.write_settings(s)
        install.apply(install.plan_project(self.repo))
        backups = list((self.repo / ".claude").glob("settings.json.bak*"))
        self.assertTrue(any(b.read_text(encoding="utf-8") == original for b in backups))

    def test_bom_and_null_sections_are_handled(self):
        (self.repo / ".claude" / "settings.json").write_bytes(b'\xef\xbb\xbf{"model":"opus"}')
        install.plan_project(self.repo)  # raises JSONDecodeError today
        self.write_settings({"hooks": None, "permissions": {"ask": None}})
        install.plan_project(self.repo)  # raises AttributeError today

    def test_unterminated_old_block_is_not_duplicated(self):
        (self.repo / "AGENTS.md").write_text("team\n<!-- agent-playbook:core v0.2 -->\nold core\n", encoding="utf-8")
        install.apply(install.plan_project(self.repo))
        self.assertEqual((self.repo / "AGENTS.md").read_text(encoding="utf-8").count("<!-- agent-playbook:core"), 1)

    def test_prose_mention_of_import_is_not_an_import(self):
        (self.repo / "CLAUDE.md").write_text("We do not use @AGENTS.md here.\n", encoding="utf-8")
        install.apply(install.plan_project(self.repo))
        self.assertIn("\n@AGENTS.md\n", (self.repo / "CLAUDE.md").read_text(encoding="utf-8"))


# --- 5. report parser ------------------------------------------------------------------

TAIL = "Not done (0)\nUnverified (0)\nFound but not fixed (0)\nWaiting on you (0)\n"


class ReportFalseBlocks(unittest.TestCase):
    def test_correct_reports_pass(self):
        for msg in (
            "**Done:** 1\n- ran tests: 43 passed\n**Not done:** 0\n**Unverified:** 0\n"
            "**Found but not fixed:** 0\n**Waiting on you:** 0\n",
            "Done (1)\n- fixed parser\n  - checked: ran pytest, 43 passed\n  - why: test fails without the fix\n" + TAIL,
            "完成（1 项）\n- 改了 a.py：pytest 通过\n未完成（0 项）\n未验证（0 项）\n发现未修（0 项）\n等你决定（0 项）\n",
            "## 1. Done (1)\n- ran tests: 3 passed\n## 2. Not done (0)\n## 3. Unverified (0)\n"
            "## 4. Found but not fixed (0)\n## 5. Waiting on you (0)\n",
        ):
            with self.subTest(msg[:30]):
                self.assertIsNone(report.check(msg))


class ReportFakePasses(unittest.TestCase):
    def test_report_inside_code_fence_or_quote_does_not_count(self):
        for msg in ("Nothing to report.\n```\nDone (0)\n" + TAIL + "```\n",
                    "\n".join("> " + l for l in ("Done (0)\n" + TAIL).splitlines())):
            with self.subTest(msg[:20]):
                self.assertIsNotNone(report.check(msg))

    def test_counts_must_match_items_in_every_section(self):
        self.assertIsNotNone(report.check("Done (1)\n- a\nNot done (0)\n- big unfinished thing\n"
                                          "Unverified (0)\nFound but not fixed (0)\nWaiting on you (0)\n"))

    def test_report_must_be_at_the_end(self):
        self.assertIsNotNone(report.check("Done (0)\n" + TAIL + "\nThen I also rewrote the database layer."))


if __name__ == "__main__":
    unittest.main()
