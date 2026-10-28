"""Build the public repo from this one and, with --yes, push it (Q14A).

  python tools/publish_public.py stage DIR        build the public tree in DIR and run every check there
  python tools/publish_public.py publish          stage, then show what would change in the public repo
  python tools/publish_public.py publish --yes    ...and commit, tag v<VERSION> and push it

The tree comes from HEAD, not the working copy, so commit first. It holds the files matched by
tools/public-allowlist.txt; a file under public/overlay/<path> replaces <path>. Every mention of this
repo's GitHub name becomes PUBLIC_REPO. The public history is its own: one commit per publish.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_REPO = "chong611/ask-first"
OVERLAY = "public/overlay/"


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd or ROOT, check=True, capture_output=True)


def private_slug() -> str:
    url = git("remote", "get-url", "origin").stdout.decode().strip()
    m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(\.git)?$", url)
    if not m:
        sys.exit(f"cannot read the GitHub name from origin: {url}")
    return m.group(1)


def allowlist() -> list[str]:
    lines = (ROOT / "tools" / "public-allowlist.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def allowed(rel: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or (g.endswith("/**") and rel.startswith(g[:-2])) for g in globs)


def stage(out: Path) -> list[str]:
    """Write the public tree into out (which must not exist) and return its paths."""
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("note: uncommitted changes are not published; the tree comes from HEAD", file=sys.stderr)
    head = git("ls-tree", "-r", "-z", "--name-only", "HEAD").stdout.decode().split("\0")
    globs = allowlist()
    files = {rel: rel for rel in head if rel and allowed(rel, globs)}
    for rel in head:
        if rel.startswith(OVERLAY):
            target = rel[len(OVERLAY):]
            if target not in files:
                sys.exit(f"overlay {rel} has no allowlisted original at {target}")
            files[target] = rel
    old, new = private_slug().encode(), PUBLIC_REPO.encode()
    for target, source in sorted(files.items()):
        data = git("show", f"HEAD:{source}").stdout.replace(old, new)
        dest = out / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return sorted(files)


def verify(tree: Path) -> None:
    """Run the repo checks and the whole test suite inside the staged tree."""
    slug = private_slug().encode()
    leftover = [p.relative_to(tree).as_posix() for p in tree.rglob("*")
                if p.is_file() and ".git" not in p.parts and slug in p.read_bytes()]
    if leftover:
        sys.exit(f"the private repo name is still in: {', '.join(leftover)}")
    sys.path.insert(0, str(ROOT / "tools"))
    import check  # this repo's check.py: the public tree has no private markers file to scan with
    hits = check.scan_markers([p for p in tree.rglob("*") if p.is_file() and ".git" not in p.parts],
                              check.private_markers(), root=tree)
    if hits:
        sys.exit("personal markers in the public tree; nothing was published:\n" + "\n".join(hits))
    if not (tree / ".git").exists():
        git("init", "-q", "-b", "main", cwd=tree)
        git("add", "-A", cwd=tree)
        git("-c", "user.name=stage", "-c", "user.email=stage@example.invalid",
            "commit", "-q", "-m", "stage", cwd=tree)
    # an empty home: the public tree must pass on any machine, not only with the owner's personal profile
    home = tempfile.mkdtemp()
    env = {**os.environ, "HOME": home, "USERPROFILE": home}
    for cmd in ([sys.executable, "tools/check.py"], [sys.executable, "-m", "unittest", "discover", "-s", "tests"]):
        run = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env=env)
        tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
        print(f"{' '.join(cmd[1:])}: exit {run.returncode}; " + " | ".join(tail))
        if run.returncode:
            sys.exit("a check failed in the public tree; nothing was published")


def publish(yes: bool) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    head = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
    name = git("config", "user.name").stdout.decode().strip()
    email = git("config", "user.email").stdout.decode().strip()
    if not email.endswith("@users.noreply.github.com"):  # a real address would stay in public history for good
        sys.exit("set this repo's git user.email to your GitHub no-reply address before publishing")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "stage"
        stage(staged)
        verify(staged)
        clone = Path(tmp) / "public"
        url = f"https://github.com/{PUBLIC_REPO}.git"
        if subprocess.run(["git", "clone", "-q", url, str(clone)], capture_output=True).returncode:
            sys.exit(f"cannot clone {url}: create the empty repo first")
        if subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=clone, capture_output=True).returncode:
            git("checkout", "-q", "-B", "main", cwd=clone)  # empty repo: no branch yet
        for item in clone.iterdir():
            if item.name != ".git":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        for item in staged.iterdir():
            if item.name != ".git":
                (shutil.copytree if item.is_dir() else shutil.copy2)(item, clone / item.name)
        git("add", "-A", cwd=clone)
        stat = git("diff", "--cached", "--stat", cwd=clone).stdout.decode().strip()
        print(stat or "no changes")
        tag = f"v{version}"
        has_tag = bool(git("ls-remote", "--tags", "origin", tag, cwd=clone).stdout.strip())
        if not yes:
            print(f"Nothing pushed. With --yes: commit as {name} <{email}>, "
                  f"{'keep' if has_tag else 'add'} tag {tag}, push to {PUBLIC_REPO}.")
            return
        ident = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
        if stat:
            git(*ident, "commit", "-q", "-m", f"ask-first {version} (from {head})", cwd=clone)
        if not has_tag:
            git(*ident, "tag", "-a", tag, "-m", f"ask-first {version}", cwd=clone)
        git("push", "-q", "origin", "main", *([tag] if not has_tag else []), cwd=clone)
        print(f"pushed {PUBLIC_REPO} main" + ("" if has_tag else f" and {tag}"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("dir")
    p = sub.add_parser("publish")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if args.cmd == "stage":
        out = Path(args.dir)
        if out.exists():
            sys.exit(f"{out} exists; pass a new folder")
        print(f"staged {len(stage(out))} files in {out}")
        verify(out)
    else:
        publish(args.yes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
