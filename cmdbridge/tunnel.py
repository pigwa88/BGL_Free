"""Klient tunelu: łączy lokalny most z publicznym relayem.

Działa w tym samym procesie co most (jak ``mailbox``): trzyma referencję do
obiektu :class:`~cmdbridge.core.Bridge` i wykonuje polecenia lokalnie. Z relayem
łączy się **wychodząco** - to PC inicjuje połączenie, więc nie trzeba otwierać
żadnego portu ani przekierowania na routerze.

Pętla pracy:

1. ``GET /worker/pull`` (long-poll) - czeka na zadanie od agenta.
2. Wykonuje je przez ``bridge.dispatch(op, request)``.
3. ``POST /worker/push`` - odsyła wynik.

Kilka zadań może działać równolegle (do ``max_concurrency``), dzięki czemu
podgląd wyjścia (``output``) albo ``interrupt`` działają nawet wtedy, gdy sesja
liczy długi build.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .core import Bridge

PULL_WAIT = 25.0            # długość pojedynczego long-polla (sekundy)
MAX_BACKOFF = 30.0          # górny limit odczekania przy braku łączności


class RelayTunnel:
    """Łącze między lokalnym mostem a publicznym relayem."""

    def __init__(
        self,
        bridge: Bridge,
        relay_url: str,
        token: str,
        *,
        pull_wait: float = PULL_WAIT,
        max_concurrency: int = 4,
        verbose: bool = False,
    ) -> None:
        self.bridge = bridge
        self.base = relay_url.rstrip("/")
        self.token = token
        self.pull_wait = pull_wait
        self.verbose = verbose
        self._slots = threading.Semaphore(max(1, max_concurrency))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._online = False
        self._last_error: Optional[str] = None
        # Do publicznego relaya idziemy przez ewentualne proxy systemowe.
        self._opener = urllib.request.build_opener()

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="cmdbridge-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.pull_wait + 5)

    @property
    def online(self) -> bool:
        return self._online

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[tunnel] {message}", flush=True)

    # ---------------------------------------------------------------- HTTP

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 timeout: Optional[float] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("X-Worker-Token", self.token)
        with self._opener.open(req, timeout=timeout or (self.pull_wait + 15)) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
        if status == 204 or not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _pull(self) -> Optional[Dict[str, Any]]:
        return self._request("GET", f"/worker/pull?wait={int(self.pull_wait)}")

    def _push(self, job_id: str, payload: Dict[str, Any]) -> None:
        try:
            self._request("POST", "/worker/push", {"id": job_id, "result": payload}, timeout=30)
        except (urllib.error.URLError, OSError) as exc:  # wynik przepadnie, ale żyjemy dalej
            self._log(f"nie udało się odesłać wyniku {job_id}: {exc}")

    # ---------------------------------------------------------------- pętla

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                job = self._pull()
                self._online = True
                self._last_error = None
                backoff = 1.0
            except urllib.error.HTTPError as exc:
                self._online = False
                self._last_error = f"HTTP {exc.code}"
                self._log(f"relay odrzucił połączenie: {exc.code} "
                          f"({'zły token łącza?' if exc.code == 401 else exc.reason})")
                if self._stop.wait(min(backoff, MAX_BACKOFF)):
                    break
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            except (urllib.error.URLError, OSError) as exc:
                self._online = False
                self._last_error = str(getattr(exc, "reason", exc))
                self._log(f"brak łączności z relayem: {self._last_error} - ponawiam za {backoff:.0f}s")
                if self._stop.wait(min(backoff, MAX_BACKOFF)):
                    break
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            if not job:
                continue  # cichy timeout long-polla - pytamy dalej
            self._dispatch_async(job)

    def _dispatch_async(self, job: Dict[str, Any]) -> None:
        self._slots.acquire()
        worker = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        worker.start()

    def _run_job(self, job: Dict[str, Any]) -> None:
        job_id = job.get("id")
        request = job.get("request") or {}
        try:
            op = request.get("op") or ("run" if request.get("command") is not None else "health")
            status, payload = self.bridge.dispatch(op, request)
            result = dict(payload)
            result["http_status"] = status
        except Exception as exc:  # pragma: no cover - siatka bezpieczeństwa
            result = {"ok": False, "error": f"Błąd wykonania na PC: {exc}", "http_status": 500}
        finally:
            self._slots.release()
        if isinstance(job_id, str):
            self._push(job_id, result)
