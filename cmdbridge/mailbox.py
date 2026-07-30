"""Tryb plikowy: AI zapisuje żądanie JSON w ``inbox/``, most odpowiada w ``outbox/``.

Przydatne dla modeli, które potrafią czytać i zapisywać pliki, ale nie mają
klienta HTTP.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from .core import Bridge


class Mailbox:
    """Obserwator katalogu wymiany plików."""

    def __init__(
        self,
        bridge: Bridge,
        root: Path,
        *,
        poll_interval: float = 0.25,
        keep_requests: bool = True,
    ) -> None:
        self.bridge = bridge
        self.root = Path(root).expanduser().resolve()
        self.inbox = self.root / "inbox"
        self.outbox = self.root / "outbox"
        self.done = self.root / "done"
        self.poll_interval = poll_interval
        self.keep_requests = keep_requests
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        for directory in (self.inbox, self.outbox, self.done):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ pętla

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="cmdbridge-mailbox", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_pending()
            except Exception as exc:  # pragma: no cover - pętla ma przeżyć wszystko
                print(f"[cmdbridge] Błąd mailboxa: {exc}")
            self._stop.wait(self.poll_interval)

    def process_pending(self) -> int:
        """Przetwarza wszystkie oczekujące pliki żądań; zwraca ich liczbę."""
        handled = 0
        for path in sorted(self.inbox.glob("*.json")):
            if path.name.endswith(".tmp.json"):
                continue
            self.process_file(path)
            handled += 1
        return handled

    # --------------------------------------------------------------- pojedyncze

    def process_file(self, path: Path) -> dict:
        request = self._read_request(path)
        if request is None:
            payload = {
                "ok": False,
                "status": 400,
                "error": "Nie udało się odczytać pliku jako JSON",
                "hint": 'Format: {"op": "run", "command": "dir"}',
            }
        else:
            op = request.get("op") or ("run" if "command" in request else "health")
            status, data = self.bridge.dispatch(op, request)
            payload = {"ok": data.get("ok", status < 400), "status": status, **data}
            payload["op"] = op

        payload["request_file"] = path.name
        payload["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._write_response(path.name, payload)
        self._retire(path)
        return payload

    def _read_request(self, path: Path, attempts: int = 5) -> Optional[dict]:
        """Czyta JSON, tolerując plik zapisywany właśnie przez inny proces."""
        for attempt in range(attempts):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                time.sleep(0.15 * (attempt + 1))
                continue
            return data if isinstance(data, dict) else None
        return None

    def _write_response(self, name: str, payload: dict) -> None:
        target = self.outbox / name
        tmp = self.outbox / (name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)  # atomowa podmiana - AI nigdy nie zobaczy połowy pliku

    def _retire(self, path: Path) -> None:
        try:
            if self.keep_requests:
                target = self.done / path.name
                counter = 1
                while target.exists():  # ta sama nazwa może wrócić - nie nadpisujemy
                    target = self.done / f"{path.stem}.{counter}{path.suffix}"
                    counter += 1
                shutil.move(str(path), str(target))
            else:
                path.unlink()
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
