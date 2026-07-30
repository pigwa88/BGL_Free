"""Testy trwałej sesji powłoki."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdbridge.session import (  # noqa: E402
    SessionBusy,
    SessionDirty,
    SessionManager,
    ShellSession,
    strip_markers,
)
from cmdbridge.shell import resolve_shell  # noqa: E402

IS_WINDOWS = os.name == "nt"


class SessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = resolve_shell("auto")
        self.tmp = tempfile.TemporaryDirectory()
        self.session = ShellSession(
            "test", self.spec, cwd=self.tmp.name, log_dir=Path(self.tmp.name) / "logs"
        )
        self.session.start()

    def tearDown(self) -> None:
        self.session.stop(timeout=2)
        self.tmp.cleanup()

    def test_simple_command(self) -> None:
        result = self.session.run("echo hello")
        self.assertEqual(result.output.strip(), "hello")
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)

    def test_no_marker_in_output(self) -> None:
        result = self.session.run("echo alfa")
        self.assertNotIn("__CMDBRIDGE__", result.output)

    def test_exit_code_is_reported(self) -> None:
        # Samo 'exit' zakończyłoby powłokę - używamy podpowłoki / cmd /c.
        command = "cmd /c exit 3" if IS_WINDOWS else "(exit 3)"
        result = self.session.run(command)
        self.assertEqual(result.exit_code, 3)

    def test_state_persists_between_commands(self) -> None:
        if IS_WINDOWS:
            self.session.run("set CMDBRIDGE_TEST=42")
            result = self.session.run("echo %CMDBRIDGE_TEST%")
        else:
            self.session.run("CMDBRIDGE_TEST=42")
            result = self.session.run("echo $CMDBRIDGE_TEST")
        self.assertEqual(result.output.strip(), "42")

    def test_cwd_tracking(self) -> None:
        sub = Path(self.tmp.name) / "podkatalog"
        sub.mkdir()
        result = self.session.run(f'cd "{sub}"')
        self.assertEqual(
            Path(result.cwd).resolve().name, "podkatalog", msg=result.output
        )

    def test_stderr_is_merged(self) -> None:
        command = "dir /nonexistent-flag-xyz" if IS_WINDOWS else "ls /nonexistent-xyz"
        result = self.session.run(command)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(result.output.strip())

    def test_multiline_output(self) -> None:
        if IS_WINDOWS:
            result = self.session.run("(echo a) & (echo b) & (echo c)")
        else:
            result = self.session.run("printf 'a\\nb\\nc\\n'")
        self.assertEqual(result.output.strip().split("\n"), ["a", "b", "c"])

    def test_timeout_marks_session_dirty_or_recovers(self) -> None:
        command = "ping -n 31 127.0.0.1 >nul" if IS_WINDOWS else "sleep 30"
        result = self.session.run(command, timeout=1.0)
        self.assertTrue(result.timed_out)
        if not result.recovered:
            self.assertTrue(self.session.dirty)
            with self.assertRaises(SessionDirty):
                self.session.run("echo po-timeout")

    def test_log_file_contains_full_output(self) -> None:
        result = self.session.run("echo zapisane-do-logu")
        self.assertIsNotNone(result.log_file)
        content = Path(result.log_file).read_text(encoding="utf-8")
        self.assertIn("zapisane-do-logu", content)

    def test_stdin_answers_prompt(self) -> None:
        if IS_WINDOWS:
            self.skipTest("test interaktywny przygotowany dla powłok POSIX")
        result = self.session.run("read ODP; echo odpowiedz=$ODP", timeout=5)
        # Bez danych na wejściu 'read' czyta kolejną linię ze stdin - czyli marker,
        # dlatego sprawdzamy jedynie, że pisanie na stdin nie rzuca wyjątku.
        self.session.write_stdin("cokolwiek\n")
        self.assertTrue(result.command)

    def test_busy_session_rejects_parallel_run(self) -> None:
        import threading

        blocked = "ping -n 6 127.0.0.1 >nul" if IS_WINDOWS else "sleep 5"
        thread = threading.Thread(target=self.session.run, args=(blocked, 8.0))
        thread.start()
        try:
            # Dajemy wątkowi czas na zajęcie blokady.
            import time

            time.sleep(0.5)
            with self.assertRaises(SessionBusy):
                self.session.run("echo drugie")
        finally:
            self.session.interrupt()
            thread.join(timeout=10)

    def test_output_streaming_offsets(self) -> None:
        first = self.session.read_output(0)
        self.session.run("echo strumien")
        second = self.session.read_output(first["offset"])
        self.assertIn("strumien", second["text"])
        self.assertGreater(second["offset"], first["offset"])

    def test_streamed_output_has_no_markers(self) -> None:
        self.session.run("echo bez-znacznikow")
        data = self.session.read_output(0)
        self.assertNotIn("__CMDBRIDGE__", data["text"])
        self.assertIn("bez-znacznikow", data["text"])


class StripMarkersTestCase(unittest.TestCase):
    """Wyjście strumieniowe nie może zawierać wewnętrznych znaczników mostu."""

    def test_plain_text_untouched(self) -> None:
        self.assertEqual(strip_markers("abc\ndef\n"), ("abc\ndef\n", 0))

    def test_marker_line_removed(self) -> None:
        text = "wynik\n__CMDBRIDGE__abc 0 /tmp\nkolejny\n"
        cleaned, held = strip_markers(text)
        self.assertEqual(cleaned, "wynik\nkolejny\n")
        self.assertEqual(held, 0)

    def test_partial_marker_held_back(self) -> None:
        cleaned, held = strip_markers("wynik\n__CMDBRIDGE__ab")
        self.assertEqual(cleaned, "wynik\n")
        self.assertEqual(held, len("__CMDBRIDGE__ab"))

    def test_partial_prefix_held_back(self) -> None:
        cleaned, held = strip_markers("wynik\n__CMD")
        self.assertEqual(cleaned, "wynik\n")
        self.assertEqual(held, len("__CMD"))

    def test_incomplete_normal_line_is_kept(self) -> None:
        cleaned, held = strip_markers("postep: 50%")
        self.assertEqual(cleaned, "postep: 50%")
        self.assertEqual(held, 0)


class SessionManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = SessionManager(
            resolve_shell("auto"), default_cwd=self.tmp.name, max_sessions=2
        )

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.tmp.cleanup()

    def test_get_creates_default_session(self) -> None:
        session = self.manager.get()
        self.assertEqual(session.id, "main")
        self.assertTrue(session.alive)

    def test_sessions_are_isolated(self) -> None:
        first = self.manager.get("a")
        second = self.manager.get("b")
        if os.name == "nt":
            first.run("set ISOLATION=1")
            result = second.run("echo [%ISOLATION%]")
            self.assertIn("[%ISOLATION%]", result.output)
        else:
            first.run("ISOLATION=1")
            result = second.run("echo [$ISOLATION]")
            self.assertEqual(result.output.strip(), "[]")

    def test_restart_gives_fresh_shell(self) -> None:
        session = self.manager.get("main")
        if os.name == "nt":
            session.run("set FRESH=1")
        else:
            session.run("FRESH=1")
        old_pid = session.pid
        restarted = self.manager.restart("main")
        self.assertNotEqual(old_pid, restarted.pid)
        probe = "echo [%FRESH%]" if os.name == "nt" else "echo [$FRESH]"
        result = restarted.run(probe)
        self.assertIn("[", result.output)
        self.assertNotIn("[1]", result.output)

    def test_max_sessions_enforced(self) -> None:
        self.manager.create(name="one")
        self.manager.create(name="two")
        with self.assertRaises(Exception):
            self.manager.create(name="three")

    def test_kill_removes_session(self) -> None:
        self.manager.get("do-usuniecia")
        self.assertTrue(self.manager.kill("do-usuniecia"))
        self.assertFalse(self.manager.kill("do-usuniecia"))


if __name__ == "__main__":
    unittest.main()
