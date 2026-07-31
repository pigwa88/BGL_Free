"""Testy publicznego relaya i tunelu (relay + tunnel + bridge)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdbridge.core import Bridge  # noqa: E402
from cmdbridge.policy import Policy  # noqa: E402
from cmdbridge.relay import RelayHub, RelayServer  # noqa: E402
from cmdbridge.session import SessionManager  # noqa: E402
from cmdbridge.shell import resolve_shell  # noqa: E402
from cmdbridge.tunnel import RelayTunnel  # noqa: E402

ACCESS = "access-token"
CONNECT = "connect-token"


def http(url, path, method="GET", body=None, headers=None, timeout=40):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            try:
                data = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                data = raw
            return response.status, data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def access(url, path, method="GET", body=None, timeout=40):
    return http(url, path, method, body, {"X-Bridge-Token": ACCESS}, timeout)


def worker(url, path, method="GET", body=None, timeout=40):
    return http(url, path, method, body, {"X-Worker-Token": CONNECT}, timeout)


# --------------------------------------------------------------------- RelayHub

class HubTestCase(unittest.TestCase):
    def test_submit_pull_push_result(self) -> None:
        hub = RelayHub()
        job_id = hub.submit({"op": "run", "command": "echo hi"})
        job = hub.pull(wait=1)
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["request"]["command"], "echo hi")
        hub.push(job_id, {"ok": True, "exit_code": 0})
        payload = hub.result(job_id, wait=1)
        self.assertEqual(payload["exit_code"], 0)

    def test_result_consumed_once(self) -> None:
        hub = RelayHub()
        jid = hub.submit({"command": "x"})
        hub.push(jid, {"ok": True})
        self.assertIsNotNone(hub.result(jid, wait=1))
        self.assertIsNone(hub.result(jid, wait=0.1))  # już odebrany

    def test_pull_times_out_when_empty(self) -> None:
        hub = RelayHub()
        start = time.time()
        self.assertIsNone(hub.pull(wait=0.3))
        self.assertGreaterEqual(time.time() - start, 0.25)

    def test_pull_wakes_on_submit(self) -> None:
        hub = RelayHub()
        got = {}

        def puller():
            got["job"] = hub.pull(wait=3)

        thread = threading.Thread(target=puller)
        thread.start()
        time.sleep(0.1)
        jid = hub.submit({"command": "echo now"})
        thread.join(timeout=3)
        self.assertEqual(got["job"]["id"], jid)

    def test_result_wakes_on_push(self) -> None:
        hub = RelayHub()
        jid = hub.submit({"command": "x"})
        hub.pull(wait=1)
        got = {}

        def waiter():
            got["r"] = hub.result(jid, wait=3)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        hub.push(jid, {"ok": True, "value": 42})
        thread.join(timeout=3)
        self.assertEqual(got["r"]["value"], 42)

    def test_worker_online_flag(self) -> None:
        hub = RelayHub()
        self.assertFalse(hub.worker_online())
        hub.ping()
        self.assertTrue(hub.worker_online())

    def test_gc_drops_stale(self) -> None:
        hub = RelayHub(job_ttl=0.05)
        jid = hub.submit({"command": "x"})
        hub.push(jid, {"ok": True})
        time.sleep(0.1)
        hub.submit({"command": "y"})  # wywoła gc
        self.assertIsNone(hub.result(jid, wait=0.05))


# ------------------------------------------------------- Relay HTTP (bez tunelu)

class RelayHTTPTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hub = RelayHub()
        cls.server = RelayServer(cls.hub, "127.0.0.1", 0, ACCESS, CONNECT)
        cls.server.start()
        cls.url = cls.server.url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        # Świeży stan kolejek dla każdego testu (hub jest współdzielony w klasie).
        self.hub._pending.clear()
        self.hub._results.clear()

    def test_health_public(self) -> None:
        status, data = http(self.url, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], "cmdbridge-relay")

    def test_console_page_served(self) -> None:
        status, _ = http(self.url, "/", timeout=10)
        self.assertEqual(status, 200)

    def test_access_requires_token(self) -> None:
        status, _ = http(self.url, "/api/status")
        self.assertEqual(status, 401)

    def test_worker_requires_token(self) -> None:
        status, _ = http(self.url, "/worker/pull?wait=0")
        self.assertEqual(status, 401)

    def test_access_token_wrong(self) -> None:
        status, _ = http(self.url, "/api/status", headers={"X-Bridge-Token": "nope"})
        self.assertEqual(status, 401)

    def test_token_via_query(self) -> None:
        status, data = http(self.url, f"/api/status?token={ACCESS}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_submit_then_worker_pull(self) -> None:
        status, data = access(self.url, "/api/submit", "POST", {"command": "echo hi"})
        self.assertEqual(status, 202)
        job_id = data["id"]
        status, job = worker(self.url, "/worker/pull?wait=1")
        self.assertEqual(status, 200)
        self.assertEqual(job["id"], job_id)

    def test_submit_needs_command_or_op(self) -> None:
        status, _ = access(self.url, "/api/submit", "POST", {"session": "main"})
        self.assertEqual(status, 400)

    def test_worker_pull_empty_204(self) -> None:
        status, data = worker(self.url, "/worker/pull?wait=0")
        self.assertEqual(status, 204)
        self.assertIsNone(data)

    def test_result_pending_204(self) -> None:
        status, data = access(self.url, "/api/submit", "POST", {"command": "sleep"})
        jid = data["id"]
        status, _ = access(self.url, f"/api/result?id={jid}&wait=0")
        self.assertEqual(status, 204)

    def test_full_round_trip_via_http(self) -> None:
        # agent zgłasza, worker pobiera, odsyła wynik, agent go odbiera
        _, data = access(self.url, "/api/submit", "POST", {"command": "echo x"})
        jid = data["id"]
        _, job = worker(self.url, "/worker/pull?wait=1")
        worker(self.url, "/worker/push", "POST",
               {"id": jid, "result": {"ok": True, "exit_code": 0, "output": "x"}})
        status, res = access(self.url, f"/api/result?id={jid}&wait=2")
        self.assertEqual(status, 200)
        self.assertEqual(res["exit_code"], 0)

    def test_run_pending_when_no_worker(self) -> None:
        status, data = access(self.url, "/api/run?wait=0", "POST", {"command": "echo x"})
        self.assertEqual(status, 202)
        self.assertTrue(data["pending"])


# ------------------------------------------- CORS (strona na innym hoście)

class CorsTestCase(unittest.TestCase):
    """Samodzielna strona (web/console.html) bywa hostowana na innej domenie."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hub = RelayHub()
        cls.server = RelayServer(cls.hub, "127.0.0.1", 0, ACCESS, CONNECT,
                                 allow_origin="https://moja-strona.pl")
        cls.server.start()
        cls.url = cls.server.url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    @staticmethod
    def _headers_of(url, path, method="GET", extra=None):
        req = urllib.request.Request(url + path, method=method)
        for k, v in (extra or {}).items():
            req.add_header(k, v)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers)

    def test_preflight_allows_token_header(self) -> None:
        status, headers = self._headers_of(
            self.url, "/api/submit", "OPTIONS",
            {"Origin": "https://moja-strona.pl", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://moja-strona.pl")
        self.assertIn("X-Bridge-Token", headers.get("Access-Control-Allow-Headers", ""))

    def test_normal_response_carries_cors(self) -> None:
        status, headers = self._headers_of(self.url, "/api/status", "GET",
                                           {"X-Bridge-Token": ACCESS})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://moja-strona.pl")
        self.assertEqual(headers.get("Vary"), "Origin")

    def test_error_response_carries_cors(self) -> None:
        # Bez CORS na 401 przeglądarka nie pokaże powodu odmowy.
        status, headers = self._headers_of(self.url, "/api/status", "GET",
                                           {"X-Bridge-Token": "zly"})
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://moja-strona.pl")

    def test_wildcard_default_has_no_vary(self) -> None:
        hub = RelayHub()
        server = RelayServer(hub, "127.0.0.1", 0, ACCESS, CONNECT)
        server.start()
        try:
            status, headers = self._headers_of(server.url, "/health")
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIsNone(headers.get("Vary"))
        finally:
            server.stop()


# --------------------------------------------------- pełny łańcuch z tunelem/PC

class TunnelEndToEndTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hub = RelayHub()
        cls.server = RelayServer(cls.hub, "127.0.0.1", 0, ACCESS, CONNECT)
        cls.server.start()
        cls.url = cls.server.url
        cls.manager = SessionManager(
            resolve_shell("auto"),
            default_cwd=cls.tmp.name,
            log_dir=Path(cls.tmp.name) / "logs",
        )
        cls.bridge = Bridge(cls.manager, Policy())
        cls.tunnel = RelayTunnel(cls.bridge, cls.url, CONNECT, pull_wait=5)
        cls.tunnel.start()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tunnel.stop()
        cls.server.stop()
        cls.bridge.shutdown()
        cls.tmp.cleanup()

    def test_command_runs_on_bridge(self) -> None:
        status, res = access(self.url, "/api/run?wait=30", "POST",
                             {"command": "echo hello-relay", "view": "full"})
        self.assertEqual(status, 200)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("hello-relay", res["output"])

    def test_policy_blocks_through_relay(self) -> None:
        status, res = access(self.url, "/api/run?wait=15", "POST",
                             {"command": "format C:"})
        # blokada polityki po stronie PC = ok:false + http_status 403
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("http_status"), 403)

    def test_health_op_through_relay(self) -> None:
        status, res = access(self.url, "/api/run?wait=15", "POST", {"op": "health"})
        self.assertEqual(status, 200)
        self.assertEqual(res["app"], "cmdbridge")

    def test_worker_shows_online(self) -> None:
        status, data = access(self.url, "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["worker_online"])

    def test_wrong_connect_token_stays_offline(self) -> None:
        bad = RelayTunnel(self.bridge, self.url, "WRONG", pull_wait=1)
        bad.start()
        time.sleep(0.6)
        self.assertFalse(bad.online)
        self.assertEqual(bad.last_error, "HTTP 401")
        bad.stop()


if __name__ == "__main__":
    unittest.main()
