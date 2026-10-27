"""Found in an end-of-release check on Windows: over-asking on Get-ChildItem, a report heading form."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "hooks"))
import policy  # noqa: E402
import report  # noqa: E402


class GetChildItemTest(unittest.TestCase):
    def test_listing_a_folder_does_not_ask(self):
        ctx = policy.Context(cwd=str(ROOT))
        for command in ("Get-ChildItem", "gci", "Get-ChildItem -Force"):
            with self.subTest(command):
                self.assertEqual(policy.classify("PowerShell", {"command": command}, ctx)[0], policy.ALLOW)

    def test_listing_environment_variables_still_asks(self):
        ctx = policy.Context(cwd=str(ROOT))
        for command in ("Get-ChildItem env:", "Get-ChildItem Env:\\", "printenv"):
            with self.subTest(command):
                tool = "Bash" if command == "printenv" else "PowerShell"
                self.assertEqual(policy.classify(tool, {"command": command}, ctx)[0], policy.ASK)


class MiddleDotHeadingTest(unittest.TestCase):
    def test_count_after_a_middle_dot(self):
        msg = "改好了。\n\n**完成 · 1**\n- 改了 a（看过）\n**未完成 · 0**\n**未验证 · 0**\n**发现未修 · 0**\n**等你决定 · 0**\n"
        self.assertIsNone(report.check(msg))


if __name__ == "__main__":
    unittest.main()
