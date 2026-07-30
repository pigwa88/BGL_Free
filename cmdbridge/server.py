"""Lokalne API HTTP mostu (tylko biblioteka standardowa)."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .core import Bridge

MAX_BODY = 8 * 1024 * 1024

SESSION_ROUTE = re.compile(r"^/sessions/([^/]+)(?:/(output|stdin|restart|interrupt))?$")


class BridgeHTTPRequestHandler(BaseHTTPRequestHandler):
    """Obsługa żądań HTTP; instancja mostu jest w atrybucie serwera."""

    server_version = "cmdbridge"
    protocol_version = "HTTP/1.1"

    # --------------------------------------------------------------- narzędzia

    @property
    def bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def token(self) -> Optional[str]:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - cisza
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Tuple[bool, Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return False, {"error": "Nieprawidłowy nagłówek Content-Length"}
        if length <= 0:
            return True, {}
        if length > MAX_BODY:
            return False, {"error": "Treść żądania jest za duża"}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, {"error": f"Nieprawidłowy JSON: {exc}"}
        if not isinstance(data, dict):
            return False, {"error": "Treść żądania musi być obiektem JSON"}
        return True, data

    def _authorized(self, query: Dict[str, list]) -> bool:
        if not self.token:
            return True
        header = self.headers.get("X-Bridge-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            header = header or auth[7:].strip()
        supplied = header or (query.get("token") or [""])[0]
        return supplied == self.token

    # ------------------------------------------------------------------ trasy

    def _guard(self, handler) -> None:
        """Żaden błąd obsługi nie może zerwać połączenia bez odpowiedzi."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass
        except Exception as exc:  # pragma: no cover - siatka bezpieczeństwa
            try:
                self._send_json(
                    500,
                    {"ok": False, "error": f"Błąd wewnętrzny mostu: {exc}",
                     "hint": "Sprawdź konsolę mostu; w razie potrzeby zrestartuj sesję"},
                )
            except Exception:
                pass

    def do_GET(self) -> None:  # noqa: N802
        self._guard(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        self._guard(self._handle_post)

    def do_DELETE(self) -> None:  # noqa: N802
        self._guard(self._handle_delete)

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/health"):
            status, payload = self.bridge.health()
            self._send_json(status, payload)
            return

        if not self._authorized(query):
            self._send_json(401, {"ok": False, "error": "Brak lub błędny token",
                                  "hint": "Dodaj nagłówek X-Bridge-Token"})
            return

        if path == "/instructions":
            self._send_text(200, self.bridge.instructions(), "text/markdown")
            return

        if path == "/sessions":
            status, payload = self.bridge.list_sessions()
            self._send_json(status, payload)
            return

        match = SESSION_ROUTE.match(path)
        if match and match.group(2) == "output":
            since = (query.get("since") or ["0"])[0]
            status, payload = self.bridge.output(match.group(1), since)
            self._send_json(status, payload)
            return

        self._send_json(404, {"ok": False, "error": f"Nieznana ścieżka {path!r}",
                              "hint": "Zobacz GET /instructions"})

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorized(query):
            self._send_json(401, {"ok": False, "error": "Brak lub błędny token",
                                  "hint": "Dodaj nagłówek X-Bridge-Token"})
            return

        ok, body = self._read_body()
        if not ok:
            self._send_json(400, {"ok": False, "error": body.get("error")})
            return

        if path == "/run":
            status, payload = self.bridge.run(body)
            self._send_json(status, payload)
            return

        if path == "/sessions":
            status, payload = self.bridge.new_session(body)
            self._send_json(status, payload)
            return

        match = SESSION_ROUTE.match(path)
        if match:
            name, action = match.group(1), match.group(2)
            if action == "stdin":
                status, payload = self.bridge.stdin(name, body)
            elif action == "restart":
                status, payload = self.bridge.restart_session(name)
            elif action == "interrupt":
                status, payload = self.bridge.interrupt(name)
            elif action is None:
                body = dict(body)
                body["session"] = name
                status, payload = self.bridge.run(body)
            else:
                status, payload = 404, {"ok": False, "error": "Nieznana operacja"}
            self._send_json(status, payload)
            return

        self._send_json(404, {"ok": False, "error": f"Nieznana ścieżka {path!r}"})

    def _handle_delete(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._send_json(401, {"ok": False, "error": "Brak lub błędny token"})
            return
        match = SESSION_ROUTE.match(parsed.path.rstrip("/") or "/")
        if match and match.group(2) is None:
            status, payload = self.bridge.kill_session(match.group(1))
            self._send_json(status, payload)
            return
        self._send_json(404, {"ok": False, "error": "Nieznana ścieżka"})


class BridgeServer:
    """Opakowanie na ``ThreadingHTTPServer`` uruchamiane w tle."""

    def __init__(
        self,
        bridge: Bridge,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        self.httpd = ThreadingHTTPServer((host, port), BridgeHTTPRequestHandler)
        self.httpd.daemon_threads = True
        self.httpd.bridge = bridge          # type: ignore[attr-defined]
        self.httpd.token = token            # type: ignore[attr-defined]
        self.httpd.verbose = verbose        # type: ignore[attr-defined]
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="cmdbridge-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
