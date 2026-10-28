"""Start Claude Code on a DeepSeek model, with this playbook's hooks working as usual (Q27A).

  python tools/deepseek.py [claude arguments...]

DeepSeek serves an Anthropic-compatible endpoint, so Claude Code runs unchanged and every hook
(tier-2 asks, the report check) still applies. The settings below live only in the child process:
your normal Claude Code keeps using Anthropic. The key comes from DEEPSEEK_API_KEY or is typed in
(hidden); it is never written anywhere.

Before using it, know that DeepSeek processes and stores data in the People's Republic of China
and may use it for training (its privacy policy); use it only on projects that may go there.
Anthropic does not support Claude Code on non-Claude models, so a Claude Code update may break it.
"""

from __future__ import annotations

import getpass
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_URL = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-flash"  # Q27A: the cheaper model; DeepSeek maps every Claude slot to it here
NOTICE = ("DeepSeek: data you or the agent send is processed in the PRC. "
          "Use this only on projects that may go there.")


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    # The desktop app bundles the CLI under %APPDATA%\Claude\claude-code\<version>\<hash>\claude.exe
    bundled = glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "*", "claude.exe"))
    return max(bundled, key=os.path.getmtime) if bundled else None


def child_env(key: str, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.pop("ANTHROPIC_API_KEY", None)  # would compete with the DeepSeek token
    env.update({
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
        "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
    })
    return env


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    claude = find_claude()
    if not claude:
        print("Claude Code was not found. Install it, or open the Claude desktop app once.", file=sys.stderr)
        return 1
    if "--permission-mode" in argv or "--dangerously-skip-permissions" in argv:
        print("Run it in the default mode: the auto and bypass modes depend on a Claude model.", file=sys.stderr)
        return 2
    print(NOTICE)
    key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek API key (hidden): ")
    if not key.strip():
        print("No key given; nothing started.", file=sys.stderr)
        return 1
    return subprocess.run([claude, *argv], env=child_env(key.strip())).returncode


if __name__ == "__main__":
    sys.exit(main())
