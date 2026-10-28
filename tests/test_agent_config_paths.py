"""Other agents' instruction, hook and settings files are protected like Claude Code's (rule 2.3)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "hooks"))
import policy  # noqa: E402

PROTECTED = [
    "/home/u/.codex/hooks.json", "/home/u/.codex/config.toml", "/home/u/.codex/rules/agent-playbook.rules",
    "/home/u/.codex/agent-playbook/hooks/policy.py", "/home/u/.codex/AGENTS.override.md",
    "/home/u/.agents/skills/pb-status/SKILL.md",
    "/home/u/.cursor/hooks.json", "/repo/.cursor/rules/x.mdc", "/home/u/.cursor/plugins/local/p/plugin.json",
    "/home/u/.cursor/cli-config.json", "/repo/.cursor/cli.json", "/home/u/.cursor/permissions.json",
    "/repo/.cursor/mcp.json", "/home/u/.cursor/hooks/fmt.ps1", "/repo/.cursor/agents/a.md", "/repo/.cursor/commands/c.md",
    "/home/u/.cursor/skills-cursor/s/SKILL.md", "/repo/.cursorrules", "/repo/.cursorignore",
    "C:/Users/u/AppData/Roaming/Cursor/User/settings.json",
    "/home/u/.gemini/settings.json", "/home/u/.gemini/GEMINI.md", "/repo/GEMINI.md",
    "/home/u/.gemini/policies/agent-playbook.toml", "/home/u/.gemini/extensions/agent-playbook/hooks/hooks.json",
    "C:/ProgramData/gemini-cli/settings.json",
]
ORDINARY = ["/repo/src/app.py", "/repo/docs/guide.txt", "/home/u/.codex/sessions/log.txt",
            "/home/u/.cursor/projects/x/state.txt", "/home/u/.gemini/tmp/scratch.txt"]


class OtherAgentConfigPaths(unittest.TestCase):
    def test_writing_another_agents_instructions_asks(self):
        for path in PROTECTED:
            with self.subTest(path):
                d = policy.classify("Write", {"file_path": path, "content": "x"}, policy.Context(cwd="/repo"))
                self.assertEqual(d[0], policy.ASK, d)

    def test_ordinary_files_in_those_folders_do_not_ask(self):
        for path in ORDINARY:
            with self.subTest(path):
                d = policy.classify("Write", {"file_path": path, "content": "x"}, policy.Context(cwd="/repo"))
                self.assertEqual(d[0], policy.ALLOW, d)

    def test_linking_over_the_settings_folder_itself_asks(self):
        for folder in (".codex", ".cursor", ".gemini", ".agents"):
            with self.subTest(folder):
                d = policy.classify("Bash", {"command": f"ln -s /tmp/x ~/{folder}"}, policy.Context(cwd="/repo"))
                self.assertEqual(d[0], policy.ASK, d)
        d = policy.classify("Bash", {"command": "ln -s /tmp/x ~/.notes"}, policy.Context(cwd="/repo"))
        self.assertEqual(d[0], policy.ALLOW, d)


if __name__ == "__main__":
    unittest.main()
