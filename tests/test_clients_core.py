"""playbook_hook.py --client: adapters, and the approval code for agents without a prompt."""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import mkdtemp  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src" / "hooks"

# Passes events through unchanged and prints what the hook emitted, so the core can be tested
# without any real client.
DUMMY = '''
import json
CAN_ASK = False
def normalize(raw):
    return raw.get("event")
def render(event, outputs, raw):
    return json.dumps({"outputs": outputs}), 0
'''


class ApprovalCode(unittest.TestCase):
    def setUp(self):
        self.hooks = Path(mkdtemp())
        for f in SRC.glob("*.py"):
            shutil.copy2(f, self.hooks / f.name)
        (self.hooks / "client_dummy.py").write_text(DUMMY, encoding="utf-8")
        self.home = mkdtemp()
        self.cwd = mkdtemp()

    def run_hook(self, event):
        out = subprocess.run([sys.executable, str(self.hooks / "playbook_hook.py"), "--client", "dummy"],
                             input=json.dumps({"event": event}).encode(), capture_output=True, timeout=60,
                             env={**os.environ, "AGENT_PLAYBOOK_HOME": self.home})
        self.assertEqual(out.returncode, 0, out.stderr.decode()[-500:])
        return json.loads(out.stdout)["outputs"]

    def push(self, command="git push --force origin main"):
        return self.run_hook({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": self.cwd,
                              "tool_name": "Bash", "tool_input": {"command": command}})

    def prompt(self, text):
        return self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": self.cwd,
                              "prompt": text})

    @staticmethod
    def code_of(outputs):
        hso = outputs[0]["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny", hso
        return re.search(r'approve ([A-Z0-9]{4})"', hso["permissionDecisionReason"]).group(1)

    def test_user_code_lets_the_same_call_through_once(self):
        code = self.code_of(self.push())
        self.assertEqual(self.code_of(self.push()), code)  # asking again gives the same code
        self.prompt(f"好的，批准 {code}")
        self.assertEqual(self.push(), [])  # allowed: no decision
        self.code_of(self.push())  # used up: denied again

    def test_code_covers_only_the_exact_call(self):
        code = self.code_of(self.push())
        self.prompt(f"approve {code}")
        self.code_of(self.push("git push --force origin other"))  # a different call is still denied
        self.assertEqual(self.push(), [])

    def test_a_wrong_code_or_no_code_approves_nothing(self):
        self.code_of(self.push())
        self.prompt("approve ZZZZ")
        self.code_of(self.push())
        self.prompt("yes go ahead")
        self.code_of(self.push())

    def test_unknown_event_from_the_adapter_is_ignored(self):
        self.assertEqual(self.run_hook(None), [])


if __name__ == "__main__":
    unittest.main()
