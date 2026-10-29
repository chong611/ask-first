"""tools/publish_public.py builds the public tree from HEAD: allowlist, overlay, repo-name swap (Q14A)."""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import mkdtemp  # noqa: E402
import publish_public  # noqa: E402


class Repo(unittest.TestCase):
    def setUp(self):
        self.repo = Path(mkdtemp())
        run = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("remote", "add", "origin", "https://github.com/someone/private-name.git")
        files = {
            "tools/public-allowlist.txt": "# comment\nREADME.md\ntests/**\n",
            "README.md": "see https://github.com/someone/private-name\n",
            "tests/fixtures/data.json": "private original\n",
            "public/overlay/tests/fixtures/data.json": "public version\n",
            "docs/DECISIONS.md": "private\n",
        }
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        run("add", "-A")
        run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
        self.old_root = publish_public.ROOT
        publish_public.ROOT = self.repo

    def tearDown(self):
        publish_public.ROOT = self.old_root


class Stage(Repo):
    def test_allowlist_overlay_and_name_swap(self):
        out = Path(mkdtemp()) / "pub"
        paths = publish_public.stage(out)
        self.assertEqual(paths, ["README.md", "tests/fixtures/data.json"])
        self.assertFalse((out / "docs").exists())
        self.assertFalse((out / "public").exists())
        self.assertEqual((out / "tests/fixtures/data.json").read_text(encoding="utf-8"), "public version\n")
        readme = (out / "README.md").read_text(encoding="utf-8")
        self.assertIn(publish_public.PUBLIC_REPO, readme)
        self.assertNotIn("someone/private-name", readme)

    def test_overlay_without_original_stops(self):
        extra = self.repo / "public/overlay/docs/DECISIONS.md"
        extra.parent.mkdir(parents=True)
        extra.write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x"],
                       cwd=self.repo, check=True)
        with self.assertRaises(SystemExit):
            publish_public.stage(Path(mkdtemp()) / "pub")


class VerifyStopsOnPersonalMarkers(Repo):
    def test_marker_in_staged_tree_stops_before_publishing(self):
        import check
        markers = Path(mkdtemp()) / "markers.txt"
        markers.write_text("zorblax\n", encoding="utf-8")
        tree = Path(mkdtemp()) / "pub"
        publish_public.stage(tree)
        (tree / "README.md").write_text("hello zorblax\n", encoding="utf-8")
        old = check.MARKER_FILES
        check.MARKER_FILES = (markers,)
        try:
            with self.assertRaises(SystemExit) as stop:
                publish_public.verify(tree)
        finally:
            check.MARKER_FILES = old
        self.assertIn("personal markers", str(stop.exception))
        self.assertNotIn("zorblax", str(stop.exception))


class CheckScansWhatIsPublished(Repo):
    def test_overlay_replaces_the_original_in_the_public_scan(self):
        import check
        old = check.ROOT
        check.ROOT = self.repo
        try:
            rels = sorted(p.relative_to(self.repo).as_posix() for p in check.public_files())
        finally:
            check.ROOT = old
        self.assertEqual(rels, ["README.md", "public/overlay/tests/fixtures/data.json"])


if __name__ == "__main__":
    unittest.main()
