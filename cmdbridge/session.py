"""Trwałe sesje powłoki: uruchamianie, wykonywanie poleceń, odczyt wyjścia."""

from __future__ import annotations

import codecs
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .procutil import interrupt_children
from .shell import MARKER_PREFIX, ShellSpec, resolve_shell

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_BUFFER = 4_000_000  # znaków trzymanych w pamięci na sesję
INTERRUPT_GRACE = 3.0           # ile czekać na odzyskanie sesji po przerwaniu


def strip_markers(text: str) -> tuple[str, int]:
    """Usuwa z tekstu wewnętrzne linie znacznika mostu.

    Zwraca oczyszczony tekst oraz liczbę znaków wstrzymanych na końcu (niepełna
    linia znacznika, którą trzeba oddać dopiero po jej domknięciu).
    """
    lines = text.split("\n")
    held = 0
    if text and not text.endswith("\n"):
        tail = lines[-1]
        # Niedokończona linia znacznika: wstrzymujemy ją do następnego odczytu.
        if MARKER_PREFIX in tail or MARKER_PREFIX.startswith(tail):
            held = len(lines.pop())
            lines.append("")  # zachowujemy zakończenie linii przed wstrzymanym fragmentem
    if not held and MARKER_PREFIX not in text:
        return text, 0
    cleaned = [line for line in lines if MARKER_PREFIX not in line]
    return "\n".join(cleaned), held


class SessionError(Exception):
    """Bazowy błąd sesji."""

    hint = ""


class SessionBusy(SessionError):
    hint = ("Sesja wykonuje inne polecenie. Poczekaj, użyj /stdin, /interrupt "
            "albo utwórz nową sesję.")


class SessionDead(SessionError):
    hint = "Proces powłoki zakończył się. Użyj 'restart' lub utwórz nową sesję."


class SessionDirty(SessionError):
    hint = ("Sesja jest niespójna po timeoucie (poprzednie polecenie nadal "
            "działa). Użyj 'restart' albo 'interrupt'.")


@dataclass
class RunResult:
    """Wynik pojedynczego polecenia."""

    session: str
    seq: int
    command: str
    output: str
    exit_code: Optional[int]
    cwd: str
    duration: float
    timed_out: bool = False
    recovered: bool = False
    log_file: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "session": self.session,
            "seq": self.seq,
            "command": self.command,
            "output": self.output,
            "exit_code": self.exit_code,
            "cwd": self.cwd,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "recovered": self.recovered,
            "log_file": self.log_file,
        }


class ShellSession:
    """Jeden długo żyjący proces powłoki sterowany przez stdin/stdout.

    Wyjście standardowe i błędów są scalone w jeden strumień, aby zachować
    kolejność komunikatów - dokładnie tak, jak widziałby to człowiek w oknie
    CMD.
    """

    def __init__(
        self,
        session_id: str,
        spec: ShellSpec,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        *,
        max_buffer: int = DEFAULT_MAX_BUFFER,
        log_dir: Optional[Path] = None,
    ) -> None:
        self.id = session_id
        self.spec = spec
        self.cwd = str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd()
        if not os.path.isdir(self.cwd):
            raise SessionError(f"Katalog roboczy nie istnieje: {self.cwd}")
        self.env_overrides = dict(env or {})
        self.max_buffer = max_buffer
        self.log_dir = Path(log_dir) / session_id if log_dir else None

        self.created_at = time.time()
        self.last_used = self.created_at
        self.seq = 0
        self.dirty = False

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._run_lock = threading.Lock()
        self._cv = threading.Condition()
        self._buf = ""
        self._dropped = 0          # ile znaków usunięto z początku bufora
        self._protect_from = 0     # bezwzględny offset, którego nie wolno przyciąć
        self._alive = False
        self._exit_code: Optional[int] = None
        self._pending_cr = ""

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Uruchamia proces powłoki i wykonuje polecenia inicjalizujące."""
        env = os.environ.copy()
        env.update(self.env_overrides)

        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(
            self.spec.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=env,
            bufsize=0,
            **kwargs,
        )
        self._alive = True
        self._reader = threading.Thread(
            target=self._read_loop, name=f"cmdbridge-reader-{self.id}", daemon=True
        )
        self._reader.start()

        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        # Rozgrzewka: odrzucamy baner powłoki i ustawiamy tryb pracy.
        for command in list(self.spec.init_commands) + [self._noop_command()]:
            try:
                self._execute(command, timeout=15.0, internal=True)
            except SessionError:
                break

    def _noop_command(self) -> str:
        return "rem cmdbridge-ready" if self.spec.name == "cmd" else ":"

    # ------------------------------------------------------------------ reader

    def _read_loop(self) -> None:
        stream = self._proc.stdout  # type: ignore[union-attr]
        decoder = codecs.getincrementaldecoder(self.spec.encoding)(errors="replace")
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                self._append(decoder.decode(chunk))
        except (OSError, ValueError):
            pass
        finally:
            tail = self._pending_cr + decoder.decode(b"", True)
            self._pending_cr = ""
            if tail:
                self._append(tail, raw=True)
            try:
                stream.close()
            except OSError:
                pass
            code = self._proc.wait() if self._proc else None
            with self._cv:
                self._alive = False
                self._exit_code = code
                self._cv.notify_all()

    def _append(self, text: str, *, raw: bool = False) -> None:
        if not text:
            return
        if not raw:
            text = self._pending_cr + text
            self._pending_cr = ""
            if text.endswith("\r"):
                text = text[:-1]
                self._pending_cr = "\r"
            text = text.replace("\r\n", "\n")
        with self._cv:
            self._buf += text
            self._trim_locked()
            self._cv.notify_all()

    def _trim_locked(self) -> None:
        overflow = len(self._buf) - self.max_buffer
        if overflow <= 0:
            return
        # Nigdy nie przycinamy fragmentu należącego do trwającego polecenia.
        keep_from = max(0, min(overflow, self._protect_from - self._dropped))
        if keep_from <= 0:
            return
        self._buf = self._buf[keep_from:]
        self._dropped += keep_from

    # ------------------------------------------------------------------- state

    @property
    def alive(self) -> bool:
        with self._cv:
            return self._alive

    @property
    def busy(self) -> bool:
        locked = self._run_lock.acquire(blocking=False)
        if locked:
            self._run_lock.release()
            return False
        return True

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def info(self) -> dict:
        return {
            "id": self.id,
            "shell": self.spec.name,
            "cwd": self.cwd,
            "pid": self.pid,
            "alive": self.alive,
            "busy": self.busy,
            "dirty": self.dirty,
            "commands": self.seq,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "output_offset": self.output_offset,
        }

    @property
    def output_offset(self) -> int:
        with self._cv:
            return self._dropped + len(self._buf)

    # --------------------------------------------------------------- execution

    def run(self, command: str, timeout: Optional[float] = None) -> RunResult:
        """Wykonuje polecenie i zwraca jego pełne wyjście."""
        if not self.alive:
            raise SessionDead("Sesja nie działa")
        if self.dirty:
            raise SessionDirty("Sesja wymaga restartu")
        if not self._run_lock.acquire(blocking=False):
            raise SessionBusy("Sesja zajęta")
        try:
            return self._execute(command, timeout=timeout or DEFAULT_TIMEOUT)
        finally:
            self._run_lock.release()

    def _execute(self, command: str, timeout: float, *, internal: bool = False) -> RunResult:
        marker = MARKER_PREFIX + uuid.uuid4().hex
        payload = (
            command.rstrip("\r\n")
            + self.spec.line_ending
            + self.spec.marker_command(marker)
            + self.spec.line_ending
        )

        with self._cv:
            start = self._dropped + len(self._buf)
            self._protect_from = start

        started = time.time()
        self._write(payload)

        found = self._wait_for_marker(marker, start, timeout)
        timed_out = found is None
        recovered = False

        if timed_out:
            # Próba odzyskania sesji: wysyłamy Ctrl+Break / SIGINT i czekamy chwilę.
            self.interrupt()
            found = self._wait_for_marker(marker, start, INTERRUPT_GRACE)
            recovered = found is not None
            if not recovered:
                self.dirty = True

        duration = time.time() - started

        if found is None:
            with self._cv:
                output = self._buf[max(0, start - self._dropped):]
            exit_code, cwd = None, self.cwd
        else:
            marker_at, line_end = found
            with self._cv:
                base = self._dropped
                output = self._buf[max(0, start - base):marker_at - base]
                line = self._buf[marker_at - base:line_end - base]
                self._protect_from = line_end + 1
            exit_code, cwd = self._parse_marker(line, marker)
            self.cwd = cwd

        # Znaczniki zaległych poleceń (np. po timeoucie) nie trafiają do wyniku.
        output, _ = strip_markers(output)
        if output.endswith("\n"):
            output = output[:-1]

        self.last_used = time.time()
        if internal:
            return RunResult(self.id, 0, command, "", exit_code, self.cwd, duration)

        self.seq += 1
        result = RunResult(
            session=self.id,
            seq=self.seq,
            command=command,
            output=output,
            exit_code=exit_code,
            cwd=self.cwd,
            duration=duration,
            timed_out=timed_out,
            recovered=recovered,
        )
        result.log_file = self._write_log(result)
        return result

    def _parse_marker(self, line: str, marker: str) -> tuple[Optional[int], str]:
        parts = line.strip().split(" ", 2)
        exit_code: Optional[int] = None
        cwd = self.cwd
        if len(parts) >= 2:
            try:
                exit_code = int(parts[1])
            except ValueError:
                exit_code = None
        if len(parts) >= 3 and parts[2].strip():
            cwd = parts[2].strip()
        return exit_code, cwd

    def _wait_for_marker(self, marker: str, start: int, timeout: float):
        """Czeka na pełną (zakończoną znakiem nowej linii) linię znacznika.

        Zwraca ``(offset_znacznika, offset_konca_linii)`` albo ``None``.
        """
        deadline = time.time() + timeout
        with self._cv:
            while True:
                base = self._dropped
                window = self._buf[max(0, start - base):]
                idx = window.find(marker)
                if idx != -1:
                    nl = window.find("\n", idx)
                    if nl != -1:
                        abs_marker = max(start, base) + idx
                        abs_end = max(start, base) + nl
                        return abs_marker, abs_end
                if not self._alive:
                    return None
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.25))

    def _write(self, data: str) -> None:
        if not self._proc or not self._proc.stdin:
            raise SessionDead("Brak procesu powłoki")
        try:
            self._proc.stdin.write(data.encode(self.spec.encoding, errors="replace"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SessionDead(f"Nie można pisać do powłoki: {exc}") from exc

    def write_stdin(self, data: str) -> None:
        """Wysyła surowy tekst na wejście powłoki (np. odpowiedź na pytanie)."""
        if not self.alive:
            raise SessionDead("Sesja nie działa")
        self._write(data)
        self.last_used = time.time()

    def read_output(self, since: int = 0) -> dict:
        """Zwraca wyjście od bezwzględnego offsetu ``since`` (tryb strumieniowy)."""
        with self._cv:
            base = self._dropped
            end = base + len(self._buf)
            lost = max(0, base - since)
            text = self._buf[max(0, since - base):]
        text, held_back = strip_markers(text)
        return {"text": text, "offset": end - held_back, "lost_chars": lost}

    # ------------------------------------------------------------------ control

    def interrupt(self) -> bool:
        """Przerywa program działający w powłoce, nie zabijając samej powłoki.

        Sygnał trafia wyłącznie do procesów potomnych - dzięki temu po timeoucie
        sesja zwykle wraca do użytku bez restartu.
        """
        if not self._proc or not self.alive:
            return False
        try:
            return interrupt_children(self._proc.pid) > 0
        except Exception:  # pragma: no cover - zależne od systemu
            return False

    def stop(self, timeout: float = 5.0) -> None:
        """Zamyka sesję wraz z całym drzewem procesów potomnych."""
        proc = self._proc
        if not proc:
            return
        try:
            if proc.poll() is None and proc.stdin:
                try:
                    proc.stdin.write(("exit" + self.spec.line_ending).encode(
                        self.spec.encoding, errors="replace"))
                    proc.stdin.flush()
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_tree()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if self._reader and self._reader.is_alive():
                self._reader.join(timeout=1.0)
            with self._cv:
                self._alive = False
                self._cv.notify_all()

    def _kill_tree(self) -> None:
        proc = self._proc
        if not proc:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()

    # ---------------------------------------------------------------- logowanie

    def _write_log(self, result: RunResult) -> Optional[str]:
        if not self.log_dir:
            return None
        path = self.log_dir / f"{result.seq:05d}.log"
        header = (
            f"# session : {self.id}\n"
            f"# command : {result.command}\n"
            f"# cwd     : {result.cwd}\n"
            f"# exit    : {result.exit_code}\n"
            f"# seconds : {result.duration:.3f}\n"
            f"{'-' * 60}\n"
        )
        try:
            path.write_text(header + result.output + "\n", encoding="utf-8", errors="replace")
        except OSError:
            return None
        return str(path)


class SessionManager:
    """Rejestr sesji powłoki - tworzenie, wyszukiwanie, restart, zamykanie."""

    def __init__(
        self,
        spec: ShellSpec,
        *,
        default_cwd: Optional[str] = None,
        max_sessions: int = 8,
        log_dir: Optional[Path] = None,
        max_buffer: int = DEFAULT_MAX_BUFFER,
    ) -> None:
        self.spec = spec
        self.default_cwd = default_cwd or os.getcwd()
        self.max_sessions = max_sessions
        self.log_dir = log_dir
        self.max_buffer = max_buffer
        self._sessions: Dict[str, ShellSession] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def create(
        self,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        shell: Optional[str] = None,
    ) -> ShellSession:
        spec = resolve_shell(shell) if shell else self.spec
        with self._lock:
            live = [s for s in self._sessions.values() if s.alive]
            if len(live) >= self.max_sessions:
                raise SessionError(
                    f"Osiągnięto limit {self.max_sessions} sesji - zamknij którąś (kill)"
                )
            if name and name in self._sessions and self._sessions[name].alive:
                raise SessionError(f"Sesja {name!r} już istnieje")
            if not name:
                self._counter += 1
                name = f"cmd{self._counter}"
            session = ShellSession(
                name,
                spec,
                cwd or self.default_cwd,
                env,
                max_buffer=self.max_buffer,
                log_dir=self.log_dir,
            )
            self._sessions[name] = session
        session.start()
        return session

    def get(self, name: Optional[str] = None, *, create: bool = True) -> ShellSession:
        """Zwraca sesję o podanej nazwie; domyślnie sesję ``main``."""
        name = name or "main"
        with self._lock:
            session = self._sessions.get(name)
        if session and session.alive:
            return session
        if session and not session.alive:
            if not create:
                raise SessionDead(f"Sesja {name!r} nie działa")
            return self.restart(name)
        if not create:
            raise SessionError(f"Nie ma sesji {name!r}")
        return self.create(name=name)

    def restart(self, name: str) -> ShellSession:
        """Zamyka istniejącą sesję i startuje nowy proces powłoki (nowy CMD)."""
        with self._lock:
            old = self._sessions.get(name)
        cwd = old.cwd if old else self.default_cwd
        env = old.env_overrides if old else None
        spec = old.spec if old else self.spec
        if old:
            old.stop()
        session = ShellSession(
            name, spec, cwd, env, max_buffer=self.max_buffer, log_dir=self.log_dir
        )
        with self._lock:
            self._sessions[name] = session
        session.start()
        return session

    def kill(self, name: str) -> bool:
        with self._lock:
            session = self._sessions.pop(name, None)
        if not session:
            return False
        session.stop()
        return True

    def list(self) -> List[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.info() for s in sessions]

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.stop(timeout=2.0)
            except Exception:  # pragma: no cover - sprzątanie
                pass
