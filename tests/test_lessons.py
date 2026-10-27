"""Q92: outside the agent-playbook repo, the lessons skill only notes the lesson."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "hooks"))
import policy  # noqa: E402


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


class LessonsScopeTest(unittest.TestCase):
    def test_skill_stops_outside_the_playbook_repo(self):
        text = (ROOT / "src" / "skills" / "lessons" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("lessons-inbox.md", text)
        self.assertIn("Do not open, edit or run anything of the playbook", text)

    def test_appending_to_the_inbox_does_not_ask(self):
        ctx = policy.Context(cwd=str(ROOT))
        for command in ('echo "- note" >> ~/.claude/agent-playbook/lessons-inbox.md',
                        "Add-Content -Encoding utf8 -Path ~/.claude/agent-playbook/lessons-inbox.md -Value x"):
            with self.subTest(command):
                tool = "PowerShell" if command.startswith("Add-") else "Bash"
                self.assertEqual(policy.classify(tool, {"command": command}, ctx)[0], policy.ALLOW)

    def test_playbook_settings_still_ask(self):
        ctx = policy.Context(cwd=str(ROOT))
        for command in ("echo x >> ~/.claude/agent-playbook/config.json", "echo x > ~/.claude/CLAUDE.md",
                        "echo x >> ~/.claude/agent-playbook/lessons-inbox.md.bak/../config.json"):
            with self.subTest(command):
                self.assertEqual(policy.classify("Bash", {"command": command}, ctx)[0], policy.ASK)


class InboxLinkTest(unittest.TestCase):
    @unittest.skipUnless(CAN_LINK_FILES, "cannot create file symlinks here (Windows: admin rights or Developer Mode)")
    def test_inbox_that_links_to_settings_still_asks(self):
        with tempfile.TemporaryDirectory() as home:
            base = os.path.join(home, ".claude", "agent-playbook")
            os.makedirs(base)
            open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8").close()
            inbox = os.path.join(base, "lessons-inbox.md")
            os.symlink(os.path.join(home, ".claude", "settings.json"), inbox)
            ctx = policy.Context(cwd=home)
            command = f"echo x >> {Path(inbox).as_posix()}"  # Bash reads a backslash as an escape
            self.assertEqual(policy.classify("Bash", {"command": command}, ctx)[0], policy.ASK)

    def test_inbox_folder_that_links_to_rules_still_asks(self):  # runs everywhere: a junction on Windows
        with tempfile.TemporaryDirectory() as home:
            rules = Path(home, ".claude", "rules")
            rules.mkdir(parents=True)
            link_dir(rules, Path(home, ".claude", "agent-playbook"))  # a note written here would load as a rule
            inbox = Path(home, ".claude", "agent-playbook", "lessons-inbox.md").as_posix()
            ctx = policy.Context(cwd=home)
            self.assertEqual(policy.classify("Bash", {"command": f"echo x >> {inbox}"}, ctx)[0], policy.ASK)


if __name__ == "__main__":
    unittest.main()
