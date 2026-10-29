"""A crashed or slow policy hook denies before-tool calls instead of letting the client run them."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import mkdtemp  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src" / "hooks"

BEFORE = {  # client -> a before-tool event in that client's own shape
    "gemini": {"hook_event_name": "BeforeTool", "tool_name": "run_shell_command",
               "tool_input": {"command": "git status"}},
    "cursor": {"hook_event_name": "beforeShellExecution", "command": "git status"},
    "codex": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git status"}},
    None: {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git status"}},
}


class FailClosed(unittest.TestCase):
    def setUp(self):
        self.hooks = Path(mkdtemp())
        for f in SRC.glob("*.py"):
            shutil.copy2(f, self.hooks / f.name)
        self.home = mkdtemp()
        self.cwd = mkdtemp()

    def append(self, name, text):
        with (self.hooks / name).open("a", encoding="utf-8") as fh:
            fh.write("\n" + text + "\n")

    def run_hook(self, client, event):
        args = [sys.executable, str(self.hooks / "playbook_hook.py")] + (["--client", client] if client else [])
        start = time.monotonic()
        out = subprocess.run(args, input=json.dumps({**event, "cwd": self.cwd, "session_id": "s"}).encode(),
                             capture_output=True, timeout=60, env={**os.environ, "AGENT_PLAYBOOK_HOME": self.home})
        return out.returncode, out.stdout.decode().strip(), time.monotonic() - start

    @staticmethod
    def denied(client, code, text):
        data = json.loads(text)
        if client == "gemini":
            return code == 2 and data["decision"] == "deny"
        if client == "cursor":
            return code == 2 and data["permission"] == "deny"
        decision = data["hookSpecificOutput"]["permissionDecision"]
        return code == 0 and decision == ("deny" if client == "codex" else "ask")

    def test_a_broken_policy_file_denies_for_every_client(self):
        self.append("policy.py", "raise RuntimeError('broken on purpose')")
        for client, event in BEFORE.items():
            with self.subTest(client):
                code, text, _ = self.run_hook(client, event)
                self.assertTrue(self.denied(client, code, text), (code, text))

    def test_a_crashing_adapter_denies(self):
        for client in ("gemini", "cursor", "codex"):
            self.append(f"client_{client}.py", "def normalize(raw):\n    raise ValueError('broken on purpose')")
            with self.subTest(client):
                code, text, _ = self.run_hook(client, BEFORE[client])
                self.assertTrue(self.denied(client, code, text), (code, text))

    def test_a_crash_on_other_events_says_nothing(self):
        self.append("policy.py", "raise RuntimeError('broken on purpose')")
        code, text, _ = self.run_hook("gemini", {"hook_event_name": "AfterAgent", "prompt_response": "hi"})
        self.assertEqual((code, json.loads(text)), (0, {}))

    def test_a_check_past_the_deadline_denies_before_the_client_gives_up(self):
        self.append("policy.py", "_slow = classify\ndef classify(*a, **k):\n    import time\n    time.sleep(20)\n"
                                 "    return _slow(*a, **k)")
        self.append("client_gemini.py", "DEADLINE = 1")
        code, text, took = self.run_hook("gemini", BEFORE["gemini"])
        self.assertTrue(self.denied("gemini", code, text), (code, text))
        self.assertIn("took longer", json.loads(text)["reason"])
        self.assertLess(took, 15)

    def test_a_normal_answer_is_not_doubled_by_the_deadline(self):
        self.append("client_gemini.py", "DEADLINE = 30")
        code, text, _ = self.run_hook("gemini", BEFORE["gemini"])
        self.assertEqual(code, 0)
        self.assertEqual(len(text.splitlines()), 1, text)


if __name__ == "__main__":
    unittest.main()
