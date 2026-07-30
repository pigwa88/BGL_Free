"""Testy trybu plikowego (mailbox) i dyspozytora operacji."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdbridge.core import Bridge  # noqa: E402
from cmdbridge.mailbox import Mailbox  # noqa: E402
from cmdbridge.policy import Policy  # noqa: E402
from cmdbridge.session import SessionManager  # noqa: E402
from cmdbridge.shell import resolve_shell  # noqa: E402

IS_WINDOWS = os.name == "nt"


class MailboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = SessionManager(resolve_shell("auto"), default_cwd=self.tmp.name)
        self.bridge = Bridge(self.manager, Policy())
        self.mailbox = Mailbox(self.bridge, Path(self.tmp.name) / "mailbox")

    def tearDown(self) -> None:
        self.mailbox.stop()
        self.bridge.shutdown()
        self.tmp.cleanup()

    def _put(self, name: str, payload: dict) -> Path:
        path = self.mailbox.inbox / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _response(self, name: str) -> dict:
        return json.loads((self.mailbox.outbox / name).read_text(encoding="utf-8"))

    def test_run_request(self) -> None:
        self._put("01.json", {"op": "run", "command": "echo plikowe"})
        self.assertEqual(self.mailbox.process_pending(), 1)
        data = self._response("01.json")
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], 200)
        self.assertIn("plikowe", data["output"])
        self.assertEqual(data["request_file"], "01.json")

    def test_op_defaults_to_run_when_command_present(self) -> None:
        self._put("02.json", {"command": "echo bez-op"})
        self.mailbox.process_pending()
        data = self._response("02.json")
        self.assertEqual(data["op"], "run")
        self.assertIn("bez-op", data["output"])

    def test_request_moved_to_done(self) -> None:
        path = self._put("03.json", {"command": "echo x"})
        self.mailbox.process_pending()
        self.assertFalse(path.exists())
        self.assertTrue((self.mailbox.done / "03.json").exists())

    def test_repeated_name_does_not_crash(self) -> None:
        for _ in range(3):
            self._put("04.json", {"command": "echo powtorka"})
            self.mailbox.process_pending()
        self.assertTrue(self._response("04.json")["ok"])

    def test_broken_json_gets_error_response(self) -> None:
        (self.mailbox.inbox / "05.json").write_text("{to nie jest json", encoding="utf-8")
        self.mailbox.process_pending()
        data = self._response("05.json")
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], 400)
        self.assertIn("hint", data)

    def test_blocked_command_reported(self) -> None:
        self._put("06.json", {"command": "format C:"})
        self.mailbox.process_pending()
        data = self._response("06.json")
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], 403)
        self.assertEqual(data["rule"], "format-drive")

    def test_session_operations(self) -> None:
        self._put("07.json", {"op": "new", "session": "plikowa"})
        self.mailbox.process_pending()
        self.assertEqual(self._response("07.json")["status"], 201)

        setter = "set MBOX=1" if IS_WINDOWS else "MBOX=1"
        probe = "echo [%MBOX%]" if IS_WINDOWS else "echo [$MBOX]"
        self._put("08.json", {"command": setter, "session": "plikowa"})
        self.mailbox.process_pending()

        self._put("09.json", {"command": probe, "session": "plikowa"})
        self.mailbox.process_pending()
        self.assertIn("[1]", self._response("09.json")["output"])

        self._put("10.json", {"op": "restart", "session": "plikowa"})
        self.mailbox.process_pending()
        self.assertTrue(self._response("10.json")["ok"])

        self._put("11.json", {"command": probe, "session": "plikowa"})
        self.mailbox.process_pending()
        self.assertNotIn("[1]", self._response("11.json")["output"])

        self._put("12.json", {"op": "kill", "session": "plikowa"})
        self.mailbox.process_pending()
        self.assertTrue(self._response("12.json")["ok"])

    def test_unknown_op(self) -> None:
        self._put("13.json", {"op": "cokolwiek"})
        self.mailbox.process_pending()
        data = self._response("13.json")
        self.assertEqual(data["status"], 400)
        self.assertIn("Dostępne", data["hint"])

    def test_health_op(self) -> None:
        self._put("14.json", {"op": "health"})
        self.mailbox.process_pending()
        self.assertTrue(self._response("14.json")["ok"])

    def test_background_watcher(self) -> None:
        self.mailbox.start()
        self._put("15.json", {"command": "echo w-tle"})
        deadline = time.time() + 10
        target = self.mailbox.outbox / "15.json"
        while time.time() < deadline and not target.exists():
            time.sleep(0.1)
        self.assertTrue(target.exists())
        self.assertIn("w-tle", self._response("15.json")["output"])


if __name__ == "__main__":
    unittest.main()
