"""Shared fixes found while adding other agents: two BOMs, Cursor importing our Claude hooks, line endings."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import mkdtemp  # noqa: E402
import install  # noqa: E402

HOOK = ROOT / "src" / "hooks" / "playbook_hook.py"


def run_hook(payload: bytes, *args):
    out = subprocess.run([sys.executable, str(HOOK), *args], input=payload, capture_output=True, timeout=60,
                         env={**os.environ, "AGENT_PLAYBOOK_HOME": mkdtemp()})
    return out.returncode, out.stdout.decode().strip()


class SharedFixes(unittest.TestCase):
    def test_two_boms_are_read(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": mkdtemp(), "session_id": "s",
                 "tool_input": {"command": "git push --force origin main"}}
        code, text = run_hook(b"\xef\xbb\xbf\xef\xbb\xbf" + json.dumps(event).encode())
        self.assertEqual(json.loads(text)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_cursor_importing_the_claude_hooks_gets_no_decision(self):
        event = {"hook_event_name": "Stop", "cursor_version": "3.27", "workspace_roots": [mkdtemp()],
                 "conversation_id": "c", "status": "completed"}
        self.assertEqual(run_hook(json.dumps(event).encode()), (0, "{}"))
        push = {"hook_event_name": "PreToolUse", "tool_name": "Shell", "workspace_roots": [mkdtemp()],
                "tool_input": {"command": "git push --force origin main"}}
        self.assertEqual(run_hook(json.dumps(push).encode()), (0, "{}"))

    def test_install_keeps_each_files_line_endings(self):
        home = Path(mkdtemp())
        lf, crlf, new = home / "lf.toml", home / "crlf.toml", home / "new.md"
        lf.write_bytes(b"a = 1\nb = 2\n")
        crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
        install.apply({lf: "a = 1\nb = 2\nc = 3\n", crlf: "a = 1\nb = 2\nc = 3\n", new: "x\ny\n"})
        self.assertEqual(lf.read_bytes(), b"a = 1\nb = 2\nc = 3\n")
        self.assertEqual(crlf.read_bytes(), b"a = 1\r\nb = 2\r\nc = 3\r\n")
        self.assertEqual(new.read_text(encoding="utf-8"), "x\ny\n")

    def test_a_plan_that_already_has_crlf_is_not_doubled(self):
        path = Path(mkdtemp()) / "c.toml"
        path.write_bytes(b"a = 1\r\n")
        install.apply({path: "a = 1\r\nb = 2\r\n"})
        self.assertEqual(path.read_bytes(), b"a = 1\r\nb = 2\r\n")


if __name__ == "__main__":
    unittest.main()
