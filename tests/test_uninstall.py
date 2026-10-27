"""Q133: uninstall removes only what agent-playbook installed, after a preview."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import install  # noqa: E402

USER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-check"}]}


def run(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = install.main(list(args))
    return code, out.getvalue()


class GlobalUninstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"
        self.settings.write_text(json.dumps({
            "model": "opus", "hooks": {"PreToolUse": [USER_HOOK]},
            "permissions": {"ask": ["Bash(git clean *)", "Bash(my-rule *)"]}}), encoding="utf-8")
        install.apply(install.plan_global(self.home))
        # an edit the user made after installing must survive the uninstall
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        data["theme"] = "dark"
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        log = self.claude / "agent-playbook" / "logs" / "tier2.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{}\n", encoding="utf-8")

    def test_preview_writes_nothing(self):
        before = self.settings.read_text(encoding="utf-8")
        code, out = run("uninstall", "global", "--home", str(self.home))
        self.assertEqual(code, 0)
        self.assertIn("Files to remove", out)
        self.assertIn("Nothing written", out)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertTrue((self.claude / "rules" / "agent-playbook.md").exists())

    def test_removes_ours_and_keeps_the_users(self):
        run("uninstall", "global", "--yes", "--home", str(self.home))
        self.assertFalse((self.claude / "rules" / "agent-playbook.md").exists())
        self.assertFalse((self.claude / "skills" / "pb-status").exists())
        self.assertFalse((self.claude / "agent-playbook" / "hooks").exists())
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["hooks"], {"PreToolUse": [USER_HOOK]})
        # "Bash(git clean *)" was there before the install, so it stays
        self.assertEqual(data["permissions"]["ask"], ["Bash(git clean *)", "Bash(my-rule *)"])
        self.assertTrue((self.claude / "agent-playbook" / "logs" / "tier2.jsonl").exists())  # kept without --purge
        self.assertIn("nothing to remove", run("uninstall", "global", "--home", str(self.home))[1])

    def test_purge_removes_logs_and_state(self):
        run("uninstall", "global", "--yes", "--purge", "--home", str(self.home))
        self.assertFalse((self.claude / "agent-playbook").exists())
        self.assertTrue(self.claude.exists())

    def test_a_skill_changed_by_the_user_is_kept(self):
        skill = self.claude / "skills" / "pb-status" / "SKILL.md"
        skill.write_text("my version\n", encoding="utf-8")
        code, out = run("uninstall", "global", "--yes", "--home", str(self.home))
        self.assertTrue(skill.exists())
        self.assertIn("kept", out)


class ProjectUninstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "CLAUDE.md").write_text("# My project\nUse tabs.\n", encoding="utf-8")
        install.apply(install.plan_project(self.repo, local=True))

    def test_restores_the_project(self):
        self.assertIn("@AGENTS.md", (self.repo / "CLAUDE.md").read_text(encoding="utf-8"))
        run("uninstall", str(self.repo), "--yes", "--home", str(self.repo / "home"))
        self.assertEqual((self.repo / "CLAUDE.md").read_text(encoding="utf-8"), "# My project\nUse tabs.\n")
        self.assertFalse((self.repo / "AGENTS.md").exists())
        self.assertFalse((self.repo / ".claude" / "settings.local.json").exists())
        self.assertFalse((self.repo / ".claude" / "hooks").exists())
        self.assertFalse((self.repo / ".claude" / "skills").exists())

    def test_agents_md_with_other_text_keeps_that_text(self):
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8") + "\nTeam rule: run tests.\n", encoding="utf-8")
        run("uninstall", str(self.repo), "--yes", "--home", str(self.repo / "home"))
        self.assertEqual(agents.read_text(encoding="utf-8"), "Team rule: run tests.\n")


if __name__ == "__main__":
    unittest.main()
