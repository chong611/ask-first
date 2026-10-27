"""Real corrections collected from use, generalized, as contract tests (DECISIONS Q43, M2).

Every correction in tests/fixtures/corrections.json is classified by what can
check it: a hook (deterministic, tested here), a grader on transcripts, or a
live run (M4). Only the hook rows are executable today.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "hooks"))
import report  # noqa: E402

FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "corrections.json").read_text(encoding="utf-8"))
CHECKS = {"hook-deferral", "hook-policy", "hook-ask", "grader", "live"}


class CorrectionsTest(unittest.TestCase):
    def test_every_correction_is_classified(self):
        self.assertEqual(len({f["id"] for f in FIXTURES}), len(FIXTURES))
        for f in FIXTURES:
            with self.subTest(f["id"]):
                self.assertIn(f["check"], CHECKS)
                self.assertRegex(f["rule"], r"^\d\.\d$")

    def test_hook_rows_have_inputs(self):
        for f in FIXTURES:
            if f["check"].startswith("hook-"):
                with self.subTest(f["id"]):
                    self.assertIn("input", f)

    def test_deferral_corrections_are_detected(self):
        for f in FIXTURES:
            if f["check"] == "hook-deferral":
                with self.subTest(f["id"]):
                    self.assertTrue(report.deferral_hits(f["input"]), f["input"])

    def test_ask_corrections_are_blocked(self):
        for f in FIXTURES:
            if f["check"] == "hook-ask":
                with self.subTest(f["id"]):
                    self.assertTrue(report.ask_check(f["input"], asked=False, interactive=True), f["input"])
                    # Q90A: asking some other question does not cover an item without an asked ID
                    self.assertIn("carry no ID", report.ask_check(f["input"], asked=True, interactive=True,
                                                                  asked_ids={"Q1"}))


if __name__ == "__main__":
    unittest.main()
