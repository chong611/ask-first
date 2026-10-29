"""v1.0.0 (Q150-Q157): version, opt-in profiles kept on update, the owner profile's own file."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import mkdtemp  # noqa: E402
import build  # noqa: E402
import install  # noqa: E402


def run(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = install.main(list(args))
    return code, out.getvalue()


class VersionTest(unittest.TestCase):
    def test_version_is_semver_and_printed(self):
        self.assertRegex(build.VERSION, r"^\d+\.\d+\.\d+$")
        with tempfile.TemporaryDirectory() as home:
            self.assertIn(f"agent-playbook {build.VERSION}", run("global", "--home", home)[1])

    def test_changelog_has_this_version(self):
        self.assertIn(f"## [{build.VERSION}]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.rules = self.home / ".claude" / "rules" / "agent-playbook.md"

    def test_fresh_install_is_core_only(self):
        run("global", "--yes", "--home", str(self.home))
        self.assertNotIn("# profile:", self.rules.read_text(encoding="utf-8"))

    def test_chosen_profiles_survive_an_update(self):
        run("global", "--yes", "--profile", "lang-zh-CN", "--profile", "advisor", "--home", str(self.home))
        run("global", "--yes", "--home", str(self.home))  # a later update without flags
        text = self.rules.read_text(encoding="utf-8")
        self.assertIn("# profile: lang-zh-CN", text)
        self.assertIn("# profile: advisor", text)
        state = json.loads((self.home / ".claude/agent-playbook/installed.json").read_text(encoding="utf-8"))
        self.assertEqual((state["version"], state["profiles"]), (build.VERSION, ["lang-zh-CN", "advisor"]))
        run("global", "--yes", "--no-profiles", "--home", str(self.home))
        self.assertNotIn("# profile:", self.rules.read_text(encoding="utf-8"))

    def test_profiles_read_from_an_old_rules_file(self):
        self.rules.parent.mkdir(parents=True)
        self.rules.write_text("# agent-playbook core\n# profile: lang-zh-CN\n# profile: owner\n", encoding="utf-8")
        self.assertEqual(install.installed_profiles(self.home), ["lang-zh-CN"])

    def test_owner_profile_moves_once_and_is_never_overwritten(self):
        src = Path(mkdtemp()) / "profiles"
        src.mkdir()
        (src / "owner.md").write_text("- Keep replies short.\n", encoding="utf-8")
        with mock.patch.object(install, "OLD_OWNER", src / "owner.md"):
            run("global", "--yes", "--home", str(self.home))
            owner = self.home / ".claude" / "rules" / "owner.md"
            self.assertEqual(owner.read_text(encoding="utf-8"), "- Keep replies short.\n")
            self.assertNotIn("Keep replies short", self.rules.read_text(encoding="utf-8"))
            owner.write_text("- edited by the user\n", encoding="utf-8")
            run("global", "--yes", "--home", str(self.home))
            self.assertEqual(owner.read_text(encoding="utf-8"), "- edited by the user\n")
            run("uninstall", "global", "--yes", "--home", str(self.home))
            self.assertTrue(owner.exists())  # the user's own file stays


if __name__ == "__main__":
    unittest.main()
