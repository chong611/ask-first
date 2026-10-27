"""Install agent-playbook into a project or into ~/.claude, showing a diff first.

  python3 tools/install.py project PATH        show what would change in the repo at PATH
                                               (hooks go to .claude/settings.local.json, this machine only)
  ... project PATH --cloud                     hooks go to the committed settings.json instead (cloud sessions)
  python3 tools/install.py global              show what would change in ~/.claude
  ... global --profile lang-zh-CN --profile advisor   add optional profiles (kept on later updates;
                                               --no-profiles removes them)
  ... --yes                                    write it (after you read the diff; DECISIONS Q20)
  python3 tools/install.py check PATH          compare the core in PATH with the global core (Q33)
  python3 tools/install.py uninstall global     show what removing it from ~/.claude would change
  python3 tools/install.py uninstall PATH       the same for the repo at PATH (Q133)
  ... --yes                                    remove it; ... --purge also removes logs, state and
                                               the lessons inbox in ~/.claude/agent-playbook

Existing files are never replaced blindly:
  - CLAUDE.md / AGENTS.md that are not ours get a marked block added or updated,
    and the rest of the file is kept.
  - settings.json is merged: our hook entries (command contains "agent-playbook")
    are replaced, everything else is kept; ask rules are added, never removed.
  - Every file that changes is backed up next to itself as <name>.bak-agent-playbook.
A personal profile belongs in ~/.claude/rules/owner.md, the user's own file, which
Claude Code loads by itself; the installer never edits it. The first global install
moves an old src/profiles/owner.md there once (DECISIONS Q155).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

CORE_BLOCK = re.compile(r"<!-- agent-playbook:core[^>]*-->.*?<!-- /agent-playbook:core -->\n?", re.S)
# An older block whose end marker was lost: up to the visible end line, else to end of file.
OPEN_BLOCK = re.compile(r"<!-- agent-playbook:core[^>]*-->.*?(?:End of agent-playbook core\.\n?|\Z)", re.S)
IMPORT_BEGIN, IMPORT_END = "<!-- agent-playbook:import -->", "<!-- /agent-playbook:import -->"


def merge_block(existing: str | None, block: str) -> str:
    """Put the core block into an instruction file, replacing an older block if present."""
    if existing is None:
        return build.HEADER + block
    if CORE_BLOCK.search(existing):
        return CORE_BLOCK.sub(lambda _: block, existing, count=1)
    if OPEN_BLOCK.search(existing):
        return OPEN_BLOCK.sub(lambda _: block, existing, count=1)
    return existing.rstrip("\n") + "\n\n" + block


def merge_import(existing: str | None) -> str:
    """CLAUDE.md must import AGENTS.md, or Claude Code never reads it."""
    if existing is None:
        return build.HEADER + "@AGENTS.md\n"
    if any(line.strip() == "@AGENTS.md" for line in existing.splitlines()):
        return existing
    return existing.rstrip("\n") + f"\n\n{IMPORT_BEGIN}\n@AGENTS.md\n{IMPORT_END}\n"


OUR_HOOK = re.compile(r"(hooks/agent-playbook|agent-playbook/hooks)/playbook_hook\.py\b")


def is_ours(hook: dict) -> bool:
    """Our entries run playbook_hook.py from a project or global install; nothing else matches."""
    words = [str(hook.get("command", ""))] + [str(a) for a in hook.get("args") or []]
    return any(OUR_HOOK.search(w.replace("\\", "/")) for w in words)


def merge_settings(existing: str | None, fragment: str) -> str:
    """Replace only our own hook items; keep every other hook, group and setting."""
    try:
        current = json.loads(existing) if existing and existing.strip() else {}
    except ValueError as exc:
        raise SystemExit(f"settings.json is not plain JSON ({exc}); nothing was written. "
                         "Remove comments or trailing commas and retry.")
    if not isinstance(current, dict):
        raise SystemExit("settings.json is not a JSON object; nothing was written.")
    ours = json.loads(fragment)
    if not isinstance(current.get("hooks"), dict):
        current["hooks"] = {}
    hooks = current["hooks"]
    for event, entries in ours["hooks"].items():
        kept = []
        for group in hooks.get(event) or []:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            items = [h for h in group.get("hooks") or [] if not (isinstance(h, dict) and is_ours(h))]
            if items:
                kept.append({**group, "hooks": items})
        hooks[event] = kept + entries
    if not isinstance(current.get("permissions"), dict):
        current["permissions"] = {}
    ask = current["permissions"].get("ask") if isinstance(current["permissions"].get("ask"), list) else []
    current["permissions"]["ask"] = ask + [r for r in ours["permissions"]["ask"] if r not in ask]
    return json.dumps(current, indent=2, ensure_ascii=False) + "\n"


def unmerge_settings(existing: str | None, fragment: str, original: str | None) -> str | None:
    """Remove our hook items, and the ask rules we added (those not in the original file)."""
    if not existing or not existing.strip():
        return existing
    try:
        current = json.loads(existing)
        before = json.loads(original) if original and original.strip() else {}
    except ValueError as exc:
        raise SystemExit(f"settings.json is not plain JSON ({exc}); nothing was written.")
    if not isinstance(current, dict):
        return existing
    hooks = current.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            kept = []
            for group in hooks[event] or []:
                if not isinstance(group, dict):
                    kept.append(group)
                    continue
                items = [h for h in group.get("hooks") or [] if not (isinstance(h, dict) and is_ours(h))]
                if items:
                    kept.append({**group, "hooks": items})
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            del current["hooks"]
    perms = current.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("ask"), list):
        had = (before.get("permissions") or {}).get("ask") or [] if isinstance(before, dict) else []
        ours = set(json.loads(fragment)["permissions"]["ask"]) - set(had)
        perms["ask"] = [r for r in perms["ask"] if r not in ours]
        if not perms["ask"]:
            del perms["ask"]
        if not perms:
            del current["permissions"]
    return json.dumps(current, indent=2, ensure_ascii=False) + "\n"


def oldest_backup(path: Path) -> str | None:
    """The first backup the installer made, i.e. the file before agent-playbook touched it."""
    backups = sorted(path.parent.glob(path.name + ".bak-agent-playbook-*"))
    return read(backups[0]) if backups else None


def plan_uninstall(home: Path, repo: Path | None, purge: bool = False) -> tuple[dict[Path, str | None], list[str]]:
    """What to remove (None) or rewrite. Files we installed are removed only if unchanged since."""
    out = build.outputs()
    plan: dict[Path, str | None] = {}
    notes: list[str] = []
    prefix, base = ("global/", home / ".claude") if repo is None else ("project/", repo)
    for rel, text in out.items():
        if not rel.startswith(prefix) or rel.endswith("settings.fragment.json"):
            continue
        path = base / rel[len(prefix):]
        if rel.endswith("rules/agent-playbook.md"):
            current = read(path)
            if current is not None:
                plan[path] = None  # the owner profile is appended at install, so do not compare
            continue
        current = read(path)
        if current is None:
            continue
        if current == text:
            plan[path] = None
        else:
            notes.append(f"kept {path}: it changed since the install; remove it yourself if it is ours")
    if repo is None:
        fragment = out["global/settings.fragment.json"]
        settings_files = [home / ".claude" / "settings.json"]
        installed = home / ".claude" / "agent-playbook" / "installed.json"
        if installed.exists():
            plan[installed] = None
        if (home / ".claude" / "rules" / "owner.md").exists():
            notes.append(f"kept {home / '.claude' / 'rules' / 'owner.md'}: it is your own profile")
        if purge:
            state = home / ".claude" / "agent-playbook"
            for path in sorted(state.rglob("*")) if state.exists() else []:
                if path.is_file():
                    plan[path] = None
        else:
            notes.append(f"kept {home / '.claude' / 'agent-playbook'} (logs, state, lessons inbox); --purge removes it")
    else:
        fragment = out["project/.claude/settings.fragment.json"]
        settings_files = [repo / ".claude" / "settings.json", repo / LOCAL_SETTINGS]
        agents, claude = repo / "AGENTS.md", repo / "CLAUDE.md"
        text = read(agents)
        if text is not None and CORE_BLOCK.search(text):
            rest = CORE_BLOCK.sub("", text).replace(build.HEADER, "").strip()
            plan[agents] = None if not rest else rest + "\n"
        text = read(claude)
        if text is not None:
            if text.replace(build.HEADER, "").strip() == "@AGENTS.md":
                plan[claude] = None
            elif IMPORT_BEGIN in text:
                kept = re.sub(re.escape(IMPORT_BEGIN) + r".*?" + re.escape(IMPORT_END) + r"\n?", "", text, flags=re.S)
                plan[claude] = kept.rstrip("\n") + "\n"
    for path in settings_files:
        current = read(path)
        if current is None:
            continue
        new = unmerge_settings(current, fragment, oldest_backup(path))
        if new is not None and json.loads(new) == {}:
            new = None if path.name == "settings.local.json" else new
        plan[path] = new
    backups = sorted(p for d in {p.parent for p in plan} if d.exists() for p in d.glob("*.bak-agent-playbook-*"))
    if backups:
        notes.append(f"kept {len(backups)} backup file(s) (*.bak-agent-playbook-*); delete them yourself when sure")
    return plan, notes


OLD_OWNER = build.SRC / "profiles" / "owner.md"  # where the personal profile lived before Q155
PROFILE_LINE = re.compile(r"^# profile: ([\w-]+)\s*$", re.M)


def installed_profiles(home: Path) -> list[str]:
    """The optional profiles chosen at the last install, so an update keeps them."""
    state = home / ".claude" / "agent-playbook" / "installed.json"
    if (text := read(state)) is not None:
        try:
            return [p for p in json.loads(text).get("profiles", []) if p in build.PUBLIC_PROFILES]
        except (ValueError, AttributeError):
            pass
    text = read(home / ".claude" / "rules" / "agent-playbook.md") or ""
    return [p for p in PROFILE_LINE.findall(text) if p in build.PUBLIC_PROFILES]


def commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=build.ROOT, capture_output=True,
                             text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


LOCAL_SETTINGS = ".claude/settings.local.json"


def plan_project(repo: Path, local: bool = False) -> dict[Path, str]:
    """local=True: hooks go to settings.local.json (this machine only, never committed) and
    run with this Python's absolute path, so they work where "python3" is missing (Windows).
    local=False (--cloud): hooks go to the committed settings.json with "python3", for
    cloud sessions, which only see committed files."""
    out = build.outputs()
    plan = {}
    agents, claude = repo / "AGENTS.md", repo / "CLAUDE.md"
    plan[agents] = merge_block(read(agents), build.core())
    plan[claude] = merge_import(read(claude))
    fragment = out["project/.claude/settings.fragment.json"]
    if local:
        fragment = fragment.replace('"python3"', json.dumps(Path(sys.executable).as_posix()))
        settings = repo / LOCAL_SETTINGS
        gitignore = repo / ".gitignore"
        current = read(gitignore)
        if not any(l.strip() in (LOCAL_SETTINGS, "/" + LOCAL_SETTINGS) for l in (current or "").splitlines()):
            plan[gitignore] = (current or "").rstrip("\n") + ("\n" if current else "") + LOCAL_SETTINGS + "\n"
    else:
        settings = repo / ".claude" / "settings.json"
    plan[settings] = merge_settings(read(settings), fragment)
    for rel, text in out.items():
        if rel.startswith("project/.claude/") and not rel.endswith("settings.fragment.json"):
            plan[repo / rel[len("project/"):]] = text
    return plan


def plan_global(home: Path, profiles: list[str] | None = None) -> dict[Path, str]:
    out = build.outputs()
    claude = home / ".claude"
    profiles = installed_profiles(home) if profiles is None else profiles
    rules = out["global/rules/agent-playbook.md"] + "".join("\n" + build.profile(p) for p in profiles)
    plan = {claude / "rules" / "agent-playbook.md": rules}
    owner = claude / "rules" / "owner.md"
    if OLD_OWNER.exists() and not owner.exists():  # one-time move (Q155); afterwards the file is the user's
        plan[owner] = build.read(OLD_OWNER)
    plan[claude / "agent-playbook" / "installed.json"] = json.dumps(
        {"version": build.VERSION, "commit": commit(), "profiles": profiles}, indent=1) + "\n"
    settings = claude / "settings.json"
    fragment = (out["global/settings.fragment.json"]
                .replace("{HOME}", home.as_posix()).replace("{PYTHON}", Path(sys.executable).as_posix()))
    plan[settings] = merge_settings(read(settings), fragment)
    for rel, text in out.items():
        if rel.startswith("global/") and rel not in ("global/rules/agent-playbook.md", "global/settings.fragment.json"):
            plan[claude / rel[len("global/"):]] = text
    return plan


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")  # a BOM (Windows PowerShell 5.1) would hide "@AGENTS.md"
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise SystemExit(f"{path} is not UTF-8; nothing was written. Save it as UTF-8 and retry.")


def diff(plan: dict[Path, str]) -> list[str]:
    """Full diff for files that already exist (what you must review); one line per new file."""
    changed, created = [], []
    removed = []
    for path, new in plan.items():
        old = read(path)
        if old == new:
            continue
        if new is None:
            removed.append(f"  - remove    {path}\n")
            continue
        if old is None:
            created.append(f"  + new file  {path}  ({len(new.splitlines())} lines)\n")
        else:
            changed += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                            fromfile=f"{path} (current)", tofile=f"{path} (after install)")
    lines = []
    if changed:
        lines += ["=== Changes to files you already have (read these) ===\n"] + changed
    if created:
        lines += [f"=== New files ({len(created)}) ===\n"] + created
    if removed:
        lines += [f"=== Files to remove ({len(removed)}) ===\n"] + removed
    return lines


def apply(plan: dict[Path, str]) -> list[Path]:
    changed = []
    for path, new in plan.items():
        old = read(path)
        if old == new:
            continue
        if new is None:  # only files identical to what we installed, our rules file, or --purge state
            path.unlink()
            remove_empty_dirs(path.parent)
            changed.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if old is not None:  # timestamped, so an older backup (e.g. the original) is never overwritten
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.bak-agent-playbook-{stamp}")
            n = 1
            while backup.exists():
                backup = path.with_name(f"{path.name}.bak-agent-playbook-{stamp}-{n}")
                n += 1
            shutil.copy2(path, backup)
            trim_backups(path)
        path.write_text(new, encoding="utf-8")
        changed.append(path)
    return changed


def remove_empty_dirs(folder: Path) -> None:
    """Remove folders we emptied (a skill folder, hooks/agent-playbook), never ~/.claude itself."""
    while folder.name not in (".claude", "") and folder.exists() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def trim_backups(path: Path, keep_recent: int = 2) -> None:
    """Keep the oldest backup (the original file) and the newest few; delete the rest."""
    backups = sorted(path.parent.glob(path.name + ".bak-agent-playbook-*"))
    for old in backups[1:-keep_recent] if len(backups) > keep_recent + 1 else []:
        old.unlink()


def core_of(text: str | None) -> str | None:
    m = CORE_BLOCK.search(text or "")
    return m.group(0) if m else None


def check(repo: Path, home: Path) -> int:
    project = core_of(read(repo / "AGENTS.md"))
    global_ = core_of(read(home / ".claude" / "rules" / "agent-playbook.md"))
    if project is None or global_ is None:
        print("core not installed in " + ("the project" if project is None else "~/.claude"))
        return 1
    if project != global_:
        print("WARNING: project and global core differ; re-run install for both")
        return 1
    print("project and global core are identical")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("target", choices=["project", "global", "check", "uninstall"])
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--yes", action="store_true", help="write the changes shown in the diff")
    parser.add_argument("--cloud", action="store_true",
                        help="project: write hooks to the committed settings.json (python3) for cloud sessions")
    parser.add_argument("--purge", action="store_true", help="uninstall: also remove logs, state and the lessons inbox")
    parser.add_argument("--profile", action="append", choices=build.PUBLIC_PROFILES,
                        help="global: add an optional profile (repeatable); later updates keep it")
    parser.add_argument("--no-profiles", action="store_true", help="global: remove the optional profiles")
    parser.add_argument("--home", default=os.path.expanduser("~"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:  # a non-UTF-8 console (common on Windows) must not crash on Chinese text in a diff
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    home, repo = Path(args.home), Path(args.path).resolve()
    if build.stale():
        print("dist/ is stale; run python3 tools/build.py first", file=sys.stderr)
        return 1
    if args.target == "check":
        return check(repo, home)
    notes: list[str] = []
    if args.target == "uninstall":
        plan, notes = plan_uninstall(home, None if args.path == "global" else repo, args.purge)
    else:
        profiles = [] if args.no_profiles else args.profile
        plan = plan_project(repo, local=not args.cloud) if args.target == "project" else plan_global(home, profiles)
    print(f"agent-playbook {build.VERSION} ({commit() or 'no git'})")
    lines = diff(plan)
    if not lines:
        print("nothing to remove" if args.target == "uninstall" else "already up to date")
        return 0
    sys.stdout.writelines(lines)
    if not args.yes:
        sys.stdout.writelines(f"{n}\n" for n in notes)
        print("\nNothing written. Read the diff above, then re-run with --yes to apply it.")
        return 0
    for path in apply(plan):
        print(f"{'removed' if plan[path] is None else 'wrote'} {path}")
    sys.stdout.writelines(f"{n}\n" for n in notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
