#!/usr/bin/env python3
"""Pure-logic unit tests for morris.py.

These exercise the deterministic parts of the tool -- line matching, mutation
application, fence/JSON extraction, discovery filtering, prompt + summary
building, and Unity test insertion -- and need no C compiler or AI backend.

Run with:  python -m unittest test_morris -v   (or: python test_morris.py)
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

# The script is named with a hyphen (morris-minor.py) so it can't be imported by
# name; load it from its path so these tests can reach the module's functions.
# Register it in sys.modules so dataclass/enum module lookups resolve.
_spec = importlib.util.spec_from_file_location(
    "morris_minor", Path(__file__).resolve().parent / "morris-minor.py"
)
morris = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = morris
_spec.loader.exec_module(morris)


class TestFindTargetLine(unittest.TestCase):
    def test_exact(self):
        lines = ["int a(void) {}", "    int x = 1;", "}"]
        self.assertEqual(morris.find_target_line(lines, 2, "    int x = 1;"), 2)

    def test_trimmed(self):
        lines = ["int a(void) {}", "    int x = 1;", "}"]
        self.assertEqual(morris.find_target_line(lines, 2, "int x = 1;"), 2)

    def test_fuzzy(self):
        lines = list("abcdefghij") + ["target", "l"]
        # hint line 3, content actually at line 11 (within +/-10)
        self.assertEqual(morris.find_target_line(lines, 3, "target"), 11)

    def test_not_found(self):
        self.assertIsNone(morris.find_target_line(["a", "b", "c"], 2, "nope"))

    def test_out_of_range(self):
        self.assertIsNone(morris.find_target_line(["a", "b"], 0, "a"))
        self.assertIsNone(morris.find_target_line(["a", "b"], 3, "a"))

    def test_last_line(self):
        self.assertEqual(morris.find_target_line(["a", "b", "c"], 3, "c"), 3)

    def test_fuzzy_boundary(self):
        lines = ["x"] * 21
        lines[10] = "target"  # line 11, exactly 10 above hint 1
        self.assertEqual(morris.find_target_line(lines, 1, "target"), 11)
        lines3 = ["x"] * 25
        lines3[0] = "target"  # line 1, 11 away from hint 12 -> outside window
        self.assertIsNone(morris.find_target_line(lines3, 12, "target"))


class TestApplyMutation(unittest.TestCase):
    def test_apply_ok(self):
        text = "int a(void)\n{\n    return 1;\n}\n"
        mutated, err = morris.apply_mutation_to_text(
            text, 3, "    return 1;", "    return 0;"
        )
        self.assertIsNone(err)
        self.assertIn("return 0;", mutated)
        self.assertTrue(mutated.endswith("\n"))

    def test_apply_mismatch(self):
        text = "int a(void)\n{\n    return 1;\n}\n"
        mutated, err = morris.apply_mutation_to_text(
            text, 3, "    return 999;", "    return 0;"
        )
        self.assertIsNone(mutated)
        self.assertIn("return 1;", err)

    def test_preserves_no_trailing_newline(self):
        text = "a\nb\nc"
        mutated, err = morris.apply_mutation_to_text(text, 2, "b", "B")
        self.assertIsNone(err)
        self.assertEqual(mutated, "a\nB\nc")


class TestFenceAndJson(unittest.TestCase):
    def test_strip_plain(self):
        self.assertEqual(morris.strip_code_fences('{"a":1}'), '{"a":1}')

    def test_strip_json_fence(self):
        self.assertEqual(
            morris.strip_code_fences('```json\n{"a":1}\n```'), '{"a":1}'
        )

    def test_strip_bare_fence(self):
        self.assertEqual(morris.strip_code_fences("```\nhello\n```"), "hello")

    def test_extract_json_with_prose(self):
        text = 'Sure! Here it is:\n```json\n{"mutations": []}\n```\nHope that helps.'
        self.assertEqual(json.loads(morris.extract_json_object(text)), {"mutations": []})

    def test_extract_json_no_fence(self):
        text = 'blah {"x": 2} blah'
        self.assertEqual(json.loads(morris.extract_json_object(text)), {"x": 2})


class TestMutationModel(unittest.TestCase):
    def test_from_dict(self):
        m = morris.Mutation.from_dict(
            {
                "file_path": "Core/Music/arp.c",
                "line_number": "42",
                "original_line": "a",
                "mutated_line": "b",
            }
        )
        self.assertEqual(m.line_number, 42)
        self.assertEqual(m.description, "")


class TestPromptsAndSummary(unittest.TestCase):
    def test_mutation_prompt_contains_count_and_source(self):
        p = morris.build_mutation_prompt("=== Core/x.c ===\n   1| int x;", "5-8")
        self.assertIn("5-8", p)
        self.assertIn("Core/x.c", p)
        self.assertIn("JSON", p)

    def test_results_summary(self):
        results = [
            morris.MutationResult(
                morris.Mutation("Core/a.c", 10, "x > 0", "x >= 0", "boundary"),
                morris.Outcome.SURVIVED,
            ),
            morris.MutationResult(
                morris.Mutation("Core/b.c", 20, "y + 1", "y - 1", "arith"),
                morris.Outcome.KILLED,
            ),
        ]
        s = morris.format_results_summary(results)
        self.assertIn("Core/a.c:10 [SURVIVED]", s)
        self.assertIn("Core/b.c:20 [KILLED]", s)
        self.assertIn("x > 0 -> x >= 0", s)

    def test_analysis_prompt_auto_vs_explain(self):
        auto = morris.build_analysis_prompt(True, "sum", "src", "tests")
        explain = morris.build_analysis_prompt(False, "sum", "src", "")
        self.assertIn("additions", auto)
        self.assertIn("RUN_TEST", auto)
        self.assertIn("explain", explain.lower())


class TestReadAllSources(unittest.TestCase):
    def test_line_numbers(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            (proj / "Core").mkdir()
            f = proj / "Core" / "x.c"
            f.write_text("one\ntwo\nthree\n", encoding="utf-8")
            out = morris.read_all_sources(proj, [f])
            self.assertIn("=== ", out)
            self.assertIn("x.c", out)
            self.assertIn("   1| one", out)
            self.assertIn("   2| two", out)
            self.assertIn("   3| three", out)
            self.assertNotIn("   0| ", out)


class TestDiscovery(unittest.TestCase):
    def test_compile_db_filters_to_source_root(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            (proj / "Core" / "Music").mkdir(parents=True)
            (proj / "test" / "unity").mkdir(parents=True)
            (proj / "test" / "support").mkdir(parents=True)
            arp = proj / "Core" / "Music" / "arp.c"
            arp.write_text("// arp", encoding="utf-8")
            for p in [
                proj / "test" / "test_arp.c",
                proj / "test" / "unity" / "unity.c",
                proj / "test" / "support" / "ff_fake.c",
            ]:
                p.write_text("// t", encoding="utf-8")
            build = proj / "test" / "build"
            build.mkdir()
            db = [
                {"directory": str(build), "file": str(arp), "command": "cc"},
                {"directory": str(build), "file": str(proj / "test" / "test_arp.c"), "command": "cc"},
                {"directory": str(build), "file": str(proj / "test" / "unity" / "unity.c"), "command": "cc"},
                {"directory": str(build), "file": str(proj / "test" / "support" / "ff_fake.c"), "command": "cc"},
            ]
            (build / "compile_commands.json").write_text(json.dumps(db), encoding="utf-8")

            found = morris.discover_from_compile_db(
                build, proj / "Core", proj / "test"
            )
            self.assertEqual([p.name for p in found], ["arp.c"])


class TestInsertUnityTests(unittest.TestCase):
    SRC = (
        "#include \"unity.h\"\n"
        "void setUp(void) {}\n"
        "void tearDown(void) {}\n"
        "static void test_existing(void) { TEST_ASSERT_TRUE(1); }\n"
        "int main(void)\n"
        "{\n"
        "    UNITY_BEGIN();\n"
        "    RUN_TEST(test_existing);\n"
        "    return UNITY_END();\n"
        "}\n"
    )

    def test_inserts_function_and_runner(self):
        patched = morris.insert_unity_tests(
            self.SRC,
            ["static void test_new(void) { TEST_ASSERT_TRUE(1); }"],
            ["test_new"],
        )
        self.assertIsNotNone(patched)
        # function inserted before main
        self.assertLess(patched.index("test_new(void)"), patched.index("int main("))
        # runner inserted after UNITY_BEGIN
        self.assertIn("RUN_TEST(test_new);", patched)
        self.assertLess(
            patched.index("UNITY_BEGIN()"), patched.index("RUN_TEST(test_new);")
        )
        # existing content untouched
        self.assertIn("RUN_TEST(test_existing);", patched)

    def test_missing_anchors_returns_none(self):
        self.assertIsNone(
            morris.insert_unity_tests("int notmain(void){}", ["x"], ["x"])
        )


class _StubRunner:
    """Stands in for Runner; records the file's on-disk state during the build."""

    def __init__(self, abs_path, outcome=None):
        self.abs_path = abs_path
        self.outcome = outcome or morris.Outcome.SURVIVED
        self.seen_during_build = None
        self.backup_existed_during_build = None

    def build_and_test(self, timeout):
        self.seen_during_build = self.abs_path.read_text(encoding="utf-8")
        self.backup_existed_during_build = morris.backup_path_for(
            self.abs_path
        ).exists()
        return self.outcome, "stub"


class TestLineMutationLifecycle(unittest.TestCase):
    def _mk(self, d, text):
        f = Path(d) / "arp.c"
        f.write_text(text, encoding="utf-8")
        return f

    def test_mutates_during_build_then_restores_and_removes_backup(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = self._mk(d, "int f(void)\n{\n    return 1;\n}\n")
            mut = morris.Mutation("arp.c", 3, "    return 1;", "    return 0;", "x")
            runner = _StubRunner(f, morris.Outcome.KILLED)
            outcome, _ = morris.test_line_mutation(runner, f, mut, 5.0)

            self.assertEqual(outcome, morris.Outcome.KILLED)
            # The build saw the mutated content, with a backup present.
            self.assertIn("return 0;", runner.seen_during_build)
            self.assertTrue(runner.backup_existed_during_build)
            # Afterwards the source is pristine and no backup remains.
            self.assertEqual(f.read_text(encoding="utf-8"), "int f(void)\n{\n    return 1;\n}\n")
            self.assertFalse(morris.backup_path_for(f).exists())

    def test_restores_even_if_build_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = self._mk(d, "int f(void)\n{\n    return 1;\n}\n")
            mut = morris.Mutation("arp.c", 3, "    return 1;", "    return 0;", "x")

            class Boom(_StubRunner):
                def build_and_test(self, timeout):
                    raise RuntimeError("build blew up")

            with self.assertRaises(RuntimeError):
                morris.test_line_mutation(Boom(f), f, mut, 5.0)
            # finally-block must still have restored the file and cleaned up.
            self.assertEqual(f.read_text(encoding="utf-8"), "int f(void)\n{\n    return 1;\n}\n")
            self.assertFalse(morris.backup_path_for(f).exists())

    def test_noop_mutation_flagged_without_building(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = self._mk(d, "int f(void)\n{\n    return 1;\n}\n")
            mut = morris.Mutation("arp.c", 3, "    return 1;", "    return 1;", "noop")
            runner = _StubRunner(f)
            outcome, detail = morris.test_line_mutation(runner, f, mut, 5.0)
            self.assertEqual(outcome, morris.Outcome.LINE_MISMATCH)
            self.assertIn("no-op", detail)
            self.assertIsNone(runner.seen_during_build)  # never built


class TestModelSelection(unittest.TestCase):
    def test_cli_models(self):
        self.assertEqual(morris.model_for("cli", False), "sonnet")
        self.assertEqual(morris.model_for("cli", True), "haiku")

    def test_api_models(self):
        self.assertEqual(morris.model_for("api", False), "claude-sonnet-4-6")
        self.assertEqual(
            morris.model_for("api", True), "claude-haiku-4-5-20251001"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
