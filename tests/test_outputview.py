"""Testy widoków wyjścia i ekstraktorów błędów."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdbridge.extractors import extract, normalize_path  # noqa: E402
from cmdbridge.outputview import (  # noqa: E402
    HEADER_END,
    HEADER_SENTINEL,
    ViewError,
    clean_line,
    iter_lines,
    render,
)

GRADLE_FAIL_LOG = """> Configure project :app
> Task :app:preBuild UP-TO-DATE
> Task :app:compileDebugKotlin FAILED
w: file:///C:/Projekt/app/src/main/java/com/example/Old.kt:8:13 Variable 'x' is never used
e: file:///C:/Projekt/app/src/main/java/com/example/MainActivity.kt:42:17 Unresolved reference: bindig
e: file:///C:/Projekt/app/src/main/java/com/example/MainActivity.kt:57:9 Type mismatch: inferred type is String but Int was expected

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':app:compileDebugKotlin'.
> Compilation error. See log for more details

* Try:
> Run with --stacktrace option to get the stack trace.

BUILD FAILED in 1m 12s
14 actionable tasks: 3 executed, 11 up-to-date
"""

GRADLE_OK_LOG = """> Task :app:preBuild UP-TO-DATE
> Task :app:mergeDebugResources
> Task :app:packageDebug
Built /home/user/Projekt/app/build/outputs/apk/debug/app-debug.apk

BUILD SUCCESSFUL in 43s
32 actionable tasks: 12 executed, 20 up-to-date
"""


def make_source(text: str) -> dict:
    return {"text": text}


class CleanLineTestCase(unittest.TestCase):
    def test_strips_ansi(self) -> None:
        self.assertEqual(clean_line("\x1b[31mbłąd\x1b[0m"), "błąd")

    def test_collapses_progress_bar(self) -> None:
        self.assertEqual(clean_line("10%\r50%\r100% gotowe"), "100% gotowe")

    def test_keeps_last_nonempty_segment(self) -> None:
        self.assertEqual(clean_line("wynik\r"), "wynik")


class IterLinesTestCase(unittest.TestCase):
    def test_skips_log_header(self) -> None:
        text = f"{HEADER_SENTINEL}\n# command : dir\n{HEADER_END}\nprawdziwa linia\n"
        lines = [line for _, line in iter_lines(make_source(text))]
        self.assertEqual(lines, ["prawdziwa linia"])

    def test_skips_marker_lines(self) -> None:
        text = "a\n__CMDBRIDGE__deadbeef 0 /tmp\nb\n"
        numbered = list(iter_lines(make_source(text)))
        self.assertEqual(numbered, [(1, "a"), (2, "b")])

    def test_reads_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.txt"
            path.write_text("x\ny\n", encoding="utf-8")
            lines = [line for _, line in iter_lines({"path": str(path)})]
        self.assertEqual(lines, ["x", "y"])


class ViewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.big = make_source("".join(f"linia-{i}\n" for i in range(1, 1001)))

    def test_full_keeps_short_output_intact(self) -> None:
        view = render(make_source("a\nb\n"), {"view": "full"})
        self.assertEqual(view["text"], "a\nb")
        self.assertFalse(view["truncated"])
        self.assertEqual(view["lines_total"], 2)

    def test_full_truncates_long_output(self) -> None:
        view = render(self.big, {"view": "full", "max_chars": 500})
        self.assertTrue(view["truncated"])
        self.assertLess(len(view["text"]), 900)
        self.assertIn("linia-1", view["text"])
        self.assertIn("linia-1000", view["text"])
        self.assertEqual(view["lines_total"], 1000)

    def test_tail(self) -> None:
        view = render(self.big, {"view": "tail", "lines": 3})
        self.assertEqual(view["text"].split("\n"), ["linia-998", "linia-999", "linia-1000"])
        self.assertEqual(view["lines_total"], 1000)
        self.assertEqual(view["first_line_shown"], 998)

    def test_head(self) -> None:
        view = render(self.big, {"view": "head", "lines": 2})
        self.assertEqual(view["text"].split("\n"), ["linia-1", "linia-2"])

    def test_grep_returns_only_matches_with_line_numbers(self) -> None:
        view = render(self.big, {"view": "grep", "pattern": r"linia-99$"})
        self.assertEqual(view["matches"], 1)
        self.assertEqual(view["text"], "99: linia-99")

    def test_grep_with_context(self) -> None:
        view = render(self.big, {"view": "grep", "pattern": r"^linia-500$", "context": 1})
        self.assertEqual(view["text"].split("\n"),
                         ["499: linia-499", "500: linia-500", "501: linia-501"])

    def test_grep_counts_matches_beyond_limit(self) -> None:
        view = render(self.big, {"view": "grep", "pattern": "linia", "lines": 5})
        self.assertEqual(view["matches"], 1000)
        self.assertEqual(view["lines_shown"], 5)
        self.assertTrue(view["truncated"])

    def test_grep_requires_pattern(self) -> None:
        with self.assertRaises(ViewError):
            render(self.big, {"view": "grep"})

    def test_grep_rejects_broken_regex(self) -> None:
        with self.assertRaises(ViewError):
            render(self.big, {"view": "grep", "pattern": "[niedomkniete"})

    def test_around_shows_neighbourhood(self) -> None:
        view = render(self.big, {"view": "around", "line": 500, "context": 2})
        self.assertEqual(view["lines_shown"], 5)
        self.assertTrue(view["text"].startswith("498: linia-498"))
        self.assertTrue(view["text"].endswith("502: linia-502"))

    def test_quiet_returns_no_text(self) -> None:
        view = render(self.big, {"view": "quiet"})
        self.assertEqual(view["text"], "")
        self.assertEqual(view["lines_total"], 1000)

    def test_unknown_view_rejected(self) -> None:
        with self.assertRaises(ViewError):
            render(self.big, {"view": "teleportacja"})

    def test_large_log_uses_constant_memory(self) -> None:
        """Widok tail na dużym pliku nie ładuje go w całości do pamięci."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.log"
            with path.open("w", encoding="utf-8") as handle:
                for i in range(200_000):
                    handle.write(f"linia numer {i} z jakąś treścią wypełniającą\n")
            view = render({"path": str(path)}, {"view": "tail", "lines": 5})
        self.assertEqual(view["lines_total"], 200_000)
        self.assertEqual(view["lines_shown"], 5)
        self.assertTrue(view["text"].endswith("linia numer 199999 z jakąś treścią wypełniającą"))
        self.assertLess(len(view["text"]), 500)


class ExtractGradleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.failed = extract(make_source(GRADLE_FAIL_LOG))
        self.ok = extract(make_source(GRADLE_OK_LOG))

    def test_detects_failed_build(self) -> None:
        self.assertEqual(self.failed["verdict"], "failed")
        self.assertEqual(self.failed["status_line"], "BUILD FAILED in 1m 12s")

    def test_detects_successful_build(self) -> None:
        self.assertEqual(self.ok["verdict"], "ok")
        self.assertEqual(self.ok["status_line"], "BUILD SUCCESSFUL in 43s")
        self.assertEqual(self.ok["findings"], [])

    def test_extracts_kotlin_errors_with_file_and_line(self) -> None:
        errors = [f for f in self.failed["findings"] if f["severity"] == "error"]
        kotlin = [f for f in errors if f.get("file", "").endswith("MainActivity.kt")]
        self.assertEqual(kotlin[0]["file"], "C:/Projekt/app/src/main/java/com/example/MainActivity.kt")
        self.assertEqual(len(kotlin), 2)
        self.assertEqual(kotlin[0]["line"], 42)
        self.assertIn("Unresolved reference", kotlin[0]["message"])

    def test_extracts_failed_task(self) -> None:
        messages = [f["message"] for f in self.failed["findings"]]
        self.assertIn("Task :app:compileDebugKotlin FAILED", messages)

    def test_extracts_what_went_wrong_block(self) -> None:
        blocks = [f for f in self.failed["findings"] if f["rule"] == "gradle-what-went-wrong"]
        self.assertEqual(len(blocks), 1)
        detail = " ".join(blocks[0]["detail"])
        self.assertIn("Execution failed for task ':app:compileDebugKotlin'", detail)
        self.assertNotIn("--stacktrace", detail)  # blok kończy się na '* Try:'

    def test_counts_warnings_separately(self) -> None:
        self.assertEqual(self.failed["counts"].get("warning"), 1)
        self.assertGreaterEqual(self.failed["counts"].get("error", 0), 3)

    def test_findings_carry_log_line_numbers(self) -> None:
        for finding in self.failed["findings"]:
            self.assertGreater(finding["log_line"], 0)

    def test_around_can_follow_log_line(self) -> None:
        finding = next(f for f in self.failed["findings"] if f.get("line") == 42)
        view = render(make_source(GRADLE_FAIL_LOG),
                      {"view": "around", "line": finding["log_line"], "context": 1})
        self.assertIn("Unresolved reference", view["text"])

    def test_finds_apk_artifact(self) -> None:
        self.assertIn(
            "/home/user/Projekt/app/build/outputs/apk/debug/app-debug.apk",
            self.ok.get("artifacts", []),
        )

    def test_rendered_text_is_compact(self) -> None:
        self.assertLess(len(self.failed["text"]), len(GRADLE_FAIL_LOG))
        self.assertIn("BUILD FAILED", self.failed["text"])
        self.assertIn("Unresolved reference", self.failed["text"])


class NormalizePathTestCase(unittest.TestCase):
    """Ścieżki z komunikatów mają być otwieralne, a nie tylko ładne."""

    def test_windows_file_url(self) -> None:
        self.assertEqual(normalize_path("file:///C:/Projekt/A.kt"), "C:/Projekt/A.kt")

    def test_posix_file_url(self) -> None:
        self.assertEqual(normalize_path("file:///home/u/a.kt"), "/home/u/a.kt")

    def test_plain_path_untouched(self) -> None:
        self.assertEqual(normalize_path("app/src/Main.java"), "app/src/Main.java")

    def test_empty(self) -> None:
        self.assertIsNone(normalize_path(""))


class ExtractOtherToolchainsTestCase(unittest.TestCase):
    def test_python_traceback(self) -> None:
        log = (
            "start\n"
            "Traceback (most recent call last):\n"
            '  File "app.py", line 3, in <module>\n'
            "    main()\n"
            "ValueError: zła wartość\n"
        )
        result = extract(make_source(log))
        self.assertEqual(result["verdict"], "failed")
        detail = " ".join(result["findings"][0]["detail"])
        self.assertIn("ValueError: zła wartość", detail)

    def test_javac_error(self) -> None:
        log = "app/src/Main.java:17: error: cannot find symbol\n"
        finding = extract(make_source(log))["findings"][0]
        self.assertEqual(finding["file"], "app/src/Main.java")
        self.assertEqual(finding["line"], 17)
        self.assertIn("cannot find symbol", finding["message"])

    def test_aapt_error(self) -> None:
        log = "ERROR:/p/app/src/main/res/layout/a.xml:5: AAPT: error: resource not found\n"
        result = extract(make_source(log))
        self.assertTrue(any("resource not found" in f["message"] for f in result["findings"]))

    def test_npm_error(self) -> None:
        log = "npm ERR! code ELIFECYCLE\nnpm ERR! build nie powiódł się\n"
        result = extract(make_source(log))
        self.assertEqual(len(result["findings"]), 2)

    def test_generic_fallback_when_nothing_recognised(self) -> None:
        log = "wszystko ok\ncoś się zepsuło: Error podczas kopiowania\nkoniec\n"
        result = extract(make_source(log))
        self.assertTrue(result["used_generic_rules"])
        self.assertIn("Error podczas kopiowania", result["findings"][0]["message"])

    def test_clean_log_reports_tail_instead_of_guessing(self) -> None:
        log = "".join(f"krok {i}\n" for i in range(30))
        result = extract(make_source(log))
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["verdict"], "unknown")
        self.assertIn("krok 29", result["text"])

    def test_duplicate_errors_are_counted_once(self) -> None:
        log = "e: file:///a/B.kt:1:1 Ten sam błąd\n" * 5
        result = extract(make_source(log))
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["count"], 5)

    def test_findings_are_capped(self) -> None:
        log = "".join(f"e: file:///a/B.kt:{i}:1 Błąd numer {i}\n" for i in range(1, 51))
        result = extract(make_source(log), {"max_findings": 5})
        self.assertEqual(len(result["findings"]), 5)
        self.assertEqual(result["findings_total"], 50)
        self.assertTrue(result["truncated"])
        self.assertIn("jeszcze 45", result["text"])


if __name__ == "__main__":
    unittest.main()
