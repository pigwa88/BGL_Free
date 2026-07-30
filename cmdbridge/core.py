"""Warstwa logiki mostu - wspólna dla API HTTP i trybu plikowego (mailbox).

Każda operacja zwraca krotkę ``(kod_http, dane)``. Dzięki temu ten sam kod
obsługuje żądania HTTP i pliki JSON wrzucane do katalogu wymiany.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import __version__
from .audit import AuditLog
from .outputview import VIEWS, ViewError, render
from .policy import Policy
from .session import (
    DEFAULT_TIMEOUT,
    SessionBusy,
    SessionDead,
    SessionDirty,
    SessionError,
    SessionManager,
)

Response = Tuple[int, Dict[str, Any]]

MAX_OUTPUT_CHARS = 20_000
MAX_TIMEOUT = 3600.0

PACKAGE_DIR = Path(__file__).resolve().parent


def find_instructions() -> Optional[Path]:
    """Znajduje plik AI_INSTRUCTIONS.md (obok pakietu lub w katalogu projektu)."""
    for candidate in (
        PACKAGE_DIR / "AI_INSTRUCTIONS.md",
        PACKAGE_DIR.parent / "AI_INSTRUCTIONS.md",
    ):
        if candidate.exists():
            return candidate
    return None


def truncate(text: str, limit: int) -> Tuple[str, bool]:
    """Skraca długie wyjście, zachowując początek i koniec."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    removed = len(text) - head - tail
    marker = f"\n...[ucięto {removed} znaków - pełne wyjście w log_file]...\n"
    return text[:head] + marker + text[-tail:], True


class ConfirmGate:
    """Opcjonalne ręczne zatwierdzanie poleceń przez człowieka w konsoli."""

    def __init__(self, enabled: bool = False, stream=None) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._stream = stream

    def approve(self, session: str, command: str) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            print(f"\n[cmdbridge] Sesja {session} chce wykonać:\n    {command}")
            try:
                answer = input("[cmdbridge] Zezwolić? [t/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in ("t", "y", "tak", "yes")


class Bridge:
    """Dyspozytor operacji mostu."""

    def __init__(
        self,
        manager: SessionManager,
        policy: Policy,
        *,
        audit: Optional[AuditLog] = None,
        confirm: Optional[ConfirmGate] = None,
        default_timeout: float = DEFAULT_TIMEOUT,
        max_output: int = MAX_OUTPUT_CHARS,
        instructions_path: Optional[Path] = None,
    ) -> None:
        self.manager = manager
        self.policy = policy
        self.audit = audit or AuditLog(None)
        self.confirm = confirm or ConfirmGate(False)
        self.default_timeout = default_timeout
        self.max_output = max_output
        self.instructions_path = instructions_path or find_instructions()
        self.started_at = time.time()

    # ----------------------------------------------------------------- pomocne

    @staticmethod
    def _error(status: int, error: str, hint: str = "", **extra) -> Response:
        payload: Dict[str, Any] = {"ok": False, "error": error}
        if hint:
            payload["hint"] = hint
        payload.update(extra)
        return status, payload

    def _timeout(self, value: Any) -> float:
        if value is None:
            return self.default_timeout
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return self.default_timeout
        return max(1.0, min(timeout, MAX_TIMEOUT))

    # --------------------------------------------------------------- operacje

    def health(self) -> Response:
        return 200, {
            "ok": True,
            "app": "cmdbridge",
            "version": __version__,
            "platform": platform.platform(),
            "shell": self.manager.spec.name,
            "cwd": self.manager.default_cwd,
            "uptime": round(time.time() - self.started_at, 1),
            "sessions": self.manager.list(),
            "policy": {
                "allow_dangerous": self.policy.allow_dangerous,
                "rules": self.policy.rule_names,
            },
            "defaults": {
                "timeout": self.default_timeout,
                "max_output": self.max_output,
            },
            "views": ("auto",) + VIEWS,
        }

    def instructions(self) -> str:
        if self.instructions_path and self.instructions_path.exists():
            try:
                return self.instructions_path.read_text(encoding="utf-8")
            except OSError:
                pass
        return "Brak pliku AI_INSTRUCTIONS.md - zobacz README.md w katalogu aplikacji."

    def list_sessions(self) -> Response:
        return 200, {"ok": True, "sessions": self.manager.list()}

    def new_session(self, body: Dict[str, Any]) -> Response:
        try:
            session = self.manager.create(
                name=body.get("session") or body.get("name"),
                cwd=body.get("cwd"),
                env=body.get("env"),
                shell=body.get("shell"),
            )
        except SessionError as exc:
            return self._error(409, str(exc), exc.hint)
        except ValueError as exc:
            return self._error(400, str(exc))
        self.audit.write("session_new", session=session.id, cwd=session.cwd)
        return 201, {"ok": True, "session": session.info()}

    def restart_session(self, name: str) -> Response:
        try:
            session = self.manager.restart(name)
        except SessionError as exc:
            return self._error(409, str(exc), exc.hint)
        self.audit.write("session_restart", session=name)
        return 200, {"ok": True, "session": session.info(),
                     "message": f"Uruchomiono nową powłokę dla sesji {name!r}"}

    def kill_session(self, name: str) -> Response:
        if not self.manager.kill(name):
            return self._error(404, f"Nie ma sesji {name!r}")
        self.audit.write("session_kill", session=name)
        return 200, {"ok": True, "message": f"Sesja {name!r} zamknięta"}

    # ------------------------------------------------------------- widoki logu

    def _view_spec(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizuje parametry widoku wyjścia z żądania."""
        spec = {
            "view": str(body.get("view") or "full").lower(),
            "pattern": body.get("pattern"),
            "context": body.get("context"),
            "lines": body.get("lines"),
            "profile": body.get("profile"),
            "max_findings": body.get("max_findings"),
            "line": body.get("line"),
            "raw": body.get("raw"),
            "case_sensitive": body.get("case_sensitive"),
            "invert": body.get("invert"),
            "max_chars": body.get("max_chars", body.get("max_output", self.max_output)),
        }
        return spec

    @staticmethod
    def _resolve_auto(spec: Dict[str, Any], failed: bool) -> Dict[str, Any]:
        """Widok ``auto``: przy porażce pokaż błędy, przy sukcesie sam ogon logu."""
        if spec.get("view") != "auto":
            return spec
        spec = dict(spec)
        spec["view"] = "errors" if failed else "tail"
        if spec["view"] == "tail" and not spec.get("lines"):
            spec["lines"] = 20
        spec["view_auto"] = True
        return spec

    def _render(self, source: Dict[str, Any], spec: Dict[str, Any]) -> Response:
        auto = spec.pop("view_auto", False)
        try:
            view = render(source, spec)
        except ViewError as exc:
            return self._error(400, str(exc))
        if auto:
            view["view_auto"] = True
        return 200, view

    def run(self, body: Dict[str, Any]) -> Response:
        command = body.get("command")
        if not isinstance(command, str):
            return self._error(400, "Pole 'command' jest wymagane (tekst)")

        name = body.get("session") or "main"
        decision = self.policy.check(command)
        if not decision.allowed:
            self.audit.write("blocked", session=name, command=command, rule=decision.rule)
            return self._error(403, decision.reason or "Polecenie zablokowane",
                               "Zmień polecenie lub uruchom most z --allow-dangerous",
                               rule=decision.rule)

        if not self.confirm.approve(name, command):
            self.audit.write("denied", session=name, command=command)
            return self._error(403, "Człowiek odrzucił polecenie w konsoli mostu")

        try:
            session = self.manager.get(name)
        except SessionError as exc:
            return self._error(409, str(exc), exc.hint)

        try:
            result = session.run(command, timeout=self._timeout(body.get("timeout")))
        except SessionBusy as exc:
            return self._error(409, str(exc), exc.hint, session=name)
        except SessionDirty as exc:
            return self._error(409, str(exc), exc.hint, session=name)
        except SessionDead as exc:
            return self._error(409, str(exc), exc.hint, session=name)
        except SessionError as exc:
            return self._error(500, str(exc), exc.hint)

        spec = self._resolve_auto(
            self._view_spec(body), failed=bool(result.exit_code) or result.timed_out
        )
        status, view = self._render(result.source, spec)
        if status != 200:
            return status, view

        payload = result.to_dict()
        payload["output"] = view.pop("text", "")
        payload.update(view)
        payload["ok"] = True
        if payload.get("truncated") and spec.get("view") == "full":
            payload["hint"] = (
                "Wyjście ucięte. Zamiast czytać całość użyj view=errors (błędy), "
                "view=grep z 'pattern' albo view=around z numerem linii."
            )
        if result.timed_out:
            payload["hint"] = (
                "Przekroczono limit czasu. Sesja odzyskana - zwiększ 'timeout'."
                if result.recovered
                else "Przekroczono limit czasu i nie udało się przerwać polecenia. "
                     "Wykonaj operację 'restart' na tej sesji."
            )
        self.audit.write(
            "run",
            session=name,
            command=command,
            exit_code=result.exit_code,
            duration=round(result.duration, 3),
            timed_out=result.timed_out,
        )
        return 200, payload

    def logs(self, body: Dict[str, Any]) -> Response:
        """Odpytuje log wcześniejszego polecenia - bez ponownego uruchamiania go.

        To jest sposób na drążenie dużego buildu: raz uruchom, potem pytaj o
        kolejne fragmenty (grep, around, tail) bez marnowania czasu i kontekstu.
        """
        name = body.get("session") or "main"
        try:
            session = self.manager.get(name, create=False)
        except SessionError as exc:
            return self._error(404, str(exc), exc.hint)

        seq = body.get("seq")
        if seq in (None, "", "last"):
            seq = session.seq
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return self._error(400, "Pole 'seq' musi być liczbą (numer polecenia)")
        if seq < 1:
            return self._error(404, f"Sesja {name!r} nie wykonała jeszcze żadnego polecenia")

        path = session.log_path_for(seq)
        if not path:
            return self._error(
                404,
                f"Brak logu polecenia {seq} w sesji {name!r}",
                "Sprawdź 'seq' w odpowiedzi /run (most mógł działać z --no-logs)",
            )

        status, view = self._render({"path": str(path)}, self._view_spec(body))
        if status != 200:
            return status, view
        view.update({"ok": True, "session": name, "seq": seq, "log_file": str(path)})
        view["output"] = view.pop("text", "")
        return 200, view

    def stdin(self, name: str, body: Dict[str, Any]) -> Response:
        data = body.get("data")
        if not isinstance(data, str):
            return self._error(400, "Pole 'data' jest wymagane (tekst)")
        if body.get("newline", True) and not data.endswith("\n"):
            data += "\n"
        try:
            session = self.manager.get(name, create=False)
            session.write_stdin(data)
        except SessionError as exc:
            return self._error(409, str(exc), exc.hint)
        return 200, {"ok": True, "written": len(data), "session": name}

    def output(self, name: str, since: Any = 0) -> Response:
        try:
            since_int = int(since or 0)
        except (TypeError, ValueError):
            since_int = 0
        try:
            session = self.manager.get(name, create=False)
        except SessionError as exc:
            return self._error(404, str(exc), exc.hint)
        data = session.read_output(since_int)
        text, truncated = truncate(data["text"], self.max_output)
        return 200, {
            "ok": True,
            "session": name,
            "text": text,
            "truncated": truncated,
            "offset": data["offset"],
            "lost_chars": data["lost_chars"],
            "busy": session.busy,
            "alive": session.alive,
        }

    def interrupt(self, name: str) -> Response:
        try:
            session = self.manager.get(name, create=False)
        except SessionError as exc:
            return self._error(404, str(exc), exc.hint)
        sent = session.interrupt()
        self.audit.write("interrupt", session=name, sent=sent)
        return 200, {"ok": True, "session": name, "signal_sent": sent}

    # --------------------------------------------------- dyspozytor operacyjny

    def dispatch(self, op: str, body: Dict[str, Any]) -> Response:
        """Wykonuje operację po nazwie (używane przez tryb mailbox)."""
        op = (op or "run").strip().lower()
        session = body.get("session") or "main"
        handlers: Dict[str, Callable[[], Response]] = {
            "run": lambda: self.run(body),
            "health": self.health,
            "status": self.health,
            "sessions": self.list_sessions,
            "new": lambda: self.new_session(body),
            "restart": lambda: self.restart_session(session),
            "kill": lambda: self.kill_session(session),
            "stdin": lambda: self.stdin(session, body),
            "output": lambda: self.output(session, body.get("since", 0)),
            "interrupt": lambda: self.interrupt(session),
            "logs": lambda: self.logs(body),
        }
        handler = handlers.get(op)
        if not handler:
            return self._error(
                400,
                f"Nieznana operacja {op!r}",
                "Dostępne: " + ", ".join(sorted(handlers)),
            )
        return handler()

    def shutdown(self) -> None:
        self.manager.shutdown()


def default_state_dir() -> Path:
    """Katalog stanu mostu (logi, token, instrukcje)."""
    env = os.environ.get("CMDBRIDGE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cmdbridge"
