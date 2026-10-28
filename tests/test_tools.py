import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build  # noqa: E402
import check  # noqa: E402
import secret_scan  # noqa: E402


class BuildTest(unittest.TestCase):
    def test_claude_md_imports_agents_md(self):
        self.assertTrue(build.outputs()["project/CLAUDE.md"].endswith("@AGENTS.md\n"))

    def test_global_file_is_core_only(self):  # profiles are opt-in at install (Q156)
        text = build.outputs()["global/rules/agent-playbook.md"]
        self.assertIn("# agent-playbook core", text)
        self.assertNotIn("# profile:", text)

    def test_stale_detects_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            build.write(dist)
            self.assertEqual(build.stale(dist), [])
            (dist / "project/AGENTS.md").write_text("edited\n", encoding="utf-8")
            self.assertEqual(build.stale(dist), ["project/AGENTS.md"])

    def test_committed_dist_is_fresh(self):
        self.assertEqual(build.stale(), [])

    def test_read_drops_bom(self):  # Windows PowerShell 5.1 saves owner.md with one; install appends it mid-file
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owner.md"
            path.write_text("# profile: owner\n", encoding="utf-8-sig")
            self.assertEqual(build.read(path), "# profile: owner\n")


class CheckTest(unittest.TestCase):
    def test_budget_ignores_comment_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.md"
            path.write_text("<!-- marker -->\nabc\n", encoding="utf-8")
            self.assertEqual(check.always_on_bytes([path]), 4)
            path.write_text("<!-- marker -->\nabc\n", encoding="utf-8-sig")  # a BOM before the comment
            self.assertEqual(check.always_on_bytes([path]), 4)

    def test_budget_counts_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.md"
            path.write_text("完成\n", encoding="utf-8")
            self.assertEqual(check.always_on_bytes([path]), 7)

    def test_repo_passes_budget_headings_and_hidden(self):
        self.assertEqual(check.check_budget(owners=[]), [])  # public text only, whatever this machine holds
        self.assertEqual(check.check_headings(), [])
        self.assertEqual(check.check_hidden(), [])

    def test_budget_counts_first_owner_profile_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing, owner = Path(tmp) / "missing.md", Path(tmp) / "owner.md"
            owner.write_text("x" * (check.BUDGET_BYTES + 1), encoding="utf-8")
            public = check.budget_paths(owners=[])
            self.assertEqual(check.budget_paths(owners=[missing, owner]), public + [owner])
            self.assertEqual(len(check.check_budget(owners=[missing, owner])), 1)
            self.assertEqual(check.owner_candidates()[0], Path.home() / ".claude" / "rules" / "owner.md")  # live first

    def test_windows_invalid_paths(self):
        bad = check.portable_path_errors(["as: env override#", "a/b?.md", "docs/CON.md", "x/trail. "])
        self.assertEqual(len(bad), 4)
        self.assertEqual(check.portable_path_errors(["docs/DECISIONS.md", "src/hooks/policy.py"]), [])

    def test_hidden_unicode_regex(self):
        self.assertTrue(check.HIDDEN.search("a\u202eb"))
        self.assertFalse(check.HIDDEN.search("完成 · Done"))


class PublicMarkerTest(unittest.TestCase):
    """Made-up terms only. The real ones live in docs/private-markers.txt, which is never published."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.markers = self.tmp / "markers.txt"
        self.markers.write_text("# made-up terms\n\nzorblax\nblue heron\n测试标记\n", encoding="utf-8")

    def check_file(self, name, text, markers=None):
        """check_public on one temp public file, with only the given marker files."""
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        files = (self.markers,) if markers is None else markers
        with mock.patch.object(check, "public_files", return_value=[path]), \
                mock.patch.object(check, "MARKER_FILES", files, create=True):
            return check.check_public()

    def test_term_from_the_markers_file_is_flagged(self):
        for line in ("see the Zorblax notes", "a BLUE heron", "blue_heron", "blue - heron", "见测试标记三"):
            with self.subTest(line=line):
                self.assertEqual(len(self.check_file("notes.md", line + "\n")), 1)

    def test_clean_line_passes(self):
        self.assertEqual(self.check_file("notes.md", "a blue sky, a heron; zorbla x; 测试 标记\n"), [])

    def test_generic_markers_need_no_markers_file(self):  # the public repo's CI has none
        missing = (self.tmp / "missing.txt",)
        self.assertIsNone(check.private_markers(missing))
        self.assertEqual(len(self.check_file("a.md", "mail someone" + "@" + "gmail.com\n", missing)), 1)
        self.assertEqual(len(self.check_file("a.md", "host " + "LAPTOP" + "-AB12CD\n", missing)), 1)
        self.assertEqual(self.check_file("a.md", "zorblax\n", missing), [])

    def test_hit_names_the_place_but_never_the_term(self):
        hits = self.check_file("a.md", "x\nzorblax\n")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].startswith("private marker (personal term) in public file "))
        self.assertTrue(hits[0].endswith("a.md:2"))
        self.assertNotIn("zorblax", hits[0].lower())

    def test_check_py_is_scanned_too(self):  # it once skipped itself, so its own markers went unseen
        self.assertEqual(len(self.check_file("check.py", "zorblax\nhost " + "LAPTOP" + "-AB12CD\n")), 2)

    def test_check_py_itself_passes(self):  # with the real marker files, where they exist
        with mock.patch.object(check, "public_files", return_value=[ROOT / "tools" / "check.py"]):
            self.assertEqual(check.check_public(), [])

    def test_both_marker_files_are_read(self):
        extra = self.tmp / "denylist.txt"
        extra.write_text("quuxfeld\n", encoding="utf-8-sig")  # a BOM from Windows PowerShell 5.1
        self.assertEqual(check.marker_terms([self.markers, extra, self.tmp / "missing.txt"]),
                         ["zorblax", "blue heron", "测试标记", "quuxfeld"])
        pattern = check.private_markers([self.markers, extra])
        self.assertTrue(pattern.search("QuuxFeld") and pattern.search("zorblax"))

    def test_marker_files_are_the_private_ones(self):
        self.assertEqual(check.MARKER_FILES,
                         (ROOT / "docs" / "private-markers.txt", ROOT / "private" / "denylist.txt"))
        self.assertNotIn(ROOT / "docs" / "private-markers.txt", check.public_files())

    def test_scan_markers_reports_paths_under_root(self):  # as tools/publish_public.py uses it
        path = self.tmp / "sub" / "a.md"
        path.parent.mkdir()
        path.write_text("zorblax\n", encoding="utf-8")
        hits = check.scan_markers([path], check.private_markers([self.markers]), root=self.tmp)
        self.assertEqual(hits, ["private marker (personal term) in public file sub/a.md:1"])


class SecretScanTest(unittest.TestCase):
    def scan_text(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text(text, encoding="utf-8")
            return secret_scan.scan([path], Path(tmp))

    def test_flags_keys_without_printing_value(self):
        fake = "sk-ant-" + "a" * 30
        hits = self.scan_text(f"key = {fake}\n")
        self.assertEqual(hits, ["f.txt:1:anthropic-key"])
        self.assertNotIn(fake, hits[0])

    def test_flags_jwt_and_private_key(self):
        jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
        hits = self.scan_text(jwt + "\n-----BEGIN RSA PRIVATE KEY-----\n")
        self.assertEqual({h.split(":")[2] for h in hits}, {"jwt", "private-key"})

    def test_personal_info_denylist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.md"
            path.write_text("quote from Alice Example\n", encoding="utf-8")
            self.assertEqual(secret_scan.scan([path], Path(tmp), ["alice example"]), ["f.md:1:personal-info"])
            self.assertEqual(secret_scan.scan([path], Path(tmp), []), [])

    def test_env_var_names_are_fine(self):
        self.assertEqual(self.scan_text("export ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY\n"), [])


if __name__ == "__main__":
    unittest.main()
