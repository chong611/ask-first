import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build  # noqa: E402
import install  # noqa: E402


class ProjectInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_repo(self):
        plan = install.plan_project(self.repo)
        install.apply(plan)
        self.assertEqual((self.repo / "CLAUDE.md").read_text(encoding="utf-8").splitlines()[-1], "@AGENTS.md")
        self.assertIn("# agent-playbook core", (self.repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((self.repo / ".claude/skills/pb-status/SKILL.md").exists())
        self.assertTrue((self.repo / ".claude/hooks/agent-playbook/playbook_hook.py").exists())
        settings = json.loads((self.repo / ".claude/settings.json").read_text(encoding="utf-8"))
        pre = settings["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(pre["onFailure"], "block")
        self.assertIn("Bash(gh pr create *)", settings["permissions"]["ask"])
        self.assertEqual(install.diff(install.plan_project(self.repo)), [])  # idempotent

    def test_local_install_uses_this_python_and_stays_uncommitted(self):
        (self.repo / ".gitignore").write_text("data/\n", encoding="utf-8")
        install.apply(install.plan_project(self.repo, local=True))
        local = json.loads((self.repo / ".claude/settings.local.json").read_text(encoding="utf-8"))
        hook = local["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["command"], Path(sys.executable).as_posix())  # not "python3"
        self.assertFalse((self.repo / ".claude/settings.json").exists())
        self.assertEqual((self.repo / ".gitignore").read_text(encoding="utf-8"), "data/\n.claude/settings.local.json\n")
        self.assertEqual(install.diff(install.plan_project(self.repo, local=True)), [])

    def test_cli_defaults_to_local(self):
        self.assertEqual(install.main(["project", str(self.repo), "--yes"]), 0)
        self.assertTrue((self.repo / ".claude/settings.local.json").exists())
        self.assertFalse((self.repo / ".claude/settings.json").exists())

    def test_keeps_existing_files(self):
        (self.repo / "CLAUDE.md").write_text("# Team rules\nUse tabs.\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# Team agents file\n", encoding="utf-8")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "team.sh"}]}]},
            "permissions": {"ask": ["Bash(rm *)"], "allow": ["Bash(ls *)"]}}), encoding="utf-8")
        install.apply(install.plan_project(self.repo))
        claude = (self.repo / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(claude.startswith("# Team rules\nUse tabs."))
        self.assertIn("@AGENTS.md", claude)
        agents = (self.repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents.startswith("# Team agents file"))
        self.assertIn("# agent-playbook core", agents)
        settings = json.loads((self.repo / ".claude/settings.json").read_text(encoding="utf-8"))
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("team.sh", commands)
        self.assertEqual(settings["permissions"]["allow"], ["Bash(ls *)"])
        self.assertIn("Bash(rm *)", settings["permissions"]["ask"])
        self.assertTrue(list(self.repo.glob("CLAUDE.md.bak-agent-playbook-*")))

    def test_claude_md_with_a_bom_keeps_one_import(self):  # Windows PowerShell 5.1 saves files with a BOM
        (self.repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8-sig")
        install.apply(install.plan_project(self.repo))
        self.assertEqual((self.repo / "CLAUDE.md").read_text(encoding="utf-8-sig"), "@AGENTS.md\n")

    def test_backups_stay_bounded_and_keep_the_original(self):
        claude = self.repo / "CLAUDE.md"
        claude.write_text("original\n", encoding="utf-8")
        for i in range(6):
            install.apply(install.plan_project(self.repo))
            claude.write_text(f"version {i}\n", encoding="utf-8")  # no import line, so the next install must change it
        backups = sorted(self.repo.glob("CLAUDE.md.bak-agent-playbook-*"))
        self.assertEqual(len(backups), 3)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "original\n")

    def test_upgrade_replaces_old_block_only(self):
        install.apply(install.plan_project(self.repo))
        agents = self.repo / "AGENTS.md"
        old = agents.read_text(encoding="utf-8").replace("# agent-playbook core", "# agent-playbook core OLD")
        agents.write_text(old + "\nproject note\n", encoding="utf-8")
        install.apply(install.plan_project(self.repo))
        text = agents.read_text(encoding="utf-8")
        self.assertNotIn("OLD", text)
        self.assertIn("project note", text)
        self.assertEqual(text.count("<!-- agent-playbook:core"), 1)


class GlobalAndCheckTest(unittest.TestCase):
    def test_global_then_check_matches_project(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            install.apply(install.plan_global(Path(home), ["lang-zh-CN"]))
            install.apply(install.plan_project(Path(repo)))
            rules = Path(home, ".claude/rules/agent-playbook.md").read_text(encoding="utf-8")
            self.assertIn("# profile: lang-zh-CN", rules)
            self.assertTrue(Path(home, ".claude/agent-playbook/hooks/policy.py").exists())
            self.assertEqual(install.check(Path(repo), Path(home)), 0)
            agents = Path(repo, "AGENTS.md")
            edited = agents.read_text(encoding="utf-8").replace("Acting is not succeeding.", "x")
            agents.write_text(edited, encoding="utf-8")
            self.assertEqual(install.check(Path(repo), Path(home)), 1)

    def test_global_reinstall_does_not_duplicate_hooks(self):
        with tempfile.TemporaryDirectory() as home:
            install.apply(install.plan_global(Path(home)))
            install.apply(install.plan_global(Path(home)))
            settings = json.loads(Path(home, ".claude/settings.json").read_text(encoding="utf-8"))
            self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
            hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
            self.assertEqual(hook["command"], Path(sys.executable).as_posix())
            self.assertTrue(hook["args"][0].startswith(Path(home).as_posix()))
            self.assertEqual(install.diff(install.plan_global(Path(home))), [])

    def test_no_write_without_yes(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(install.main(["global", "--home", home]), 0)
            self.assertFalse(Path(home, ".claude").exists())


if __name__ == "__main__":
    unittest.main()
