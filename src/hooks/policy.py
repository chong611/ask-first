"""Tier-2 policy (core rule 2.3) and secret exposure (rule 5.1) as pure functions.

classify(tool_name, tool_input, ctx) -> (decision, reason); decision is "ask" or "allow".
"ask" makes Claude Code show a permission prompt (even in auto mode); the user's
answer is the approval rule 2.3 requires. Nothing here approves on the user's behalf.

Coverage is partial by design (docs/ENFORCEMENT.md). When the policy cannot tell
whether something is safe (a deletion whose targets it cannot resolve, an inline
script that deletes), it asks. What it cannot see at all (a script file that edits
a database) is left to the rule text and the data snapshot.
"""

from __future__ import annotations

import fnmatch
import functools
import glob
import itertools
import os
import re
import shlex
import stat
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_patterns  # noqa: E402

ALLOW = "allow"
ASK = "ask"


@dataclass
class Context:
    cwd: str = "."
    allowed_branches: tuple[str, ...] = ()
    data_paths: tuple[str, ...] = ()           # globs relative to cwd: always user data
    ask_tools: tuple[str, ...] = ()            # tool-name globs, e.g. "mcp__Google_Drive__*"
    tracked: set[str] | None = None            # git-tracked paths; None = unknown
    dirty: set[str] | None = None              # tracked paths with uncommitted changes; None = ask git
    created: set[str] = field(default_factory=set)  # absolute paths the agent created this session
    current_branch: str | None = None
    temp_vars: frozenset = frozenset()          # shell variables this command set from mktemp, once
    shell: str = ""                             # "PowerShell": there cp, mv and tee are cmdlet aliases


# --- shell parsing ---------------------------------------------------------------


@functools.lru_cache(maxsize=4096)
def base_name(word: str) -> str:
    """Path(word).name, cached: it runs for nearly every word of every command."""
    return Path(word).name

SEPARATORS = {";", ";;", "&&", "||", "|", "&", "|&", "(", ")", "{", "}", "!"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "case", "esac", "time", "!"}
WRAPPERS = {"sudo", "env", "nohup", "command", "builtin", "exec", "nice", "timeout", "stdbuf", "xargs",
            "doas", "chronic", "ionice"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
# powershell.exe and pwsh options that take a value (-ExecutionPolicy Bypass, -WindowStyle Hidden, ...).
PWSH_VALUE_FLAGS = ("-ex", "-ep", "-w", "-v", "-i", "-o", "-con", "-wd", "-ps", "-settings", "-cus")
PLAIN, MORE, PATHS = "plain", "more", "paths"  # how split_commands reads a command
SUBEXPR = "(…)"  # stands for an argument in parentheses; tokenize never yields it, as "(" is split off
SUBST = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`")


def logical_lines(command: str) -> list[str]:
    """Split on newlines that are not inside quotes and not escaped."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(c)
                i += 1
                c = command[i]
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < len(command) and command[i + 1] == "\n":
            i += 2
            continue
        elif c == "\n":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


BRACED_VAR = re.compile(r"\$\{(\w+(?::\w+)?)\}(?!\w)")

# PowerShell path expressions, read as the path they make. Only the write check (rule 2.3, own instructions)
# uses them (split_commands(..., mode=PATHS)); every other check keeps the plain words, where an expression
# it cannot read is a target known only at run time, which asks.
JOIN_PATH = re.compile(r"\(\s*Join-Path\s([^()]*)\)", re.I)
PATH_COMBINE = re.compile(r"\[(?:system\.)?io\.path\]::Combine\s*\(([^()]*)\)", re.I)
DOTNET_CALL = re.compile(r"(\[(?:system\.)?io\.[\w.]+\]::\w+|New-Object\s+(?:-TypeName\s+)?(?:System\.)?IO\.\w+)\s*\(",
                         re.I)
# ("x"), ($p), @('a','b'), ($HOME + '/x'): a value. A bare word in parentheses, (pwd), runs a command: kept.
VALUE = r"(?:\"[^\"]*\"|'[^']*'|\$(?=[\w:])|~)(?:\"[^\"]*\"|'[^']*'|[^\s()'\";|&,+])*"
PAREN_VALUES = re.compile(rf"(?<![^\s,=(&])@?\(\s*({VALUE}(?:\s*[,+]\s*{VALUE})*)\s*\)(?=[\s,;|)]|$)")
# (Resolve-Path x), (Get-Item x).FullName: the path x itself.
PATH_OF = re.compile(r"\(\s*(?:Resolve-Path|rvpa|Convert-Path|cvpa|Get-Item|gi|Get-ChildItem|gci|ls|dir)\s+"
                     r"(?:-(?:Literal)?Path\s+)?([^\s()|;&]+)\s*\)(?:\.(?:Path|FullName|ProviderPath))?(?![.\[\w])", re.I)


def join_path(m: re.Match) -> str:
    """(Join-Path a b), (Join-Path -ChildPath b -Path a): the one path they make."""
    words, base, children, i = m.group(1).split(), [], [], 0
    if any(w.count("'") % 2 or w.count('"') % 2 for w in words):
        return m.group(0)  # a quoted part with a space: leave it
    while i < len(words):
        w = words[i].lower()
        if w.startswith("-"):
            if i + 1 == len(words) or not any(n.startswith(w[1:]) for n in ("path", "childpath", "additionalchildpath")):
                return m.group(0)  # -Resolve and the like: leave it
            (base if "path".startswith(w[1:]) else children).append(words[i + 1])
            i += 2
            continue
        (children if base else base).append(words[i])
        i += 1
    return "/".join(base + children) if base and children else m.group(0)


def path_combine(m: re.Match) -> str:
    """[IO.Path]::Combine(a, b): a later absolute part replaces what comes before it, as in .NET."""
    out: list[str] = []
    for p in (p.strip() for p in m.group(1).split(",")):
        if re.match(r"^['\"]?([A-Za-z]:)?/", p):
            out = []
        out.append(p)
    return "/".join(out)


def concat(m: re.Match) -> str:
    """The text inside ("x"), @('a', 'b') or ($HOME + '/x'): items joined by ',', parts of a sum joined."""
    return ",".join("".join(q.strip() for q in item.split("+")) for item in m.group(1).split(","))


def tokenize(line: str, paths: bool = False) -> list[str]:
    if paths:
        line = BRACED_VAR.sub(r"$\1", line)  # ${HOME}, ${env:USERPROFILE}: the braces would split the word
        for _ in range(5):  # innermost first: (Join-Path (Join-Path $HOME .claude) x)
            before = line
            line = JOIN_PATH.sub(join_path, line)  # (Join-Path $HOME x) is one path, not a command of its own
            line = PATH_OF.sub(r"\1", line)
            line = PATH_COMBINE.sub(path_combine, line)
            line = PAREN_VALUES.sub(concat, line)
            if line == before:
                break
        line = DOTNET_CALL.sub(r"\1 ", line)  # [IO.File]::WriteAllText(p, s): p and s become its arguments
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|(){}<>!")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return line.split()


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def split_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Cut heredoc bodies out of a command: (command without bodies, [(line before <<, body)])."""
    lines, kept, bodies, i = command.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append((line[:m.start()], "\n".join(body)))
    return "\n".join(kept), bodies


def split_commands(command: str, depth: int = 0, mode: str = PLAIN) -> list[list[str]]:
    """Simple commands (token lists), including those inside $(), backticks, bash -c, eval and
    heredocs. A heredoc fed to a shell is parsed as commands; fed to anything else (python3 -,
    cat > file) it becomes that command's last argument, so inline-code checks still see it.
    mode: PLAIN reads a command as before Q102A; MORE also unwraps $x = cmd, . cmd, iex, powershell "...";
    PATHS also reads PowerShell path expressions (see JOIN_PATH), for the write check only."""
    if depth > 4:
        return []
    out: list[list[str]] = []
    for m in SUBST.finditer(command):
        out += split_commands(m.group(1) or m.group(2) or "", depth + 1, mode)
    command, bodies = split_heredocs(command)
    for head, body in bodies:
        cmds = split_commands(head, depth + 1, mode)
        if cmds and base_name(cmds[-1][0]) in SHELLS:
            out += split_commands(body, depth + 1, mode)
        elif cmds:
            out.append(cmds[-1] + [body])
    for line in logical_lines(command):
        current: list[str] = []
        for tok in tokenize(line, mode == PATHS):
            if tok in SEPARATORS or (tok and set(tok) <= set(";&|(){}!")):
                if current and tok == "(" and mode != PLAIN:
                    current.append(SUBEXPR)  # rm @('..'), rm (pwd): an argument made at run time
                if current:
                    out += expand(current, depth, mode)
                current = []
            else:
                current.append(tok)
        if current:
            out += expand(current, depth, mode)
    return out


def expand(cmd: list[str], depth: int, mode: str = PLAIN) -> list[list[str]]:
    cmd = strip_prefix(cmd, mode != PLAIN)
    if cmd and cmd[0] == SUBEXPR:  # T=$(mktemp), if (x): no command before the parentheses
        cmd = cmd[1:]
    if not cmd:
        return []
    name = base_name(cmd[0])
    if name in SHELLS:
        args = cmd[1:]
        for i, a in enumerate(args):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                rest = [x for x in args[i + 1:] if x != "--"]
                return split_commands(rest[0], depth + 1, mode) if rest else []
        return [cmd]
    if name == "eval":
        return split_commands(" ".join(cmd[1:]), depth + 1, mode)
    lname = name.lower()
    if lname in ("cmd", "cmd.exe"):
        for i, a in enumerate(cmd[1:], 1):
            if a.lower() in ("/c", "/k"):
                return split_commands(" ".join(cmd[i + 1:]).replace("^", ""), depth + 1, mode)
        return [cmd]
    if lname in ("powershell", "powershell.exe", "pwsh", "pwsh.exe") and mode == PLAIN:
        for i, a in enumerate(cmd[1:], 1):
            if a.lower() in ("-c", "-command"):
                return split_commands(" ".join(cmd[i + 1:]).replace("\\", "/"), depth + 1, mode)
        return [cmd]
    if lname in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        out = [cmd]  # itself too: powershell "..." > ~/.claude/CLAUDE.md
        for i, a in enumerate(cmd[1:], 1):  # as before: the first exact -c or -Command
            if a.lower() in ("-c", "-command"):
                out += split_commands(" ".join(cmd[i + 1:]).replace("\\", "/"), depth + 1, mode)
                break
        start = pwsh_command_start(cmd)
        if start is not None:
            seen = {tuple(c) for c in out}
            out += [c for c in split_commands(" ".join(cmd[start:]).replace("\\", "/"), depth + 1, mode)
                    if tuple(c) not in seen]
        return out
    if lname in ("invoke-expression", "iex") and mode != PLAIN:  # the command itself is kept too
        words = [a for a in cmd[1:] if not (len(a) > 1 and "-command".startswith(a.lower()))]  # -C, -Command
        return [cmd] + split_commands(" ".join(words), depth + 1, mode) if words else [cmd]
    if name == "for":  # "for x in a b" carries no command of its own
        return []
    return [cmd]


def pwsh_command_start(cmd: list[str]) -> int | None:
    """Where powershell's command text starts, read the way Windows PowerShell 5.1 does: -c, -Com, /c, or
    the first word that is not an option. None for a script file or -EncodedCommand."""
    i = 1
    while i < len(cmd):
        a = cmd[i].lower()
        a = "-" + a[1:] if a.startswith("/") else a
        if len(a) > 1 and "-command".startswith(a):
            return i + 1
        if a.startswith(PWSH_VALUE_FLAGS):
            i += 2
        elif a.startswith("-"):
            if "-file".startswith(a) or a.startswith("-e"):
                return None
            i += 1
        else:
            return None if a.endswith(".ps1") else i
    return None


def strip_prefix(cmd: list[str], more: bool = False) -> list[str]:
    i = 0
    while i < len(cmd):
        word, base = cmd[i], base_name(cmd[i])
        if word in KEYWORDS:
            i += 1
        elif base in WRAPPERS and i + 1 < len(cmd):
            i += 1
            while i < len(cmd) and cmd[i].startswith("-"):  # wrapper options, e.g. timeout -s 9
                i += 1
            if base == "timeout" and i < len(cmd) and re.fullmatch(r"[\d.]+[smhd]?", cmd[i]):
                i += 1
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
            i += 1
        elif not more:
            break
        elif re.fullmatch(r"\$[\w:]+", word) and i + 1 < len(cmd) and cmd[i + 1] in ("=", "+=", "-="):
            i += 2  # PowerShell: $null = <command>
        elif (m := re.fullmatch(r"\$[\w:]+=(.+)", word)):
            cmd = cmd[:i] + [m.group(1)] + cmd[i + 1:]  # $x=git push
        elif word == "." and i + 1 < len(cmd):
            i += 1  # dot-sourcing: . Set-Content ...
        else:
            break
    return cmd[i:]


# --- paths and user data ---------------------------------------------------------

def norm(path: str) -> str:
    return path.replace("\\", "/")


def os_realpath(path: str) -> str:
    """os.path.realpath, or the path itself when Windows cannot resolve the text (WinError 1113)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def real(path: str) -> str:
    """The real path. os.path.realpath is slow on Windows, so it runs only for a drive (subst), a link,
    or a short 8.3 name (CLAUDE~1); a long command names thousands of paths that are none of these."""
    try:
        full = os.path.abspath(path)
    except (OSError, ValueError):  # a NUL in the text
        return path
    if norm(full).startswith("//"):
        return os_realpath(full)  # \\?\C:\..., \\localhost\C$\...: as os.path.realpath (a host may be slow)
    if os.name != "nt" and ".." in norm(path).split("/"):
        return os_realpath(path)  # POSIX resolves a link before the '..' after it; abspath does not
    drive, rest = os.path.splitdrive(full)
    here, _ = real_step(drive + os.sep, "")
    names = [n for n in re.split(r"[\\/]", rest) if n]
    for k, name in enumerate(names):
        here, exists = real_step(here, name)
        if not exists:  # nothing below a missing folder can be a link
            return os.path.join(here, *names[k + 1:])
    return here


@functools.lru_cache(maxsize=8192)  # cleared on each classify(); a folder is resolved once per command
def real_step(parent: str, name: str) -> tuple[str, bool]:
    """(parent/name resolved, whether it exists), one lstat for both."""
    here = os.path.join(parent, name) if name else parent
    try:
        st = os.lstat(here)
    except (OSError, ValueError):
        return here, False
    link = stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)  # 0x400: a junction
    return (os_realpath(here) if not name or link or re.search(r"~\d", name) else here), True


def expand_user(path: str) -> list[str]:
    """Every path a leading ~ may mean. Bash (Git Bash too) uses $HOME; PowerShell, and Python
    on Windows, use %USERPROFILE%. They can differ, so callers check each one."""
    if path != "~" and not path.startswith(("~/", "~\\")):
        return [os.path.expanduser(path)]
    homes = [os.environ.get("HOME"), os.environ.get("USERPROFILE"), os.path.expanduser("~")]
    return [h + path[1:] for h in dict.fromkeys(homes) if h and h != "~"] or [path]


def resolve_targets(args: list[str], ctx: Context) -> list[str] | None:
    """Expand ~, $VARS and globs. None if a target cannot be resolved (then: ask)."""
    targets = []
    for arg in [item for a in args for item in array_items(a)]:  # Remove-Item docs,build: both
        if arg.startswith("-"):
            continue
        if (m := re.fullmatch(r"\$\{?(\w+)\}?(/.*)?", arg)) and m.group(1) in ctx.temp_vars:
            continue                        # inside a temp folder this same command made
        if os.name != "nt" and (re.match(r"^[A-Za-z]:[\\/]", arg) or arg.startswith("\\\\")):
            return None                     # a Windows path seen from elsewhere: cannot check it
        for a in expand_user(arg):
            a = os.path.expandvars(a)
            if "$" in a:
                return None
            full = a if os.path.isabs(a) else os.path.join(ctx.cwd, a)
            if any(ch in a for ch in "*?["):
                try:
                    targets += glob.glob(full)
                except ValueError:  # a NUL in the text
                    return None
            else:
                targets.append(full)
    return targets


REGENERABLE = {"build", "dist", "out", "target", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
               ".ruff_cache", ".cache", ".next", ".nuxt", "coverage", "htmlcov", ".tox", ".parcel-cache"}


def relpath(full: str, ctx: Context) -> str | None:
    try:
        rel = os.path.relpath(real(full), real(ctx.cwd))
    except ValueError:
        return None
    return None if rel.startswith("..") else norm(rel)


def is_user_data(full: str, ctx: Context) -> bool:
    """True if deleting/overwriting `full` loses something git cannot restore and the agent did not create."""
    full = os.path.abspath(full)
    if real(full) in {real(c) for c in ctx.created}:
        return False
    rel = relpath(full, ctx)
    if rel is not None and any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*")
                               for g in ctx.data_paths):
        return True
    if not os.path.lexists(full):
        return False
    if rel is None:
        return True                                   # outside the project: never ours to lose
    parts = rel.split("/")
    if ".git" in parts:
        return True                                   # repository history itself
    if parts[-1] in REGENERABLE or any(p in REGENERABLE for p in parts):
        return False                                  # build output: regenerable
    if ctx.tracked is None:
        return True
    if os.path.isdir(full) and not os.path.islink(full):
        prefix = "" if rel == "." else rel + "/"
        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d != ".git" and d not in REGENERABLE]
            for f in files:
                r = norm(os.path.relpath(os.path.join(root, f), full))
                p = prefix + r
                if p not in ctx.tracked or p in dirty_paths(ctx):
                    return True
        return False
    return rel not in ctx.tracked or rel in dirty_paths(ctx)


# --- git ---------------------------------------------------------------------------

def decode(out: bytes) -> str:
    """Git output as text. Not os.fsdecode: on Windows it raises on a non-UTF-8 path in the repo."""
    return out.decode("utf-8", "surrogateescape")


def run_git(ctx: Context, *args: str) -> bytes | None:
    import subprocess
    try:
        out = subprocess.run(["git", *args], cwd=ctx.cwd, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return out.stdout if out.returncode == 0 else None


def dirty_paths(ctx: Context) -> set[str]:
    if ctx.dirty is None:
        out = run_git(ctx, "status", "--porcelain=v1", "-z", "--untracked-files=no")
        ctx.dirty = {decode(e[3:]) for e in (out or b"").split(b"\0") if len(e) > 3}
    return ctx.dirty


def current_branch(ctx: Context) -> str | None:
    out = run_git(ctx, "rev-parse", "--abbrev-ref", "HEAD")
    return decode(out).strip() if out else None


GIT_OPT_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def git_args(cmd: list[str]) -> list[str] | None:
    """Subcommand args with global options removed and -c alias.X=... expanded; None if not git."""
    if not cmd or base_name(cmd[0]) not in ("git", "git.exe"):
        return None
    args, i, aliases = cmd[1:], 0, {}
    while i < len(args):
        a = args[i]
        if a in GIT_OPT_WITH_ARG:
            if a == "-c" and i + 1 < len(args) and args[i + 1].startswith("alias."):
                key, _, val = args[i + 1][6:].partition("=")
                aliases[key] = val
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    rest = args[i:]
    if rest and rest[0] in aliases:
        val = aliases[rest[0]]
        if val.startswith("!"):
            return ["!shell-alias"]
        rest = tokenize(val) + rest[1:]
    return rest


def check_git(args: list[str], ctx: Context) -> str | None:
    if not args:
        return None
    sub, rest = args[0], args[1:]
    flags = [r for r in rest if r.startswith("-")]
    if sub == "!shell-alias":
        return "git shell alias runs arbitrary commands (tier-2: cannot verify)"
    if sub == "push":
        if any(f in ("-f", "--mirror", "--delete", "-d", "--prune") or f.startswith("--force") for f in flags):
            return "force-push or remote delete (tier-2: rewriting history)"
        positional = [r for r in rest if not r.startswith("-")]
        if any(r.startswith("+") or r.startswith(":") for r in positional[1:]):
            return "forced or deleting refspec (tier-2: rewriting history)"
        if ctx.allowed_branches:
            targets = [r.split(":")[-1] for r in positional[1:]]
            branch = ctx.current_branch or current_branch(ctx)
            targets = [branch if t == "HEAD" else t for t in targets] or [branch]
            bad = [t for t in targets if t not in ctx.allowed_branches]
            if bad:
                return f"push to a branch not given to you: {', '.join(str(b) for b in bad)} (tier-2)"
        return None
    if sub == "reset" and "--hard" in flags:
        return "git reset --hard discards uncommitted work (tier-2)"
    if sub == "clean" and any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", f) or f == "--force" for f in flags):
        return "git clean -f deletes untracked files (tier-2)"
    if sub == "checkout" and ("--" in rest or "." in rest or "-f" in flags or "--force" in flags) and "-b" not in flags:
        return "git checkout over paths or with --force discards uncommitted work (tier-2)"
    if sub == "restore" and not ("--staged" in flags and "--worktree" not in flags and "-W" not in flags):
        return "git restore discards uncommitted work (tier-2)"
    if sub == "switch" and any(f in ("-f", "--force", "--discard-changes") for f in flags):
        return "git switch --discard-changes discards uncommitted work (tier-2)"
    if sub == "stash" and rest[:1] in (["drop"], ["clear"]):
        return "git stash drop/clear discards work (tier-2)"
    if sub == "branch" and any(f in ("-D", "-d", "--delete") for f in flags):
        return "branch deletion (tier-2)"
    if sub == "worktree" and rest[:1] == ["remove"] and any(f in ("-f", "--force") for f in flags):
        return "git worktree remove --force discards work (tier-2)"
    if sub == "reflog" and rest[:1] in (["expire"], ["delete"]):
        return "git reflog expire drops recovery points (tier-2)"
    if sub == "gc" and any("prune" in f for f in flags):
        return "git gc --prune drops unreachable commits (tier-2)"
    if sub in ("rebase", "filter-branch", "filter-repo", "replace") or (sub == "commit" and "--amend" in flags):
        return f"git {sub} rewrites history (tier-2)"
    if sub == "update-ref" and "-d" in flags:
        return "git update-ref -d deletes a ref (tier-2)"
    return None


# --- other shell commands ----------------------------------------------------------------

GH_WRITE_VERBS = {"create", "merge", "close", "reopen", "edit", "delete", "comment", "review", "ready",
                  "upload", "set", "remove", "archive", "unarchive", "rename", "transfer", "fork", "run",
                  "enable", "disable", "lock", "unlock", "pin", "unpin", "sync", "add"}


def check_gh(cmd: list[str]) -> str | None:
    if base_name(cmd[0]) not in ("gh", "gh.exe"):
        return None
    args = cmd[1:]
    if args[:1] == ["api"]:
        method = "GET"
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1]
            elif a.startswith("--method="):
                method = a.split("=", 1)[1]
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:]
            elif a in ("-f", "-F", "--field", "--raw-field", "--input") and method == "GET":
                method = "POST"
        if method.upper() != "GET":
            return f"gh api {method.upper()} writes to GitHub (tier-2)"
        return None
    if len(args) >= 2 and args[1] in GH_WRITE_VERBS:
        return f"gh {args[0]} {args[1]} acts on GitHub (tier-2)"
    return None


LOCAL_URL = re.compile(r"^(https?://)?(localhost|127\.\d+\.\d+\.\d+|\[::1\]|0\.0\.0\.0)(:\d+)?(/|$)")


def check_http(cmd: list[str]) -> str | None:
    name = base_name(cmd[0])
    if name not in ("curl", "curl.exe", "wget", "http", "https", "xh"):
        return None
    args = cmd[1:]
    urls = [a for a in args if re.match(r"^(https?://|[\w.-]+\.[a-z]{2,}(/|$)|localhost|127\.)", a)]
    if urls and all(LOCAL_URL.match(u) for u in urls):
        return None
    sends = False
    for i, a in enumerate(args):
        if a in ("-X", "--request") and i + 1 < len(args) and args[i + 1].upper() != "GET":
            sends = True
        elif re.fullmatch(r"-[a-zA-Z]*X[A-Z]+", a) and not a.endswith("GET"):
            sends = True
        elif re.fullmatch(r"-[a-zA-Z]*X", a) and i + 1 < len(args) and args[i + 1].upper() != "GET":
            sends = True
        elif a.startswith("--request=") and not a.endswith("GET"):
            sends = True
        elif a in ("-d", "-F", "-T", "--json", "--form", "--upload-file") or a.startswith(("--data", "--form")):
            sends = True
        elif a.startswith(("--post-data", "--post-file", "--body-data", "--body-file", "--method")):
            sends = True
    if name in ("http", "https", "xh") and args and args[0].upper() in ("POST", "PUT", "PATCH", "DELETE"):
        sends = True
    return f"{name} sends data to an external service (tier-2: message, publish or production write)" if sends else None


DELETERS = {"rm", "rmdir", "shred", "unlink", "truncate", "del", "erase", "rd"}
POWERSHELL_DELETE = re.compile(r"^(remove-item|rm|ri|del|erase|rd|rmdir|clear-content)$", re.I)
INLINE_DELETE = re.compile(r"rmtree|os\.remove|os\.unlink|\.unlink\(|os\.rmdir|fs\.rm|rmSync|unlinkSync|DROP\s+TABLE|"
                           r"DELETE\s+FROM|TRUNCATE\s|send2trash|Remove-Item", re.I)
INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "php", "deno", "bun", "sqlite3", "psql", "mysql"}
UNINSTALL = re.compile(
    r"^(pip3?|pipx|npm|pnpm|yarn|brew|apt|apt-get|dnf|yum|pacman|choco|winget|scoop|cargo|gem|conda|mamba|uv|snap)$")
PUBLISHERS = {("npm", "publish"), ("npm", "unpublish"), ("twine", "upload"), ("cargo", "publish"),
              ("docker", "push"), ("gem", "push"), ("poetry", "publish"), ("uv", "publish"), ("vsce", "publish")}
SCHEDULERS = {"crontab", "at", "batch", "schtasks", "launchctl", "register-scheduledtask"}


def check_delete(cmd: list[str], ctx: Context) -> str | None:
    name = base_name(cmd[0]).lower()
    args = cmd[1:]
    if name in ("rd", "del", "erase", "rmdir"):
        args = [a for a in args if not re.fullmatch(r"/[A-Za-z]", a)]
    if name in DELETERS or POWERSHELL_DELETE.match(name):
        if not any(not a.startswith("-") for a in args):
            return f"{name} gets its targets at run time (e.g. from xargs) (tier-2: cannot check them)"
        if SUBEXPR in args:
            return f"{name} with a target computed at run time, e.g. (pwd) (tier-2: cannot check it)"
        targets = resolve_targets(args, ctx)
        if targets is None:
            return f"{name} with targets that cannot be resolved (tier-2: possible user data)"
        lost = [t for t in targets if is_user_data(t, ctx)]
        if lost:
            shown = ", ".join(relpath(t, ctx) or t for t in lost[:3])
            return f"{name} deletes data git cannot restore: {shown} (tier-2)"
        return None
    if name == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
        if "-delete" in args or any(base_name(a) in DELETERS for a in args):
            return "find -delete / -exec rm deletes files it selects at run time (tier-2)"
    if base_name(cmd[0]) in INTERPRETERS and INLINE_DELETE.search(" ".join(args)):
        return f"{base_name(cmd[0])} inline code deletes files or rows (tier-2)"
    return None


CHANGE_DIR = {"cd", "pushd", "popd", "chdir", "set-location", "sl", "push-location", "pop-location"}


def moved(cwd: str, args: list[str], precise: bool = False) -> list[str | None]:
    """The directory after `cd ...` (args include the command), or [None] when it cannot be known.
    precise (the write check only): also $HOME, $env:USERPROFILE, -Path:dir and wildcards, and one
    directory per meaning of ~. Every other check keeps the plain reading, where these are unknown."""
    if base_name(args[0]).lower() in ("popd", "pop-location"):
        return [None]
    made = SUBEXPR in args  # cd (Resolve-Path ..): made at run time
    if made and precise:
        return [None]
    args = [a for a in args if a != SUBEXPR]
    if precise:
        args = [a.partition(":")[2] if a.startswith("-") and ":" in a else a for a in args]  # -Path:dir
    args = [a for a in args[1:] if not a.startswith("-") or a == "-"]
    paths = expand_user(ps_expand(args[0]) if precise and args else args[0] if args else "~")
    if len(paths) > 1 and not precise:
        return [None]                       # $HOME and %USERPROFILE% differ: no single directory
    out: list[str | None] = [None] if made else []  # made: unknown, and the plain reading kept too
    for target in paths:
        target = os.path.expandvars(target)
        if target == "-" or "$" in target or (any(ch in target for ch in "*?[") and not precise):
            return [None]
        if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", target):
            return [None]
        full = os.path.normpath(os.path.join(cwd, target))
        if any(ch in target for ch in "*?["):  # Set-Location ~/.cl*
            found = [f for f in hidden_glob(full) or [] if os.path.isdir(f)]
            if not found or len(found) > 8:
                return [None]
            out += found
        else:
            out.append(full)
    return out


CHECK_SECONDS = 6  # classify() answers within this (a check cut short asks); Claude Code's hook timeout is 10 s
BUDGET = {"entries": 20_000, "deadline": float("inf")}  # per classify(): folder entries read, time to answer


@functools.lru_cache(maxsize=256)  # cleared on each classify()
def listing(folder: str) -> tuple[str, ...]:
    try:
        with os.scandir(folder) as entries:
            return tuple(e.name for e in entries)
    except (OSError, ValueError):
        return ()


def hidden_glob(pattern: str) -> list[str] | None:
    """What a wildcard path matches, names starting with a dot included, as in PowerShell (~/*/settings.json).
    None when the call's budget of folder entries runs out (C:/*/*/*/x.txt)."""
    drive, rest = os.path.splitdrive(norm(pattern))
    if norm(pattern).startswith("//"):
        return None  # a network path: a missing host takes seconds to answer
    found = [drive + "/" if rest.startswith("/") else "."]
    for part in (p for p in rest.split("/") if p):
        found = found[:50]
        if not any(ch in part for ch in "*?["):
            found = [os.path.join(f, part) for f in found]
            continue
        nxt = []
        for f in found[:50]:
            if time.monotonic() > BUDGET["deadline"]:  # before each folder: a slow disk overruns by one listing
                return None
            names = listing(f)
            BUDGET["entries"] -= len(names) + 1
            if BUDGET["entries"] < 0:
                return None
            nxt += [os.path.join(f, n) for n in names if fnmatch.fnmatch(n, part)]
        found = nxt
    BUDGET["entries"] -= len(found)
    return None if BUDGET["entries"] < 0 else [f for f in found[:50] if os.path.lexists(f)]


def is_delete(cmd: list[str]) -> bool:
    name = base_name(cmd[0]).lower()
    return name in DELETERS or bool(POWERSHELL_DELETE.match(name)) or (name == "find" and "-delete" in cmd)


def check_shell_command(cmd: list[str], ctx: Context, piped: list[str] | None = None) -> str | None:
    if (reason := check_delete(cmd, ctx)):  # the only check that reads SUBEXPR: rm (pwd) asks
        return reason
    cmd = [a for a in cmd if a != SUBEXPR]
    name = base_name(cmd[0])
    lname = name.lower()
    args = cmd[1:]
    sub = git_args(cmd)
    if sub is not None:
        return check_git(sub, ctx)
    for check in (check_gh, check_http):
        if (reason := check(cmd)):
            return reason
    if UNINSTALL.match(name) and any(a in ("uninstall", "remove", "purge", "rm", "un", "erase") for a in args[:2]):
        return f"{name} {args[0] if args else ''} uninstalls software (tier-2)"
    for pub in PUBLISHERS:
        if name == pub[0] and args[:1] == [pub[1]]:
            return f"{' '.join(pub)} (tier-2: publishing)"
    if any(base_name(a) == "install.py" for a in cmd) and any(a.startswith("--y") for a in args):
        return "installs or changes agent rules and settings (tier-2: own instructions, permissions)"
    if lname in SCHEDULERS and not (lname == "crontab" and args[:1] == ["-l"]) and not (lname == "at" and "-l" in args) \
            and not (lname == "schtasks" and args[:1] and args[0].lower() in ("/query", "-query")  # Q145A: lists only
                     and not any(re.fullmatch(r"[/-](create|change|delete|run|end|showsid)", a, re.I) for a in args)):
        return f"{name} schedules future actions (tier-2: scheduling)"
    if lname == "systemctl" and any(".timer" in a for a in args):
        return "systemd timer (tier-2: scheduling)"
    return original_write_reason(cmd, ctx) or write_reason(cmd, ctx, piped)


def original_write_reason(cmd: list[str], ctx: Context) -> str | None:
    """The write check as it was before Q102A, kept as a layer of its own: whatever asked then still asks."""
    name, args = base_name(cmd[0]), cmd[1:]
    for i, a in enumerate(cmd):
        if a in (">", ">>") and i + 1 < len(cmd) and original_self_instruction(cmd[i + 1], ctx):
            return f"redirect writes agent instructions or settings: {cmd[i + 1]} (tier-2)"
    if name in ("tee", "cp", "mv", "install", "ln", "sed", "perl") and (name != "sed" or any(
            a.startswith("-i") for a in args)):
        for a in args:
            if time.monotonic() > BUDGET["deadline"]:
                return "a command too long to check within the hook's time limit (tier-2: cannot verify)"
            if not a.startswith("-") and original_self_instruction(a, ctx):
                return f"{name} writes agent instructions or settings (tier-2)"
    return None


def original_self_instruction(path: str, ctx: Context) -> bool:
    return original_self(path, ctx.cwd)


@functools.lru_cache(maxsize=4096)  # cleared on each classify(); real() resolves links as os.path.realpath does
def original_self(path: str, cwd: str) -> bool:
    resolved = real(os.path.expanduser(path))
    if LESSONS_INBOX.search(norm(path)) and LESSONS_INBOX.search(norm(resolved)):
        return False
    candidates = {norm(path)}
    for p in expand_user(path):
        p = os.path.expandvars(p)
        full = p if os.path.isabs(p) else os.path.join(cwd, p)
        candidates |= {norm(full), norm(real(full))}
    return any(SELF_INSTRUCTION.search(c) for c in candidates)


def write_reason(cmd: list[str], ctx: Context, piped: list[str] | None = None) -> str | None:
    """Rule 2.3, own instructions: a write into them. piped: words a pipeline may hand to this command."""
    for i, a in enumerate(cmd):
        if a in (">", ">>") and i + 1 < len(cmd) and is_self_instruction(cmd[i + 1], ctx):
            return f"redirect writes agent instructions or settings: {cmd[i + 1]} (tier-2)"
    if (base_name(cmd[0]).lower() != "sed" or any(a.startswith("-i") for a in cmd[1:])) \
            and (found := file_writes(cmd, ctx, piped)):
        writes, trees, reads = (list(dict.fromkeys(p for p in ps if p != SUBEXPR)) for ps in (*found, [])[:3])
        for paths, mode in ((writes, {}), (trees, {"tree": True}), (reads, {"exact": True})):
            for p in paths:
                if time.monotonic() > BUDGET["deadline"]:
                    return "a command too long to check within the hook's time limit (tier-2: cannot verify)"
                if is_self_instruction(p, ctx, **mode):
                    return f"{base_name(cmd[0])} writes agent instructions or settings (tier-2)"
    return None


# --- file writers ------------------------------------------------------------------------
# Each returns (paths it may write, paths judged as a whole tree: a link's target, a folder something lands in).

# Unix and cmd.exe tools: every argument may be a file they write. The placers put the others into
# their last argument (robocopy and xcopy: the second).
UNIX_WRITERS = {"tee", "cp", "mv", "install", "ln", "sed", "perl", "robocopy", "xcopy", "mklink", "rsync", "subst"}
UNIX_PLACERS = {"cp", "mv", "install", "ln", "robocopy", "xcopy", "rsync"}
COPIERS = {"cp", "install", "robocopy", "xcopy", "rsync"}  # their sources are only read

# PowerShell cmdlets that write files (Windows PowerShell 5.1), with their positional parameters in order.
PS_WRITERS = {"set-content": ("path", "value"), "add-content": ("path", "value"), "clear-content": ("path",),
              "out-file": ("filepath", "encoding"), "tee-object": ("filepath",), "new-item": ("path",),
              "copy-item": ("path", "destination"), "move-item": ("path", "destination"),
              "rename-item": ("path", "newname"), "expand-archive": ("path", "destinationpath"),
              "invoke-webrequest": ("uri",), "invoke-restmethod": ("uri",), "export-csv": ("path",),
              "export-clixml": ("path",), "start-bitstransfer": ("source", "destination"),
              "start-transcript": ("path",), "new-psdrive": ("name", "psprovider", "root")}
PS_ALIASES = {"sc": "set-content", "ac": "add-content", "clc": "clear-content", "ni": "new-item", "copy": "copy-item",
              "cpi": "copy-item", "move": "move-item", "mi": "move-item", "ren": "rename-item", "rni": "rename-item",
              "iwr": "invoke-webrequest", "irm": "invoke-restmethod", "epcsv": "export-csv"}
# Those that take the file from the pipeline when no path is given (FileInfo objects bind to -LiteralPath).
PS_PIPED_PATH = {"set-content", "add-content", "clear-content", "copy-item", "move-item", "rename-item"}
# Their parameters (aliases mapped to one name). PowerShell also takes any unambiguous prefix. A parameter
# not listed here is assumed to take a value, and that value is judged as a path.
PS_PATH_PARAMS = {"path": "path", "literalpath": "path", "pspath": "path", "lp": "path", "filepath": "path",
                  "destination": "destination", "destinationpath": "destination", "name": "name",
                  "newname": "newname", "outfile": "path", "outputdirectory": "path", "root": "root"}
PS_TEXT_PARAMS = {"value": "value", "target": "value", "inputobject": "", "encoding": "encoding",
                  "itemtype": "itemtype", "type": "itemtype", "filter": "", "include": "", "exclude": "",
                  "credential": "", "stream": "", "width": "", "variable": "", "uri": "uri", "method": "", "body": "",
                  "headers": "", "erroraction": "", "ea": "", "warningaction": "", "wa": "", "informationaction": "",
                  "errorvariable": "", "ev": "", "warningvariable": "", "outvariable": "", "ov": "", "outbuffer": "",
                  "pipelinevariable": "", "progressaction": "", "fromsession": "", "tosession": "", "timeoutsec": "",
                  "source": "", "psprovider": "", "scope": "", "description": ""}
PS_SWITCHES = {"force", "nonewline", "passthru", "usetransaction", "usetx", "asbytestream", "append", "noclobber",
               "container", "recurse", "verbose", "vb", "debug", "db", "whatif", "wi", "confirm", "cf",
               "usebasicparsing", "usedefaultcredentials", "disablekeepalive", "skipcertificatecheck"}
# Each cmdlet's own switches (Windows PowerShell 5.1): a prefix of one of these is that switch for this cmdlet,
# whatever the shared tables say (cp -r is -Recurse, Start-Transcript -include is -IncludeInvocationHeader).
COMMON_SWITCHES = ("verbose", "debug", "whatif", "confirm")
CMDLET_SWITCHES = {
    "set-content": ("force", "nonewline", "passthru", "usetransaction"), "add-content": ("force", "nonewline",
    "passthru", "usetransaction"), "clear-content": ("force", "usetransaction"),
    "out-file": ("append", "force", "noclobber", "nonewline"), "tee-object": ("append",), "new-item": ("force",),
    "copy-item": ("container", "force", "passthru", "recurse", "usetransaction"),
    "move-item": ("force", "passthru", "usetransaction"), "rename-item": ("force", "passthru", "usetransaction"),
    "expand-archive": ("force", "passthru"), "export-csv": ("append", "force", "noclobber", "notypeinformation",
    "useculture", "includetypeinformation"), "export-clixml": ("force", "noclobber"),
    "start-transcript": ("append", "force", "noclobber", "includeinvocationheader", "useminimalheader"),
    "new-psdrive": ("persist",), "start-bitstransfer": ("asynchronous", "suspended"),
    "invoke-webrequest": ("usebasicparsing", "usedefaultcredentials", "disablekeepalive", "passthru"),
    "invoke-restmethod": ("usedefaultcredentials", "disablekeepalive", "passthru")}

# .NET calls, Windows PowerShell 5.1's only way to write UTF-8 without a BOM. Group 1: the method.
DOTNET_WRITER = re.compile(r"\[(?:system\.)?io\.(file\]::(?:write|append|copy|move|replace|create|open(?!read|text)"
                           r"|delete)|directory\]::(?:move|delete)|(?:streamwriter|filestream)\]::new"
                           r"|compression\.zipfile\]::extracttodirectory)\w*", re.I)
NEW_OBJECT_WRITER = re.compile(r"(?:system\.)?io\.(?:streamwriter|filestream)", re.I)


def is_folder(path: str, ctx: Context) -> bool:
    """An existing folder, in any spelling of home; one the hook cannot locate counts as a folder."""
    if path.endswith("/") or path in (".", "..") or not ctx.cwd:
        return True
    for p in expand_user(ps_expand(path)):
        p = os.path.expandvars(p)
        if p.startswith("$") or os.path.isdir(os.path.join(ctx.cwd, p)):
            return True
    return False


def array_items(value: str) -> list[str]:
    """A PowerShell array a,b as its items (the whole text too: a quoted name may hold a comma)."""
    return [value] + [p for p in value.split(",") if p] if "," in value else [value]


def ps_param(opt: str, cmdlet: str = "") -> tuple[str, str]:
    """('path' | 'text', its name), ('switch', '') or ('unknown', '') for a parameter name or prefix."""
    opt = opt.lower()
    own = CMDLET_SWITCHES.get(cmdlet, ()) + COMMON_SWITCHES
    if opt and opt not in PS_PATH_PARAMS and any(sw.startswith(opt) for sw in own):
        return "switch", ""
    if opt in PS_PATH_PARAMS:
        return "path", PS_PATH_PARAMS[opt]
    if opt in PS_TEXT_PARAMS:
        return "text", PS_TEXT_PARAMS[opt]
    if opt in PS_SWITCHES:
        return "switch", ""
    paths = [v for k, v in PS_PATH_PARAMS.items() if k.startswith(opt)]
    texts = [v for k, v in PS_TEXT_PARAMS.items() if k.startswith(opt)]
    switches = [k for k in PS_SWITCHES if k.startswith(opt)]
    if opt and paths:
        return "path", paths[0]  # its value is judged whatever PowerShell binds it to
    if opt and texts and not switches:
        return "text", texts[0]
    if opt and switches and not texts:
        return "switch", ""
    return "unknown", ""  # text or switch: the next word may be the path, so it is judged


def ps_writes(cmdlet: str, args: list[str], ctx: Context, piped: list[str] | None) -> tuple[list[str], list[str]]:
    """Both readings of a flag given as a prefix or not known here (cp -r: -Recurse, not -Root): as a switch,
    and as a name that takes the next word."""
    args = ["-" + a[1:] if a[:1] and a[:1] in "\u2013\u2014\u2015" else a for a in args]  # an en dash, too
    w1, t1 = ps_reading(cmdlet, args, ctx, piped, greedy=True)
    w2, t2 = ps_reading(cmdlet, args, ctx, piped, greedy=False)
    return list(dict.fromkeys(w1 + w2)), list(dict.fromkeys(t1 + t2))


def ps_reading(cmdlet: str, args: list[str], ctx: Context, piped: list[str] | None,
               greedy: bool) -> tuple[list[str], list[str]]:
    named: dict[str, list[str]] = {}
    positional, judged, i = [], [], 0
    groups: list[list[str]] = []
    for a in args:  # a.txt, b.txt: one array, as PowerShell reads it
        if groups and (groups[-1][-1].endswith(",") or a.startswith(",")):
            groups[-1].append(a)
        else:
            groups.append([a])
    args = ["".join(g) for g in groups]
    while i < len(args):
        a, i = args[i], i + 1
        if len(a) < 2 or not a.startswith("-"):
            positional.append(a)
            continue
        opt, colon, value = a[1:].partition(":")
        kind, canon = ps_param(opt, cmdlet)
        exact = opt.lower() in PS_PATH_PARAMS or opt.lower() in PS_TEXT_PARAMS or opt.lower() in PS_SWITCHES
        if kind == "switch" or (not greedy and not exact and not colon):
            continue
        if not colon and i < len(args) and not args[i].startswith("-"):
            value, i = args[i], i + 1
        if kind == "unknown":
            judged.append(value)
        elif canon:
            named.setdefault(canon, []).append(value)
    slots = [PS_PATH_PARAMS.get(s, s) for s in PS_WRITERS[cmdlet]]
    free = [s for s in slots if s not in named]
    for slot, value in zip(free, positional):
        named.setdefault(slot, []).append(value)
    judged += positional[len(free):]  # more words than PowerShell would bind: judge them too
    if "path" not in named and piped and cmdlet in PS_PIPED_PATH:  # Get-Item x | Set-Content -Value y
        named["path"] = [a for a in piped if not a.startswith("-")]
    get = lambda key: list(dict.fromkeys(p for v in named.get(key, []) for p in array_items(v) if p))  # noqa: E731
    paths, trees = get("path"), []
    dest, names, new_names = get("destination")[:8], get("name")[:8], get("newname")[:8]  # one value in PowerShell
    judged += dest + names + new_names
    if cmdlet == "new-item":
        kind = (get("itemtype") or [""])[0].lower()  # PowerShell takes a prefix: -ItemType d, Junc
        if kind and "directory".startswith(kind):
            return [], []  # an empty folder changes nothing an agent loads, as with mkdir
        if kind and any(t.startswith(kind) for t in ("symboliclink", "junction", "hardlink")):
            trees = get("value")
        paths += placed(paths or ["."], names)
    elif cmdlet in ("copy-item", "move-item", "start-bitstransfer"):  # into a folder: lands as folder/name
        trees = placed([d for d in dest or ["."] if is_folder(d, ctx)], paths)
        if cmdlet == "move-item":
            trees += paths  # Move-Item ~/.claude ~/x takes the settings away
        else:
            paths = []  # a copy's source is only read
    elif cmdlet == "rename-item":
        trees = list(paths)  # Rename-Item ~/.claude off
        paths += [os.path.join(os.path.dirname(p), n) for p in paths[:8] for n in new_names]
    elif cmdlet == "expand-archive":
        paths, trees = [], dest
    elif cmdlet == "new-psdrive":
        paths, judged, trees = [], [], get("root")  # a drive that names ~/.claude is a link to it
    return [p for p in paths + judged if p], trees


# Names that matter when a file lands in a folder: what SELF_INSTRUCTION looks for, and any .md (memory/x.md).
PLACED_NAME = re.compile(r"^(\.claude|\.agent-playbook|claude\.local\.md|\.claude\.json|\.mcp\.json|settings(\.local)?"
                         r"\.json.*|rules|skills|hooks|agents|commands|output-styles|agent-playbook|memory|[^/]*\.md"
                         r"|\.codex|\.cursor|\.gemini|\.agents|policies|extensions|plugins|hooks\.json|config\.toml"
                         r"|cli-config\.json|cli\.json|permissions\.json|system-defaults\.json)$",
                         re.I)


def placed(folders: list[str], names: list[str]) -> list[str]:
    """folder/name for the names that can matter (linear: a long array names no more than once)."""
    useful = list(dict.fromkeys(b for n in names if PLACED_NAME.match(b := os.path.basename(n.rstrip("/")))))
    return [os.path.join(f, b) for f in list(dict.fromkeys(folders))[:8] for b in useful]


def dotnet_writes(cmd: list[str], i: int) -> list[str]:
    """The path arguments of the .NET call in cmd[i]: ([IO.File]::WriteAllText p, s) after tokenize."""
    args, new = [], True
    for tok in cmd[i + 1:]:
        if tok.lower() in ("-argumentlist", "-args"):
            continue
        for j, piece in enumerate(tok.split(",")):
            new = new or j > 0
            if piece and new:
                args.append(piece)
                new = False
    method = (m.group(1).lower() if (m := DOTNET_WRITER.search(cmd[i])) else "new")
    if method.startswith(("file]::copy", "compression.zipfile]::extract")):
        return args[1:2]
    return args[:3] if "move" in method or "replace" in method else args[:1]


def download_writes(name: str, args: list[str]) -> tuple[list[str], list[str]]:
    """curl -o FILE, --output-dir DIR; wget -O FILE, -P DIR; and PowerShell's curl/wget alias, -OutFile FILE."""
    files, dirs = [], []
    for i, a in enumerate(args):
        value = args[i + 1] if i + 1 < len(args) else ""
        opt, eq, inline = a.partition("=")
        attached = re.fullmatch(r"-[a-zA-Z]*" + ("o" if name == "curl" else "O") + r"(.+)", a)
        if attached and not a.startswith("--"):  # curl -o$HOME/x, wget -qO$HOME/x
            files.append(attached.group(1))
        elif (name == "curl" and (re.fullmatch(r"-[a-zA-Z]*o", a) or a == "--output")) \
                or (name == "wget" and (re.fullmatch(r"-[a-zA-Z]*O", a) or opt == "--output-document")) \
                or (len(opt) > 4 and "-outfile".startswith(opt.lower().partition(":")[0])):
            files.append(inline or a.partition(":")[2] or value)
        elif (name == "curl" and opt == "--output-dir") or (name == "wget" and (a == "-P" or opt == "--directory-prefix")):
            dirs.append(inline or value)
    return files, dirs


def file_writes(cmd: list[str], ctx: Context, piped: list[str] | None = None) -> tuple[list[str], ...] | None:
    """(paths written, paths judged as a whole tree[, sources only read, judged as before this change])."""
    name = base_name(cmd[0]).lower()
    cmdlet = PS_ALIASES.get(name, name)
    if ctx.shell == "PowerShell":  # there cp, mv and tee are Copy-Item, Move-Item and Tee-Object
        cmdlet = {"cp": "copy-item", "mv": "move-item", "tee": "tee-object"}.get(name, cmdlet)
    if cmdlet in PS_WRITERS:
        return ps_writes(cmdlet, cmd[1:], ctx, piped)
    if name.removesuffix(".exe") in ("curl", "wget"):
        return download_writes(name.removesuffix(".exe"), cmd[1:])
    if name in ("fsutil", "fsutil.exe") and [a.lower() for a in cmd[1:3]] == ["hardlink", "create"]:
        return cmd[3:], cmd[3:]  # fsutil hardlink create LINK TARGET
    if name == "dd":  # dd of=FILE
        return [a[3:] for a in cmd[1:] if a.startswith("of=")], []
    if name in ("unzip", "7z", "tar"):  # unzip -d DIR, 7z x -oDIR, tar -C DIR: what lands there is unknown
        dirs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a in ("-d", "-C", "--directory")]
        dirs += [a[2:] for a in cmd[1:] if name == "7z" and a.startswith("-o")]
        dirs += [a.partition("=")[2] for a in cmd[1:] if a.startswith("--directory=")]
        return [], dirs
    if name in UNIX_WRITERS:
        vals, skip = [], False
        for a in cmd[1:]:
            if name in ("robocopy", "xcopy", "mklink") and a.startswith("/"):
                skip = a.lower() in ("/xd", "/xf")  # robocopy /XD names...: folders left out, not written
                continue
            if a.startswith("-"):  # --target-directory=DIR, -Destination:DIR
                a = a.lstrip("-").partition("=" if a.startswith("--") else ":")[2]
            if not skip:
                vals += array_items(a) if a else []
        dest = (vals[1:2] if name in ("robocopy", "xcopy") else vals[-1:]) if name in UNIX_PLACERS else []
        targets = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "-t"]  # cp -t DIR files: the last counts
        if targets:
            dest = targets[-1:]
        dest = ([a.partition("=")[2] for a in cmd if a.startswith("--target-directory=")] or dest)[-1:]
        sources = [v for v in vals if v not in dest]
        trees = placed([d for d in dest if len(vals) > 2 or is_folder(d, ctx)], sources)
        if name in ("robocopy", "xcopy"):
            trees += dest  # the source folder's contents land in the target itself
        if name in ("ln", "mklink", "subst", "mv"):
            trees += vals  # a link or drive to ~/.claude; mv ~/.claude ~/x takes the settings away
        if name in COPIERS:
            return dest, trees, sources
        return vals, trees
    for i, a in enumerate(cmd):  # [IO.File]::WriteAllText(p, s), $fs = [IO.File]::Create(p), [void][IO.File]::...
        if (i == 0 or cmd[i - 1] == "=") and DOTNET_WRITER.search(a):
            return dotnet_writes(cmd, i), []
        if (i == 0 or cmd[i - 1] == "=") and a.lower() == "new-object":
            rest = [t for t in cmd[i + 1:] if t.lower() != "-typename"]
            if rest and NEW_OBJECT_WRITER.fullmatch(rest[0]):
                return dotnet_writes(rest, 0), []
    return None


# --- self-instruction and secret files -----------------------------------------------------

# One shared "(^|/)" prefix, then the names: matching tries the alternatives only at a path start or
# after a slash, which keeps long commands with many paths fast (test_many_folders_and_copies_stay_fast).
SELF_INSTRUCTION = re.compile(
    r"(?:^|/)(?:"
    r"(?:claude\.md|agents\.md|claude\.local\.md|memory\.md|\.claude\.json|\.mcp\.json)$"
    r"|\.claude/(?:settings(?:\.local)?\.json|rules/|skills/|hooks/|agents/|commands/|output-styles/|agent-playbook/"
    r"|plugins/)"
    r"|\.agent-playbook/|memory/[^/]+\.md$"
    # the other agents' instructions, hooks, rules and skills (client_codex, client_cursor, client_gemini)
    r"|(?:agents\.override\.md|gemini\.md)$|\.agents/skills/"
    r"|\.codex/(?:hooks\.json|config\.toml|rules/|skills/|agent-playbook/)"
    r"|\.cursor/(?:hooks\.json|hooks/|rules/|plugins/|skills/|skills-cursor/|agents/|commands/|agent-playbook/"
    r"|cli-config\.json|cli\.json|permissions\.json|mcp\.json)|\.cursor(?:rules|ignore)$|cursor/user/settings\.json$"
    r"|\.gemini/(?:[^/]+\.json$|policies/|extensions/|commands/|skills/|agents/|hooks/)"
    r"|gemini-?cli/(?:settings\.json|system-defaults\.json|policies/))", re.I)


LESSONS_INBOX = re.compile(r"(^|/)\.claude/agent-playbook/lessons-inbox\.md$", re.I)
PS_HOME = re.compile(r"^\$(home|env:userprofile|env:home)(?=/|\\|$)", re.I)
PS_ENV = re.compile(r"\$env:(\w+)", re.I)


def ps_expand(path: str) -> str:
    """PowerShell's spellings: $HOME and $env:USERPROFILE are the home folder (every one ~ may mean)."""
    path = re.sub(r"^(?:Microsoft\.PowerShell\.Core[\\/])?FileSystem::", "", path, flags=re.I)
    path = PS_HOME.sub("~", path)
    return PS_ENV.sub(lambda m: os.environ.get(m.group(1), m.group(0)), path)


CLAUDE_FOLDER = re.compile(r"(^|/)\.(claude|codex|cursor|gemini|agents)/?$", re.I)
def without_stream(path: str) -> str:
    """CLAUDE.md::$DATA and CLAUDE.md:x are streams of CLAUDE.md itself (NTFS); C: stays a drive."""
    head, sep, last = path.rpartition("/")
    if ":" not in last or (not head and re.match(r"^[A-Za-z]:$", last)):
        return path
    return head + sep + last.split(":", 1)[0]


PROBE_NAMES = (".claude", ".agent-playbook", "rules", "skills", "hooks", "agents", "commands", "output-styles",
               "agent-playbook", "memory", "settings.json", "settings.local.json", "claude.md", "agents.md",
               "claude.local.md", "memory.md", ".claude.json", ".mcp.json", "x.md", "x",
               ".codex", ".cursor", ".gemini", ".agents", "agents.override.md", "gemini.md", "policies", "extensions",
               "plugins", "hooks.json", "config.toml", "cli-config.json", "permissions.json")


def could_name_instructions(pattern: str) -> bool:
    """Whether a wildcard path could match an instruction path: each wildcard part is tried as each name
    SELF_INSTRUCTION looks for, or any other name (x)."""
    parts = norm(pattern).lower().split("/")
    options = [[n for n in PROBE_NAMES if fnmatch.fnmatchcase(n, part)] or ["x"] if any(ch in part for ch in "*?[")
               else [part] for part in parts]
    combos = 1
    for o in options:
        combos *= len(o)
    if combos > 512:
        return True  # too many to try: assume it may
    return any(SELF_INSTRUCTION.search("/".join(c)) or SELF_INSTRUCTION.search("/".join(c) + "/")
               for c in itertools.product(*options))


def is_self_instruction(path: str, ctx: Context, tree: bool = False, exact: bool = False) -> bool:
    """tree: also a .claude folder itself (a link to it, or a folder copied over it). exact: the file itself
    only, as before Q102A (a copy's source). An empty ctx.cwd is a folder the hook could not follow."""
    anywhere = (path.startswith(("~", "$")) and not re.match(r"\$pwd\b", path, re.I)) or os.path.isabs(path)
    return self_instruction(path, "" if anywhere else ctx.cwd, tree, exact)


@functools.lru_cache(maxsize=4096)  # one command can name the same path in many folders; cleared per call
def self_instruction(path: str, cwd: str, tree: bool, exact: bool = False) -> bool:
    candidates = {norm(path)}
    for p in expand_user(ps_expand(path)):
        p = os.path.expandvars(re.sub(r"^\$pwd(?=/|$)", lambda m: cwd or m.group(0), p, flags=re.I))
        if p.startswith("$"):
            continue  # a variable the hook cannot know ($p = ...): only the text itself is judged
        if not cwd and not os.path.isabs(p):
            continue  # a folder the hook could not follow: only the text itself is judged
        full = re.sub(r"(?<=/)\./", "", norm(p if os.path.isabs(p) else os.path.join(cwd, p)))  # dir/./x
        found = hidden_glob(full) if any(ch in p for ch in "*?[") and not exact else []  # rule?/x.md: rules/x.md
        if found is None and could_name_instructions(full):
            return True  # too wide a wildcard to check (C:/*/*/*/x) that may reach them
        candidates |= {norm(f) for f in [full, *(found or [])]} | {norm(real(f)) for f in [full, *(found or [])]}
    candidates |= {without_stream(c) for c in candidates}
    resolved = candidates - {norm(path)} if len(candidates) > 1 else candidates  # cd dir; ac lessons-inbox.md
    if all(LESSONS_INBOX.search(c) for c in resolved):
        return False  # a note for later review (Q92), never loaded as instructions; not if it is a link
    return any(SELF_INSTRUCTION.search(c) or (not exact and SELF_INSTRUCTION.search(c + "/"))
               or (tree and CLAUDE_FOLDER.search(c)) for c in candidates)  # c + "/": cp x.md ~/.claude/rules


SECRET_FILE = re.compile(
    r"(^|/)(\.env(\.(?!example$|sample$|template$|dist$)[\w.-]+)?|[^/]*\.pem|[^/]*\.key|id_(rsa|ed25519|ecdsa|dsa)"
    r"|\.?credentials(\.json)?|secrets?\.(json|ya?ml|toml)|\.netrc|_netrc|\.pgpass|\.npmrc|\.pypirc"
    r"|\.git-credentials|hosts\.yml|\.dockercfg)$|(^|/)\.docker/config\.json$", re.I)
NOT_SECRET = re.compile(r"\.(example|sample|template|dist)$", re.I)
INLINE_SECRET = re.compile(r"""['"/\\]\.env\b(?!\.(example|sample|template))|\.credentials|id_rsa|\.netrc""", re.I)
PRINTERS = {"cat", "head", "tail", "less", "more", "bat", "strings", "xxd", "od", "hexdump", "grep", "rg", "awk",
            "sed", "base64", "nl", "tac", "sort", "uniq", "cut", "jq", "yq", "type", "get-content", "gc", "openssl",
            "cp", "scp", "curl", "nc"}
SECRET_VAR = re.compile(r"\$\{?[A-Za-z_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z_]*\}?", re.I)


def is_secret_file(path: str) -> bool:
    p = norm(path)
    return bool(SECRET_FILE.search(p)) and not NOT_SECRET.search(p)


def expand_globs(args: list[str], cwd: str) -> list[str]:
    out = []
    for a in args:
        out.append(a)
        if any(ch in a for ch in "*?["):
            out += [g for p in expand_user(a) for g in glob.glob(os.path.join(cwd, p))]
    return out


def check_secret_exposure(cmd: list[str], cwd: str = ".") -> str | None:
    """Rule 5.1: anything that prints a secret puts it into the transcript."""
    name = base_name(cmd[0]).lower()
    args = cmd[1:]
    if name in PRINTERS and any(is_secret_file(a) for a in expand_globs(args, cwd) if not a.startswith("-")):
        return "prints a secret file into the transcript (rule 5.1)"
    if name in ("printenv", "env", "set", "export", "declare", "compgen", "get-childitem") and \
            (not args and name != "get-childitem" or name == "printenv"
             or any(a.lower().startswith("env:") for a in args)):  # bare Get-ChildItem lists the folder
        return "prints environment variables, which may hold secrets (rule 5.1)"
    if name in ("echo", "printf", "write-output", "write-host") and any(SECRET_VAR.search(a) for a in args):
        return "echoes a secret variable (rule 5.1)"
    if name in INTERPRETERS and INLINE_SECRET.search(" ".join(args)):
        return f"{name} inline code reads a secret file (rule 5.1)"
    return None


# --- tool-level classification -------------------------------------------------------------

OUTWARD_WORDS = re.compile(
    r"(^|_)(create|update|delete|merge|send|post|publish|share|trash|push|fork|write|reply|comment|review|trigger"
    r"|enable|disable|archive|transfer|add|remove|set|run|dispatch|upload|move|rename|invite|approve|close|lock"
    r"|assign|label|schedule|cancel)(_|$)", re.I)
OUTWARD_NATIVE = {"RemoteTrigger", "CronCreate", "CronDelete", "ScheduleWakeup", "PushNotification"}
# MCP servers the Claude apps run locally for their own UI (plans, session state, IDE, previews).
LOCAL_MCP_SERVERS = {"ccd_turn", "ccd_session", "ide", "claude_preview"}
# Single local actions on servers whose other tools still ask: a message to the user's own Claude session (Q94A).
LOCAL_MCP_TOOLS = {("ccd_session_mgmt", "send_message")}
LOCAL_OBJECT = re.compile(r"(^|_)(plan|todo|todos|title|status|notes?)$", re.I)


def file_write_reason(path: str, ctx: Context, overwrite: bool) -> str | None:
    if not path:
        return None
    if is_self_instruction(path, ctx) or original_self_instruction(path, ctx):  # the old reading kept too
        return f"edits agent instructions or settings: {path} (tier-2)"
    p = os.path.expanduser(path)
    full = p if os.path.isabs(p) else os.path.join(ctx.cwd, p)
    resolved = real(full)
    if resolved != os.path.abspath(full) and is_self_instruction(resolved, ctx):
        return f"writes through a link into agent settings: {path} -> {resolved} (tier-2)"
    inside = relpath(full, ctx) is not None
    if overwrite or not inside:
        if os.path.exists(resolved) and is_user_data(resolved, ctx):
            return f"overwrites a file git cannot restore: {path} (tier-2)"
    elif relpath(full, ctx) is not None and any(
            fnmatch.fnmatch(relpath(full, ctx), g) for g in ctx.data_paths):
        return f"edits a declared data file: {path} (tier-2)"
    return None


# What the MORE reading reads differently from PLAIN; without any of these the two are the same.
MORE_SIGNS = re.compile(r"[(=]|(?:^|[\s;|&{])\.\s|\biex\b|invoke-expression|powershell|pwsh|@['\"]", re.I)
HERE_OPEN = re.compile(r"@(['\"])[ \t]*\r?\n")


def cut_here_strings(command: str) -> str:
    """A PowerShell here-string (@' on one line ... '@ at the start of a later one) as one quoted word, so an
    apostrophe inside it (don't) cannot hide the lines after it. One pass: once a quote has no close, later
    openers with that quote are left as they are."""
    out, pos, search, closeless = [], 0, 0, set()
    while (m := HERE_OPEN.search(command, search)):
        close = -1 if m.group(1) in closeless else command.find("\n" + m.group(1) + "@", m.end() - 1)
        if close < 0:
            closeless.add(m.group(1))
            search = m.end()
            continue
        out += [command[pos:m.start()], "'here-string'"]
        pos = search = close + 3
    return "".join(out) + command[pos:]


def check_shell(command: str, ctx: Context, mode: str = PLAIN) -> str | None:
    """Every check on a shell command read as `mode`; for PATHS only the write check, reading PowerShell
    path expressions ((Join-Path ...), ("x")) as the paths they make."""
    paths = mode == PATHS
    dirs: list[str | None] = [ctx.cwd]  # every directory a later command may run in
    queue, prev, evaluated = split_commands(command, mode=mode), None, 0
    earlier: list[str] = []  # words of the commands before: what a pipeline may hand on (Get-Item x | ...)
    while queue:
        if time.monotonic() > BUDGET["deadline"]:
            return "a command too long to check within the hook's time limit (tier-2: cannot verify)"
        cmd = queue.pop(0)
        if prev:
            earlier = (earlier + prev[1:])[-64:]
        if base_name(cmd[0]).lower() in ("iex", "invoke-expression") and len(cmd) == 1 and prev \
                and evaluated < 8 and mode != PLAIN:  # 'Set-Content ...' | iex
            queue[:0], evaluated = split_commands(" ".join(prev), mode=mode), evaluated + 1
        if base_name(cmd[0]).lower() in CHANGE_DIR:
            dirs = list(dict.fromkeys(dirs + [m for d in dirs for m in (moved(d, cmd, paths) if d else [None])]))
            if len(dirs) > 16 and paths:  # the start and the newest: the likeliest
                dirs = list(dict.fromkeys([None, ctx.cwd] + [d for d in dirs if d][-15:]))
            elif len(dirs) > 16:
                dirs = dirs[:16] + [None]
            prev = cmd
            continue
        for d in dirs:
            if d is None:
                if is_delete(cmd) and not paths:
                    return f"{cmd[0]} after a cd the hook cannot follow (tier-2: possible user data)"
                if (reason := write_reason(cmd, replace(ctx, cwd=""), earlier)):
                    return reason
                continue
            here = replace(ctx, cwd=d)
            if paths:
                if (reason := write_reason(cmd, here, earlier)):
                    return reason
            elif (reason := check_shell_command(cmd, here, earlier)
                  or check_secret_exposure([a for a in cmd if a != SUBEXPR], d)):
                return reason
        prev = cmd
    if time.monotonic() > BUDGET["deadline"]:  # the last command alone took the time
        return "a command too long to check within the hook's time limit (tier-2: cannot verify)"
    return None


def classify(tool_name: str, tool_input: dict, ctx: Context) -> tuple[str, str]:
    self_instruction.cache_clear()  # files and links may have changed since the last call
    original_self.cache_clear()
    real_step.cache_clear()
    listing.cache_clear()
    BUDGET.update(entries=20_000, deadline=time.monotonic() + CHECK_SECONDS)
    tool_name = str(tool_name or "")
    if not isinstance(tool_input, dict):
        tool_input = {}
    for pattern in ctx.ask_tools:
        if fnmatch.fnmatch(tool_name, pattern):
            return ASK, f"{tool_name} is configured as tier-2 for this user"
    if tool_name in OUTWARD_NATIVE:
        return ASK, f"{tool_name} schedules or sends something (tier-2)"
    if tool_name.startswith("mcp__") and OUTWARD_WORDS.search(tool_name.split("__")[-1]):
        parts = tool_name.split("__")
        server, action = (parts[1] if len(parts) > 2 else ""), parts[-1]
        if (server.lower() not in LOCAL_MCP_SERVERS and (server.lower(), action.lower()) not in LOCAL_MCP_TOOLS
                and not LOCAL_OBJECT.search(action)):
            return ASK, f"{tool_name} acts outside this machine (tier-2)"
    if tool_name in ("Bash", "PowerShell"):
        command = str(tool_input.get("command", ""))
        if tool_name == "PowerShell":
            command = command.replace("\\", "/")  # PowerShell escapes with a backtick, not a backslash
        made = re.findall(r"\b([A-Za-z_]\w*)=(?:\$\(|`)\s*mktemp\b", command)
        ctx = replace(ctx, temp_vars=frozenset(
            v for v in made if len(re.findall(rf"(?<![\w$]){v}=", command)) == 1))  # reassigned: not trusted
        more = cut_here_strings(command) if tool_name == "PowerShell" else command
        joined = re.sub(r"`\r?\n", " ", more) if tool_name == "PowerShell" else more  # Set-Content `
        ctx = replace(ctx, shell=tool_name)
        if (reason := check_shell(command, ctx) or (MORE_SIGNS.search(more) and check_shell(more, ctx, MORE))
                or check_shell(joined, ctx, PATHS)):
            return ASK, reason
        return ALLOW, ""
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if (reason := file_write_reason(path, ctx, overwrite=tool_name == "Write")):
            return ASK, reason
        content = " ".join(str(tool_input.get(k, "")) for k in ("content", "new_string", "new_source"))
        for edit in tool_input.get("edits") or []:
            if isinstance(edit, dict):
                content += " " + str(edit.get("new_string", ""))
        if (found := secret_patterns.find(content)):
            return ASK, f"writes a secret-like value ({', '.join(found)}) into {path} (rule 5.1)"
        return ALLOW, ""
    if tool_name in ("Read", "Grep", "Glob"):
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if is_secret_file(path) and (tool_name != "Grep" or tool_input.get("output_mode") != "files_with_matches"):
            return ASK, "reads a secret file into the transcript (rule 5.1)"
    return ALLOW, ""

