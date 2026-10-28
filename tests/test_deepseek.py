"""tools/deepseek.py: the DeepSeek settings stay in the child process and the key is never shown (Q27A)."""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import deepseek  # noqa: E402


class DeepSeekLauncher(unittest.TestCase):
    def test_child_env_points_every_slot_at_deepseek_and_drops_the_anthropic_key(self):
        env = deepseek.child_env("k-test", {"ANTHROPIC_API_KEY": "other", "PATH": "p"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], deepseek.BASE_URL)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "k-test")
        for slot in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(env[slot], deepseek.MODEL)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["PATH"], "p")

    def test_parent_environment_is_not_changed(self):
        before = dict(os.environ)
        deepseek.child_env("k-test")
        self.assertEqual(dict(os.environ), before)

    def test_key_is_passed_to_the_child_only_and_never_printed(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(deepseek, "find_claude", return_value="claude"), \
                mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k-secret-123"}), \
                mock.patch.object(deepseek.subprocess, "run") as run, redirect_stdout(out), redirect_stderr(err):
            run.return_value.returncode = 0
            self.assertEqual(deepseek.main(["-c"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["claude", "-c"])
        self.assertEqual(kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], "k-secret-123")
        self.assertNotIn("k-secret-123", out.getvalue() + err.getvalue())
        self.assertIn("PRC", out.getvalue())

    def test_refuses_modes_that_need_a_claude_model(self):
        with mock.patch.object(deepseek, "find_claude", return_value="claude"), \
                mock.patch.object(deepseek.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(deepseek.main(["--dangerously-skip-permissions"]), 2)
        run.assert_not_called()

    def test_no_claude_no_start(self):
        with mock.patch.object(deepseek, "find_claude", return_value=None), \
                mock.patch.object(deepseek.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(deepseek.main([]), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
