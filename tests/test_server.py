"""Testy API HTTP mostu."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdbridge.core import Bridge, truncate  # noqa: E402
from cmdbridge.policy import Policy  # noqa: E402
from cmdbridge.server import BridgeServer  # noqa: E402
from cmdbridge.session import SessionManager  # noqa: E402
from cmdbridge.shell import resolve_shell  # noqa: E402

IS_WINDOWS = os.name == "nt"
TOKEN = "test-token"


def call(url, path, method="GET", body=None, token=TOKEN, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Bridge-Token", token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.manager = SessionManager(
            resolve_shell("auto"),
            default_cwd=cls.tmp.name,
            log_dir=Path(cls.tmp.name) / "logs",
        )
        cls.bridge = Bridge(cls.manager, Policy(), max_output=200)
        cls.server = BridgeServer(cls.bridge, "127.0.0.1", 0, TOKEN)
        cls.server.start()
        cls.url = cls.server.url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.bridge.shutdown()
        cls.tmp.cleanup()

    # ------------------------------------------------------------------ health

    def test_health_is_public(self) -> None:
        status, data = call(self.url, "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("shell", data)

    def test_token_required(self) -> None:
        status, data = call(self.url, "/run", "POST", {"command": "echo x"}, token=None)
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_wrong_token_rejected(self) -> None:
        status, _ = call(self.url, "/sessions", token="zle")
        self.assertEqual(status, 401)

    # --------------------------------------------------------------------- run

    def test_run_command(self) -> None:
        status, data = call(self.url, "/run", "POST", {"command": "echo witaj"})
        self.assertEqual(status, 200)
        self.assertEqual(data["exit_code"], 0)
        self.assertIn("witaj", data["output"])
        self.assertEqual(data["session"], "main")

    def test_run_creates_named_session_on_demand(self) -> None:
        status, data = call(
            self.url, "/run", "POST", {"command": "echo x", "session": "auto1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["session"], "auto1")

    def test_missing_command_is_400(self) -> None:
        status, data = call(self.url, "/run", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("command", data["error"])

    def test_invalid_json_is_400(self) -> None:
        req = urllib.request.Request(
            self.url + "/run", data=b"{nie-json", method="POST"
        )
        req.add_header("X-Bridge-Token", TOKEN)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_dangerous_command_blocked(self) -> None:
        status, data = call(self.url, "/run", "POST", {"command": "format C:"})
        self.assertEqual(status, 403)
        self.assertEqual(data["rule"], "format-drive")
        self.assertIn("hint", data)

    def test_output_truncation_and_log_file(self) -> None:
        command = (
            'for /l %i in (1,1,400) do @echo linia-%i'
            if IS_WINDOWS
            else "for i in $(seq 1 400); do echo linia-$i; done"
        )
        status, data = call(self.url, "/run", "POST", {"command": command})
        self.assertEqual(status, 200)
        self.assertTrue(data["truncated"])
        self.assertLess(len(data["output"]), 600)
        full = Path(data["log_file"]).read_text(encoding="utf-8")
        self.assertIn("linia-400", full)

    def test_timeout_reports_hint(self) -> None:
        command = "ping -n 21 127.0.0.1 >nul" if IS_WINDOWS else "sleep 20"
        status, data = call(
            self.url,
            "/run",
            "POST",
            {"command": command, "session": "timeouty", "timeout": 1},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["timed_out"])
        self.assertIn("hint", data)
        call(self.url, "/sessions/timeouty", "DELETE")

    # ---------------------------------------------------------------- sesje

    def test_session_lifecycle(self) -> None:
        status, data = call(self.url, "/sessions", "POST", {"session": "zycie"})
        self.assertEqual(status, 201)
        self.assertEqual(data["session"]["id"], "zycie")

        status, data = call(self.url, "/sessions")
        self.assertIn("zycie", [s["id"] for s in data["sessions"]])

        status, data = call(self.url, "/sessions/zycie/restart", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, data = call(self.url, "/sessions/zycie", "DELETE")
        self.assertEqual(status, 200)

        status, data = call(self.url, "/sessions/zycie", "DELETE")
        self.assertEqual(status, 404)

    def test_duplicate_session_is_409(self) -> None:
        call(self.url, "/sessions", "POST", {"session": "duplikat"})
        status, data = call(self.url, "/sessions", "POST", {"session": "duplikat"})
        self.assertEqual(status, 409)
        call(self.url, "/sessions/duplikat", "DELETE")

    def test_restart_clears_state(self) -> None:
        setter = "set ZMIENNA=1" if IS_WINDOWS else "ZMIENNA=1"
        probe = "echo [%ZMIENNA%]" if IS_WINDOWS else "echo [$ZMIENNA]"
        call(self.url, "/run", "POST", {"command": setter, "session": "reset"})
        call(self.url, "/sessions/reset/restart", "POST", {})
        _, data = call(self.url, "/run", "POST", {"command": probe, "session": "reset"})
        self.assertNotIn("[1]", data["output"])
        call(self.url, "/sessions/reset", "DELETE")

    def test_cwd_persists_in_session(self) -> None:
        sub = Path(self.tmp.name) / "podkat"
        sub.mkdir(exist_ok=True)
        call(self.url, "/run", "POST", {"command": f'cd "{sub}"', "session": "kat"})
        _, data = call(self.url, "/run", "POST", {"command": "cd" if IS_WINDOWS else "pwd",
                                                 "session": "kat"})
        self.assertIn("podkat", data["cwd"])
        call(self.url, "/sessions/kat", "DELETE")

    def test_output_endpoint(self) -> None:
        call(self.url, "/run", "POST", {"command": "echo strumyk", "session": "strum"})
        status, data = call(self.url, "/sessions/strum/output?since=0")
        self.assertEqual(status, 200)
        self.assertIn("strumyk", data["text"])
        self.assertGreater(data["offset"], 0)
        call(self.url, "/sessions/strum", "DELETE")

    def test_stdin_endpoint(self) -> None:
        call(self.url, "/run", "POST", {"command": "echo x", "session": "wejscie"})
        status, data = call(
            self.url, "/sessions/wejscie/stdin", "POST", {"data": "rem nic"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        call(self.url, "/sessions/wejscie", "DELETE")

    def test_interrupt_endpoint(self) -> None:
        call(self.url, "/run", "POST", {"command": "echo x", "session": "przerwij"})
        status, data = call(self.url, "/sessions/przerwij/interrupt", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        call(self.url, "/sessions/przerwij", "DELETE")

    def test_unknown_path_is_404(self) -> None:
        status, _ = call(self.url, "/nie-ma-takiej")
        self.assertEqual(status, 404)

    def test_instructions_endpoint(self) -> None:
        req = urllib.request.Request(self.url + "/instructions")
        req.add_header("X-Bridge-Token", TOKEN)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as response:
            text = response.read().decode("utf-8")
        self.assertIn("CMD Bridge", text)


class TruncateTestCase(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        text, cut = truncate("abc", 100)
        self.assertEqual(text, "abc")
        self.assertFalse(cut)

    def test_long_text_keeps_head_and_tail(self) -> None:
        text, cut = truncate("A" * 500 + "KONIEC", 100)
        self.assertTrue(cut)
        self.assertTrue(text.startswith("A"))
        self.assertTrue(text.endswith("KONIEC"))
        self.assertIn("ucięto", text)

    def test_zero_limit_disables_truncation(self) -> None:
        text, cut = truncate("A" * 50, 0)
        self.assertEqual(len(text), 50)
        self.assertFalse(cut)


if __name__ == "__main__":
    unittest.main()
