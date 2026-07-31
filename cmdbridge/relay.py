"""Publiczny przekaźnik (relay) mostu CMD Bridge.

Most słucha tylko na 127.0.0.1, więc model AI działający poza komputerem
użytkownika nie może się z nim połączyć bezpośrednio. Ten moduł stawia
publiczny serwer HTTP, który pełni rolę skrzynki pośredniej:

    AI / przeglądarka  ──►  RELAY (publiczny)  ◄──  TUNNEL (na PC) ──► CMD Bridge

- Strona po stronie AI to zwykły link z tokenem dostępu (``?token=...``).
  Można ją otworzyć w przeglądarce (konsola z AJAX i auto-odświeżaniem) albo
  odpytywać jej API bezpośrednio (``POST /api/run``).
- PC łączy się z relayem **wychodząco** (nie trzeba przekierowań portów ani
  otwierania firewalla): tunel odpytuje ``/worker/pull`` o polecenia i odsyła
  wyniki na ``/worker/push``.

Dwa różne tokeny rozdzielają role:

- **token dostępu** (``access``) - trafia do linku dawanego modelowi AI,
- **token łącza** (``connect``) - trzyma go PC, żeby zgłosić się po polecenia.

Relay sam **niczego nie wykonuje** - tylko koleją przekazuje żądania i wyniki.
Cała polityka bezpieczeństwa działa dalej po stronie PC (w module ``core``).

Uruchomienie na publicznym serwerze (najlepiej za HTTPS/reverse-proxy)::

    python -m cmdbridge.relay --host 0.0.0.0 --port 9000 --public-url https://twoja-domena
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__

MAX_BODY = 8 * 1024 * 1024
MAX_WAIT = 55.0            # górny limit long-pollingu (sekundy)
WORKER_ONLINE_WINDOW = 60  # ile sekund od ostatniego kontaktu = "online"
DEFAULT_JOB_TTL = 900.0    # po tylu sekundach porzucamy nieodebrane wyniki/zadania


def _now() -> float:
    return time.time()


class RelayHub:
    """Kolejki poleceń i wyników między agentem (publicznym) a workerem (PC).

    Wszystko trzymane jest w pamięci i chronione jedną blokadą z ``Condition``,
    dzięki czemu long-polling (``pull``/``result``) budzi się natychmiast, gdy
    pojawi się nowe zadanie albo wynik - bez odpytywania w pętli.
    """

    def __init__(self, job_ttl: float = DEFAULT_JOB_TTL) -> None:
        self._cv = threading.Condition()
        self._pending: Deque[Dict[str, Any]] = deque()
        self._results: Dict[str, Dict[str, Any]] = {}
        self._counter = 0
        self._worker_last = 0.0
        self._submitted = 0
        self._completed = 0
        self._job_ttl = job_ttl

    # ----------------------------------------------------- strona agenta (AI)

    def submit(self, request: Dict[str, Any]) -> str:
        """Kolejkuje żądanie od agenta i zwraca jego identyfikator."""
        with self._cv:
            self._gc_locked()
            self._counter += 1
            self._submitted += 1
            job_id = f"{int(_now() * 1000):x}-{self._counter}"
            self._pending.append({"id": job_id, "request": request, "created": _now()})
            self._cv.notify_all()
        return job_id

    def result(self, job_id: str, wait: float) -> Optional[Dict[str, Any]]:
        """Czeka (do ``wait`` s) na wynik zadania. ``None`` gdy jeszcze go nie ma."""
        deadline = _now() + max(0.0, wait)
        with self._cv:
            while job_id not in self._results:
                remaining = deadline - _now()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)
            return self._results.pop(job_id)["payload"]

    # ----------------------------------------------------- strona workera (PC)

    def pull(self, wait: float) -> Optional[Dict[str, Any]]:
        """Long-poll workera: zwraca kolejne zadanie albo ``None`` po ``wait`` s."""
        deadline = _now() + max(0.0, wait)
        with self._cv:
            self._worker_last = _now()
            while not self._pending:
                remaining = deadline - _now()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)
                self._worker_last = _now()
            return self._pending.popleft()

    def push(self, job_id: str, payload: Dict[str, Any]) -> None:
        """Zapisuje wynik od workera i budzi czekającego agenta."""
        with self._cv:
            self._worker_last = _now()
            self._completed += 1
            self._results[job_id] = {"payload": payload, "created": _now()}
            self._cv.notify_all()

    def ping(self) -> None:
        with self._cv:
            self._worker_last = _now()

    # ------------------------------------------------------------------ stan

    def worker_online(self) -> bool:
        if not self._worker_last:
            return False
        return (_now() - self._worker_last) < WORKER_ONLINE_WINDOW

    def status(self) -> Dict[str, Any]:
        with self._cv:
            self._gc_locked()
            last = self._worker_last
            return {
                "worker_online": bool(last) and (_now() - last) < WORKER_ONLINE_WINDOW,
                "worker_last_seen": round(_now() - last, 1) if last else None,
                "pending": len(self._pending),
                "results_waiting": len(self._results),
                "submitted": self._submitted,
                "completed": self._completed,
            }

    def _gc_locked(self) -> None:
        """Porzuca zadania i wyniki starsze niż TTL (wywoływane pod blokadą)."""
        cutoff = _now() - self._job_ttl
        while self._pending and self._pending[0]["created"] < cutoff:
            self._pending.popleft()
        stale = [k for k, v in self._results.items() if v["created"] < cutoff]
        for k in stale:
            del self._results[k]


class RelayHandler(BaseHTTPRequestHandler):
    """Obsługa żądań relaya. Instancja stanu jest w atrybutach serwera."""

    server_version = "cmdbridge-relay"
    protocol_version = "HTTP/1.1"

    # --------------------------------------------------------------- skróty

    @property
    def hub(self) -> RelayHub:
        return self.server.hub  # type: ignore[attr-defined]

    @property
    def access_token(self) -> Optional[str]:
        return self.server.access_token  # type: ignore[attr-defined]

    @property
    def connect_token(self) -> Optional[str]:
        return self.server.connect_token  # type: ignore[attr-defined]

    @property
    def allow_origin(self) -> str:
        return getattr(self.server, "allow_origin", "*")

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - cisza
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # --------------------------------------------------------- niskopoziomowe

    def _send_cors(self) -> None:
        """Pozwala wołać API ze strony hostowanej na innej domenie.

        Autoryzacja opiera się na tokenie w nagłówku, a nie na ciasteczkach,
        więc nie wysyłamy ``Allow-Credentials`` - przeglądarka i tak nie doda
        tu ciasteczek, a token musi podać jawnie skrypt strony.
        """
        origin = self.allow_origin
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Bridge-Token, X-Worker-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        if origin != "*":
            self.send_header("Vary", "Origin")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_no_content(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()

    def _send_html(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        if self.command != "HEAD":
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

    @staticmethod
    def _token_from(headers, query: Dict[str, list], header_name: str) -> str:
        supplied = headers.get(header_name) or ""
        auth = headers.get("Authorization") or ""
        if not supplied and auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            supplied = (query.get("token") or [""])[0]
        return supplied

    def _check(self, expected: Optional[str], supplied: str) -> bool:
        if not expected:
            return True
        return secrets.compare_digest(str(supplied), str(expected))

    def _wait_param(self, query: Dict[str, list], default: float) -> float:
        try:
            wait = float((query.get("wait") or [default])[0])
        except (TypeError, ValueError):
            wait = default
        return max(0.0, min(wait, MAX_WAIT))

    # -------------------------------------------------------- siatka bezpieczeń

    def _guard(self, handler) -> None:
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass
        except Exception as exc:  # pragma: no cover - siatka bezpieczeństwa
            try:
                self._send_json(500, {"ok": False, "error": f"Błąd relaya: {exc}"})
            except Exception:
                pass

    def do_GET(self) -> None:  # noqa: N802
        self._guard(self._handle_get)

    def do_HEAD(self) -> None:  # noqa: N802
        self._guard(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        self._guard(self._handle_post)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Preflight CORS - przeglądarka pyta o zgodę przed właściwym żądaniem."""
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._send_cors()
        self.end_headers()

    # ------------------------------------------------------------------ GET

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/console"):
            self._send_html(200, CONSOLE_HTML)
            return

        if path == "/health":
            self._send_json(200, {
                "ok": True, "app": "cmdbridge-relay", "version": __version__,
                "worker_online": self.hub.worker_online(),
            })
            return

        # --- strona agenta (token dostępu) ---
        if path == "/api/status":
            if not self._check(self.access_token,
                               self._token_from(self.headers, query, "X-Bridge-Token")):
                self._send_json(401, {"ok": False, "error": "Zły token dostępu"})
                return
            self._send_json(200, {"ok": True, **self.hub.status()})
            return

        if path == "/api/result":
            if not self._check(self.access_token,
                               self._token_from(self.headers, query, "X-Bridge-Token")):
                self._send_json(401, {"ok": False, "error": "Zły token dostępu"})
                return
            job_id = (query.get("id") or [""])[0]
            if not job_id:
                self._send_json(400, {"ok": False, "error": "Brak parametru 'id'"})
                return
            payload = self.hub.result(job_id, self._wait_param(query, 25.0))
            if payload is None:
                self._send_no_content()
                return
            self._send_json(200, payload)
            return

        # --- strona workera (token łącza) ---
        if path == "/worker/pull":
            if not self._check(self.connect_token,
                               self._token_from(self.headers, query, "X-Worker-Token")):
                self._send_json(401, {"ok": False, "error": "Zły token łącza"})
                return
            job = self.hub.pull(self._wait_param(query, 25.0))
            if job is None:
                self._send_no_content()
                return
            self._send_json(200, {"ok": True, "id": job["id"], "request": job["request"]})
            return

        if path == "/worker/ping":
            if not self._check(self.connect_token,
                               self._token_from(self.headers, query, "X-Worker-Token")):
                self._send_json(401, {"ok": False, "error": "Zły token łącza"})
                return
            self.hub.ping()
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"ok": False, "error": f"Nieznana ścieżka {path!r}"})

    # ----------------------------------------------------------------- POST

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        # --- strona workera: odesłanie wyniku ---
        if path == "/worker/push":
            if not self._check(self.connect_token,
                               self._token_from(self.headers, query, "X-Worker-Token")):
                self._send_json(401, {"ok": False, "error": "Zły token łącza"})
                return
            ok, body = self._read_body()
            if not ok:
                self._send_json(400, {"ok": False, "error": body.get("error")})
                return
            job_id = body.get("id")
            if not isinstance(job_id, str):
                self._send_json(400, {"ok": False, "error": "Brak 'id'"})
                return
            self.hub.push(job_id, body.get("result") or {})
            self._send_json(200, {"ok": True})
            return

        # --- strona agenta (token dostępu) ---
        if not self._check(self.access_token,
                           self._token_from(self.headers, query, "X-Bridge-Token")):
            self._send_json(401, {"ok": False, "error": "Zły token dostępu",
                                  "hint": "Dodaj nagłówek X-Bridge-Token lub ?token="})
            return

        ok, body = self._read_body()
        if not ok:
            self._send_json(400, {"ok": False, "error": body.get("error")})
            return

        if path == "/api/submit":
            request = self._normalize_request(body)
            if request is None:
                self._send_json(400, {"ok": False, "error": "Brak 'command' lub 'op'"})
                return
            job_id = self.hub.submit(request)
            self._send_json(202, {"ok": True, "id": job_id,
                                  "worker_online": self.hub.worker_online()})
            return

        if path == "/api/run":
            request = self._normalize_request(body)
            if request is None:
                self._send_json(400, {"ok": False, "error": "Brak 'command' lub 'op'"})
                return
            job_id = self.hub.submit(request)
            payload = self.hub.result(job_id, self._wait_param(query, 30.0))
            if payload is None:
                self._send_json(202, {
                    "ok": True, "pending": True, "id": job_id,
                    "worker_online": self.hub.worker_online(),
                    "hint": "Wynik jeszcze nie gotowy - odpytaj GET /api/result?id=" + job_id,
                })
                return
            self._send_json(200, payload)
            return

        self._send_json(404, {"ok": False, "error": f"Nieznana ścieżka {path!r}"})

    @staticmethod
    def _normalize_request(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Sprowadza ciało do żądania rozumianego przez ``Bridge.dispatch``."""
        request = dict(body)
        op = request.get("op")
        if not op:
            op = "run" if request.get("command") is not None else None
        if not op:
            return None
        request["op"] = str(op).strip().lower()
        return request


class RelayServer:
    """Opakowanie na ``ThreadingHTTPServer`` uruchamiane w tle."""

    def __init__(self, hub: RelayHub, host: str, port: int,
                 access_token: Optional[str], connect_token: Optional[str],
                 verbose: bool = False, allow_origin: str = "*") -> None:
        self.httpd = ThreadingHTTPServer((host, port), RelayHandler)
        self.httpd.daemon_threads = True
        self.httpd.hub = hub                          # type: ignore[attr-defined]
        self.httpd.access_token = access_token        # type: ignore[attr-defined]
        self.httpd.connect_token = connect_token      # type: ignore[attr-defined]
        self.httpd.verbose = verbose                  # type: ignore[attr-defined]
        self.httpd.allow_origin = allow_origin        # type: ignore[attr-defined]
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="cmdbridge-relay", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------- strona

CONSOLE_HTML = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMD Bridge — konsola zdalna</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         background: #0d1117; color: #c9d1d9; }
  header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
           padding: 10px 16px; background: #161b22; border-bottom: 1px solid #30363d; }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; color: #e6edf3; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #6e7681; display: inline-block; }
  .dot.on { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  .dot.off { background: #f85149; }
  .spacer { flex: 1; }
  .muted { color: #8b949e; font-size: 12px; }
  main { padding: 12px 16px; max-width: 1100px; margin: 0 auto; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
  label { font-size: 12px; color: #8b949e; }
  input, select, textarea, button { font-family: inherit; font-size: 13px;
      background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 6px 8px; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: #58a6ff; }
  textarea { width: 100%; min-height: 64px; resize: vertical; }
  input.small { width: 90px; }
  button { background: #238636; border-color: #2ea043; color: #fff; cursor: pointer; font-weight: 600; }
  button:hover { background: #2ea043; }
  button.ghost { background: #21262d; border-color: #30363d; color: #c9d1d9; font-weight: 400; }
  button:disabled { opacity: .5; cursor: default; }
  .meta { font-size: 12px; color: #8b949e; margin: 8px 0 4px; min-height: 16px; }
  pre { background: #010409; border: 1px solid #30363d; border-radius: 8px; padding: 12px;
        white-space: pre-wrap; word-break: break-word; overflow-x: auto; max-height: 64vh;
        overflow-y: auto; margin: 0; font-size: 12.5px; line-height: 1.45; }
  .ok { color: #3fb950; } .bad { color: #f85149; } .warn { color: #d29922; }
  .tokenbar { padding: 40px 16px; max-width: 460px; margin: 40px auto; text-align: center; }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>CMD Bridge — konsola zdalna</h1>
  <span class="muted" id="wstat">łączenie…</span>
  <span class="spacer"></span>
  <span class="muted" id="ver"></span>
</header>
<main id="app" hidden>
  <div class="row">
    <label>sesja</label><input id="sess" class="small" value="main">
    <label>widok</label>
    <select id="view">
      <option value="auto">auto</option>
      <option value="errors">errors</option>
      <option value="summary">summary</option>
      <option value="tail">tail</option>
      <option value="head">head</option>
      <option value="grep">grep</option>
      <option value="full">full</option>
      <option value="quiet">quiet</option>
    </select>
    <label>pattern</label><input id="pattern" placeholder="(dla grep)">
    <label>timeout</label><input id="to" class="small" type="number" value="120">
  </div>
  <div class="row">
    <label><input type="checkbox" id="auto"> auto-odświeżanie wyjścia co</label>
    <input id="interval" class="small" type="number" value="3"> <span class="muted">s (podgląd sesji na żywo)</span>
    <span class="spacer"></span>
    <button class="ghost" id="clear">wyczyść</button>
  </div>
  <textarea id="cmd" placeholder="Wpisz polecenie i naciśnij Ctrl+Enter…  np. gradlew.bat assembleDebug --console=plain"></textarea>
  <div class="row">
    <button id="run">Uruchom ▶</button>
    <button class="ghost" id="int">przerwij</button>
    <button class="ghost" id="restart">restart sesji</button>
  </div>
  <div class="meta" id="meta"></div>
  <pre id="out"></pre>
</main>
<div class="tokenbar" id="tokenbar" hidden>
  <p class="muted">Podaj token dostępu (był w linku po <code>?token=</code>):</p>
  <div class="row" style="justify-content:center">
    <input id="tokinput" placeholder="token dostępu" style="width:260px">
    <button id="toksave">Zapisz</button>
  </div>
</div>
<script>
(function(){
  var qs = new URLSearchParams(location.search);
  var token = qs.get('token') || sessionStorage.getItem('cmdbridge_relay_token') || '';
  if (token) sessionStorage.setItem('cmdbridge_relay_token', token);

  var $ = function(id){ return document.getElementById(id); };
  var offsets = {};   // sesja -> offset podglądu wyjścia
  var busy = false;

  function headers(){ return {'Content-Type':'application/json','X-Bridge-Token':token}; }
  function needToken(){ $('app').hidden = true; $('tokenbar').hidden = false; }

  async function api(path, opts){
    opts = opts || {};
    opts.headers = Object.assign(headers(), opts.headers||{});
    return fetch(path, opts);
  }

  async function submit(request){
    var r = await api('/api/submit', {method:'POST', body: JSON.stringify(request)});
    if (r.status === 401){ needToken(); throw new Error('401'); }
    var j = await r.json();
    if (!j.id) throw new Error(j.error || 'brak id');
    return j.id;
  }

  async function result(id, wait){
    for(;;){
      var r = await api('/api/result?id='+encodeURIComponent(id)+'&wait='+(wait||25));
      if (r.status === 204) continue;          // wciąż w toku - pytaj dalej
      if (r.status === 401){ needToken(); throw new Error('401'); }
      try { return await r.json(); }
      catch(e){ return {ok:false, error:'zła odpowiedź '+r.status}; }
    }
  }

  async function call(request, wait){
    var id = await submit(request);
    return result(id, wait);
  }

  function esc(s){ return (s||'').replace(/[&<>]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }

  function render(res){
    var cls = res.exit_code === 0 ? 'ok' : (res.exit_code == null ? 'warn' : 'bad');
    var bits = [];
    if (res.exit_code !== undefined) bits.push('<span class="'+cls+'">exit='+res.exit_code+'</span>');
    if (res.verdict) bits.push('werdykt='+res.verdict);
    if (res.status_line) bits.push(esc(res.status_line));
    if (res.cwd) bits.push('cwd='+esc(res.cwd));
    if (res.duration != null) bits.push(res.duration+'s');
    if (res.view) bits.push('widok='+res.view);
    if (res.lines_total != null) bits.push('linii='+res.lines_total);
    if (res.error) bits.push('<span class="bad">'+esc(res.error)+'</span>');
    if (res.hint) bits.push('<span class="warn">'+esc(res.hint)+'</span>');
    $('meta').innerHTML = bits.join(' · ');
    var text = res.output != null ? res.output : (res.text != null ? res.text : '');
    if (res.findings && res.findings.length){
      text = res.findings.map(function(f){
        var loc = f.file ? (' '+f.file+(f.line?':'+f.line:'')) : '';
        return '['+ (f.severity||'error') +']'+loc+' '+(f.message||'')
             + (f.log_line?'  (log:'+f.log_line+')':''); }).join('\n')
             + (text ? '\n\n'+text : '');
    }
    $('out').textContent = text || '(brak wyjścia)';
  }

  async function runCommand(){
    var command = $('cmd').value.trim();
    if (!command || busy) return;
    busy = true; $('run').disabled = true;
    $('meta').textContent = 'wykonuję…';
    try {
      var req = {op:'run', command: command, session: $('sess').value||'main',
                 view: $('view').value, timeout: parseInt($('to').value)||120};
      if ($('view').value === 'grep' && $('pattern').value) req.pattern = $('pattern').value;
      render(await call(req, 25));
      offsets[$('sess').value] = 0;   // po nowym poleceniu podgląd od początku
    } catch(e){ if (e.message!=='401') $('meta').innerHTML='<span class="bad">'+esc(e.message)+'</span>'; }
    finally { busy = false; $('run').disabled = false; }
  }

  async function tick(){
    // pasek stanu workera
    try {
      var r = await api('/api/status');
      if (r.status === 401){ needToken(); return; }
      var s = await r.json();
      var on = s.worker_online;
      $('dot').className = 'dot ' + (on ? 'on' : 'off');
      $('wstat').textContent = on ? ('PC online · w kolejce: '+s.pending)
                                  : ('PC offline'+(s.worker_last_seen!=null?' (ost. '+s.worker_last_seen+'s temu)':''));
    } catch(e){ $('dot').className = 'dot off'; $('wstat').textContent='brak relaya'; }

    // podgląd wyjścia na żywo (bez blokowania, gdy trwa polecenie)
    if ($('auto').checked && !busy){
      var sess = $('sess').value || 'main';
      try {
        var res = await call({op:'output', session: sess, since: offsets[sess]||0}, 5);
        if (res && res.ok){
          offsets[sess] = res.offset || 0;
          if (res.text){ $('out').textContent += res.text; $('out').scrollTop = $('out').scrollHeight; }
        }
      } catch(e){}
    }
  }

  // zdarzenia
  $('run').onclick = runCommand;
  $('cmd').addEventListener('keydown', function(e){
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runCommand(); }
  });
  $('clear').onclick = function(){ $('out').textContent=''; $('meta').textContent=''; };
  $('int').onclick = function(){ call({op:'interrupt', session:$('sess').value||'main'}, 8).then(function(r){
    $('meta').innerHTML = '<span class="warn">przerwano (signal_sent='+(r&&r.signal_sent)+')</span>'; }); };
  $('restart').onclick = function(){ call({op:'restart', session:$('sess').value||'main'}, 10).then(function(r){
    offsets[$('sess').value]=0; $('meta').innerHTML='<span class="warn">'+esc((r&&r.message)||'restart')+'</span>'; }); };
  $('toksave').onclick = function(){
    token = $('tokinput').value.trim();
    if (token){ sessionStorage.setItem('cmdbridge_relay_token', token);
      $('tokenbar').hidden = true; $('app').hidden = false; }
  };

  // wersja + start
  fetch('/health').then(function(r){return r.json();}).then(function(h){
    $('ver').textContent = 'relay '+(h.version||''); }).catch(function(){});

  if (!token){ needToken(); }
  else { $('app').hidden = false; }

  tick();
  setInterval(function(){
    var iv = Math.max(1, parseInt($('interval').value)||3);
    // status co ~iv s; realizowane prostym licznikiem
    tick._c = (tick._c||0) + 1;
    if (tick._c % iv === 0 || tick._c < 2) tick();
  }, 1000);
})();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------- CLI relaya

def default_relay_state_dir() -> Path:
    env = os.environ.get("CMDBRIDGE_HOME")
    base = Path(env).expanduser() if env else Path.home() / ".cmdbridge"
    return base / "relay"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmdbridge.relay",
        description="Publiczny przekaźnik dla CMD Bridge (link z tokenem dla AI).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="Adres nasłuchu (0.0.0.0 = publicznie)")
    parser.add_argument("--port", type=int, default=9000, help="Port HTTP relaya")
    parser.add_argument("--access-token", help="Token dostępu (link dla AI); domyślnie losowy")
    parser.add_argument("--connect-token", help="Token łącza (dla PC); domyślnie losowy")
    parser.add_argument("--public-url",
                        help="Publiczny adres (np. https://twoja-domena) do wypisania linku")
    parser.add_argument("--allow-origin", default="*",
                        help="CORS: skąd wolno wołać API (np. https://twoja-strona; "
                             "'*' = zewsząd, pusty = wyłącz CORS)")
    parser.add_argument("--state-dir", default=None, help="Katalog na relay.json")
    parser.add_argument("--job-ttl", type=float, default=DEFAULT_JOB_TTL,
                        help="Po ilu sekundach porzucać nieodebrane zadania/wyniki")
    parser.add_argument("--verbose", action="store_true", help="Loguj żądania HTTP")
    parser.add_argument("--version", action="version", version=f"cmdbridge-relay {__version__}")
    return parser


def _write_state(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _banner(server: RelayServer, access: str, connect: str, public_url: Optional[str]) -> None:
    base = (public_url or server.url).rstrip("/")
    link = f"{base}/?token={access}"
    print("=" * 70)
    print(f" CMD Bridge RELAY {__version__} - publiczny przekaźnik")
    print("=" * 70)
    print(f" Nasłuch          : {server.url}")
    if public_url:
        print(f" Adres publiczny  : {public_url}")
    print(f" Token dostępu    : {access}")
    print(f" Token łącza (PC) : {connect}")
    print("-" * 70)
    print(" 1) Na PC (obok mostu) uruchom tunel:")
    print(f"      python -m cmdbridge --relay {base} --relay-token {connect}")
    print(" 2) Modelowi AI podaj ten link (konsola z AJAX) albo API:")
    print(f"      {link}")
    print(f"      POST {base}/api/run   nagłówek  X-Bridge-Token: {access}")
    print("-" * 70)
    print(" UWAGA: to wystawia wykonywanie poleceń na PC do internetu.")
    print(" Postaw relay za HTTPS (reverse-proxy), trzymaj tokeny w tajemnicy,")
    print(" a most uruchamiaj na koncie bez uprawnień administratora.")
    print(" Ctrl+C kończy pracę relaya.")
    print("=" * 70, flush=True)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    access = args.access_token or secrets.token_urlsafe(24)
    connect = args.connect_token or secrets.token_urlsafe(24)

    hub = RelayHub(job_ttl=args.job_ttl)
    try:
        server = RelayServer(hub, args.host, args.port, access, connect, args.verbose,
                             allow_origin=args.allow_origin)
    except OSError as exc:
        print(f"Nie można otworzyć portu {args.port}: {exc}")
        return 2
    server.start()

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_relay_state_dir()
    _write_state(state_dir / "relay.json", {
        "version": __version__,
        "pid": os.getpid(),
        "url": server.url,
        "public_url": args.public_url,
        "access_token": access,
        "connect_token": connect,
    })

    _banner(server, access, connect, args.public_url)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except (AttributeError, ValueError):  # pragma: no cover - Windows
        pass
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        print("\n[relay] Zamykanie...", flush=True)
        server.stop()
        try:
            (state_dir / "relay.json").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
