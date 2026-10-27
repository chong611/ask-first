"""Q134: replies that are mostly English under the Chinese profile are logged, never blocked."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "hooks"))
import report  # noqa: E402

ENGLISH = ("I changed the deploy script so that it copies the build into the releases folder. "
           "I have not run it, because that would copy files to the production server. ") * 3
CHINESE = "我把 deploy.sh 的目标目录改成了 releases，但没有在服务器上运行，所以效果还没验证。" * 3


class MostlyEnglishTest(unittest.TestCase):
    def test_english_prose_is_flagged(self):
        self.assertIsNotNone(report.mostly_english(ENGLISH))

    def test_chinese_with_code_and_paths_is_not(self):
        text = CHINESE + "\n```bash\n" + "git status && python3 tools/check.py\n" * 20 + "```\n`src/hooks/report.py`"
        self.assertIsNone(report.mostly_english(text))

    def test_short_english_is_not(self):
        self.assertIsNone(report.mostly_english("OK, done. Tests: 161 passed."))


class StopHookLogTest(unittest.TestCase):
    def run_stop(self, home: str, message: str) -> str:
        event = {"hook_event_name": "Stop", "session_id": "s1", "cwd": home, "last_assistant_message": message}
        env = {**os.environ, "AGENT_PLAYBOOK_HOME": os.path.join(home, ".claude", "agent-playbook"),
               "CLAUDE_CODE_ENTRYPOINT": "sdk-test"}
        out = subprocess.run([sys.executable, str(ROOT / "src" / "hooks" / "playbook_hook.py")],
                             input=json.dumps(event), capture_output=True, text=True, encoding="utf-8", env=env)
        return out.stdout

    def test_logged_only_with_the_zh_profile_and_never_blocked(self):
        with tempfile.TemporaryDirectory() as home:
            log = Path(home, ".claude", "agent-playbook", "logs", "language.jsonl")
            self.assertEqual(self.run_stop(home, ENGLISH).strip(), "")
            self.assertFalse(log.exists())  # no zh profile installed: nothing logged
            rules = Path(home, ".claude", "rules", "agent-playbook.md")
            rules.parent.mkdir(parents=True)
            rules.write_text("# profile: lang-zh-CN\n", encoding="utf-8")
            self.assertEqual(self.run_stop(home, ENGLISH).strip(), "")  # never a block
            self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["session"], "s1")


if __name__ == "__main__":
    unittest.main()
