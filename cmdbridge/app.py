"""Punkt wejścia aplikacji - konfiguracja i uruchomienie mostu."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

from . import __version__
from .audit import AuditLog
from .core import Bridge, ConfirmGate, default_state_dir, find_instructions
from .mailbox import Mailbox
from .policy import Policy, parse_rule_file
from .server import BridgeServer
from .session import DEFAULT_TIMEOUT, SessionManager
from .shell import available_shells, resolve_shell
from .tunnel import RelayTunnel

INSTRUCTIONS_FILE = find_instructions()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmdbridge",
        description="Most między CMD a modelem AI bez bezpośredniego dostępu do powłoki.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Adres nasłuchu (domyślnie tylko lokalny)")
    parser.add_argument("--port", type=int, default=8765, help="Port API HTTP")
    parser.add_argument("--token", help="Token dostępu (domyślnie losowy)")
    parser.add_argument("--no-auth", action="store_true",
                        help="Wyłącza token (tylko dla zaufanego środowiska)")
    parser.add_argument("--shell", default="auto",
                        help="Powłoka: auto|" + "|".join(available_shells()))
    parser.add_argument("--cwd", default=os.getcwd(), help="Domyślny katalog roboczy sesji")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="Domyślny limit czasu polecenia w sekundach")
    parser.add_argument("--max-output", type=int, default=20_000,
                        help="Maksymalna liczba znaków wyjścia w odpowiedzi")
    parser.add_argument("--max-sessions", type=int, default=8,
                        help="Maksymalna liczba równoległych sesji CMD")
    parser.add_argument("--state-dir", default=None,
                        help="Katalog na logi, audyt i dane połączenia")
    parser.add_argument("--mailbox", nargs="?", const="", default=None,
                        help="Włącza tryb plikowy (katalog wymiany; domyślnie <state-dir>/mailbox)")
    parser.add_argument("--no-server", action="store_true",
                        help="Nie uruchamiaj API HTTP (tylko tryb plikowy)")
    parser.add_argument("--relay", metavar="URL",
                        help="Podłącz się do publicznego relaya pod tym adresem "
                             "(np. https://twoja-domena) - daje AI zdalny dostęp przez link")
    parser.add_argument("--relay-token", metavar="TOKEN",
                        help="Token łącza relaya (connect-token wypisany przez cmdbridge.relay)")
    parser.add_argument("--relay-jobs", type=int, default=4,
                        help="Ile poleceń z relaya wykonywać równolegle")
    parser.add_argument("--allow-dangerous", action="store_true",
                        help="Wyłącza blokadę poleceń niszczących")
    parser.add_argument("--disable-rule", action="append", default=[],
                        help="Wyłącza pojedynczą regułę bezpieczeństwa (można powtarzać)")
    parser.add_argument("--rules-file", help="Plik z dodatkowymi regułami 'nazwa = wyrażenie'")
    parser.add_argument("--confirm", action="store_true",
                        help="Każde polecenie wymaga potwierdzenia w konsoli")
    parser.add_argument("--no-utf8", action="store_true",
                        help="Nie ustawiaj strony kodowej 65001 w cmd.exe")
    parser.add_argument("--encoding", help="Wymuszone kodowanie wyjścia powłoki")
    parser.add_argument("--no-logs", action="store_true",
                        help="Nie zapisuj pełnych logów poleceń na dysk")
    parser.add_argument("--verbose", action="store_true", help="Loguj żądania HTTP")
    parser.add_argument("--print-instructions", action="store_true",
                        help="Wypisz instrukcję dla AI i zakończ")
    parser.add_argument("--version", action="version", version=f"cmdbridge {__version__}")
    return parser


def _write_connection_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _copy_instructions(state_dir: Path) -> Optional[Path]:
    """Kopiuje instrukcję dla AI do katalogu stanu (żeby AI mogło ją odczytać)."""
    if not INSTRUCTIONS_FILE:
        return None
    target = state_dir / "AI_INSTRUCTIONS.md"
    try:
        target.write_text(INSTRUCTIONS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        return INSTRUCTIONS_FILE
    return target


def _banner(server: Optional[BridgeServer], mailbox: Optional[Mailbox],
            token: Optional[str], state_dir: Path, instructions: Optional[Path],
            relay_url: Optional[str] = None) -> None:
    print("=" * 66)
    print(f" CMD Bridge {__version__} - pośrednik CMD <-> AI")
    print("=" * 66)
    if server:
        print(f" API HTTP     : {server.url}")
        print(f" Token        : {token or '(wyłączony)'}")
    if mailbox:
        print(f" Tryb plikowy : {mailbox.root}")
        print(f"   żądania    : {mailbox.inbox}")
        print(f"   odpowiedzi : {mailbox.outbox}")
    if relay_url:
        print(f" Relay (zdaln): {relay_url}  <- AI łączy się przez link relaya")
    print(f" Katalog stanu: {state_dir}")
    if instructions:
        print(f" Instrukcja AI: {instructions}")
    print("-" * 66)
    if relay_url:
        print(" Zdalny dostęp przez relay aktywny. Link z tokenem dostępu dla AI")
        print(f" wypisuje serwer relaya. Tunel łączy się z: {relay_url}")
    else:
        print(" Skopiuj poniższą linię do czatu z AI:")
        if server:
            print(f'   Masz dostęp do CMD przez most HTTP: {server.url} '
                  f'(nagłówek X-Bridge-Token: {token or "brak"}). '
                  f'Instrukcja: GET {server.url}/instructions')
        elif mailbox:
            print(f"   Masz dostęp do CMD: zapisuj żądania JSON w {mailbox.inbox}, "
                  f"odpowiedzi czytaj z {mailbox.outbox}. "
                  f"Instrukcja: {instructions}")
    print("-" * 66)
    print(" Ctrl+C kończy pracę mostu.")
    print("=" * 66, flush=True)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.print_instructions:
        if INSTRUCTIONS_FILE:
            print(INSTRUCTIONS_FILE.read_text(encoding="utf-8"))
            return 0
        print("Nie znaleziono AI_INSTRUCTIONS.md", file=sys.stderr)
        return 1

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    try:
        spec = resolve_shell(args.shell, utf8=not args.no_utf8, encoding=args.encoding)
    except ValueError as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2

    extra_rules = []
    if args.rules_file:
        try:
            extra_rules = parse_rule_file(Path(args.rules_file).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"Nie można wczytać reguł: {exc}", file=sys.stderr)
            return 2

    manager = SessionManager(
        spec,
        default_cwd=str(Path(args.cwd).expanduser().resolve()),
        max_sessions=args.max_sessions,
        log_dir=None if args.no_logs else state_dir / "logs",
    )
    policy = Policy(
        allow_dangerous=args.allow_dangerous,
        extra_rules=extra_rules,
        disabled_rules=args.disable_rule,
    )
    instructions_path = _copy_instructions(state_dir)
    bridge = Bridge(
        manager,
        policy,
        audit=AuditLog(state_dir / "audit.jsonl"),
        confirm=ConfirmGate(args.confirm),
        default_timeout=args.timeout,
        max_output=args.max_output,
        instructions_path=instructions_path or INSTRUCTIONS_FILE,
    )

    token = None if args.no_auth else (args.token or secrets.token_urlsafe(24))

    server: Optional[BridgeServer] = None
    if not args.no_server:
        try:
            server = BridgeServer(bridge, args.host, args.port, token, args.verbose)
        except OSError as exc:
            print(f"Nie można otworzyć portu {args.port}: {exc}", file=sys.stderr)
            manager.shutdown()
            return 2
        server.start()

    mailbox: Optional[Mailbox] = None
    if args.mailbox is not None:
        mailbox_dir = Path(args.mailbox).expanduser() if args.mailbox else state_dir / "mailbox"
        mailbox = Mailbox(bridge, mailbox_dir)
        mailbox.start()

    tunnel: Optional[RelayTunnel] = None
    if args.relay:
        if not args.relay_token:
            print("--relay wymaga --relay-token (token łącza z serwera relaya)",
                  file=sys.stderr)
            if server:
                server.stop()
            if mailbox:
                mailbox.stop()
            manager.shutdown()
            return 2
        tunnel = RelayTunnel(
            bridge, args.relay, args.relay_token,
            max_concurrency=args.relay_jobs, verbose=args.verbose,
        )
        tunnel.start()

    if not server and not mailbox and not tunnel:
        print("Nic do uruchomienia: --no-server wymaga --mailbox lub --relay",
              file=sys.stderr)
        manager.shutdown()
        return 2

    _write_connection_file(
        state_dir / "bridge.json",
        {
            "version": __version__,
            "pid": os.getpid(),
            "url": server.url if server else None,
            "token": token,
            "shell": spec.name,
            "cwd": manager.default_cwd,
            "mailbox": str(mailbox.root) if mailbox else None,
            "relay": args.relay if tunnel else None,
            "instructions": str(instructions_path) if instructions_path else None,
        },
    )

    _banner(server, mailbox, token, state_dir, instructions_path,
            relay_url=args.relay if tunnel else None)

    stop = threading.Event()

    def _handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):  # pragma: no cover - Windows
        pass

    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        print("\n[cmdbridge] Zamykanie...", flush=True)
        if tunnel:
            tunnel.stop()
        if mailbox:
            mailbox.stop()
        if server:
            server.stop()
        bridge.shutdown()
        try:
            (state_dir / "bridge.json").unlink()
        except OSError:
            pass
    return 0
