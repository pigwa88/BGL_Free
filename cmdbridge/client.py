"""Prosty klient CLI mostu - do testów ręcznych i skryptów.

Przykłady::

    python -m cmdbridge.client status
    python -m cmdbridge.client run "dir"
    python -m cmdbridge.client run --session build "cd .. && dir"
    python -m cmdbridge.client restart --session main
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .core import default_state_dir


def load_connection(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Wczytuje adres i token zapisane przez działający most."""
    base = Path(state_dir).expanduser() if state_dir else default_state_dir()
    path = base / "bridge.json"
    if not path.exists():
        raise SystemExit(
            f"Nie znaleziono {path} - czy most działa? Uruchom: python -m cmdbridge"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def request(
    url: str,
    token: Optional[str],
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    timeout: float = 3700.0,
) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("X-Bridge-Token", token)
    # Most jest lokalny - pomijamy ewentualne proxy systemowe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise SystemExit(f"Brak połączenia z mostem: {exc.reason}") from exc
    try:
        return status, json.loads(payload)
    except json.JSONDecodeError:
        return status, payload


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="cmdbridge.client", description="Klient mostu CMD")
    parser.add_argument("--state-dir", help="Katalog stanu mostu")
    parser.add_argument("--session", default="main", help="Nazwa sesji")
    parser.add_argument("--timeout", type=float, help="Limit czasu polecenia")
    parser.add_argument("--raw", action="store_true", help="Wypisz surowy JSON")

    # Te same opcje wolno podać także po nazwie operacji (SUPPRESS sprawia,
    # że brak opcji w podpoleceniu nie kasuje wartości podanej wcześniej).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default=argparse.SUPPRESS, help="Nazwa sesji")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                        help="Limit czasu polecenia")
    common.add_argument("--raw", action="store_true", default=argparse.SUPPRESS,
                        help="Wypisz surowy JSON")

    sub = parser.add_subparsers(dest="op", required=True)

    run = sub.add_parser("run", parents=[common], help="Wykonaj polecenie")
    run.add_argument("command", nargs="+")
    sub.add_parser("status", parents=[common], help="Stan mostu")
    sub.add_parser("sessions", parents=[common], help="Lista sesji")
    sub.add_parser("new", parents=[common], help="Nowa sesja CMD")
    sub.add_parser("restart", parents=[common], help="Restart sesji (nowy CMD)")
    sub.add_parser("kill", parents=[common], help="Zamknij sesję")
    sub.add_parser("interrupt", parents=[common],
                   help="Przerwij bieżące polecenie (Ctrl+Break)")
    out = sub.add_parser("output", parents=[common], help="Nowe wyjście sesji")
    out.add_argument("--since", type=int, default=0)
    stdin_cmd = sub.add_parser("stdin", parents=[common],
                               help="Wyślij tekst na wejście powłoki")
    stdin_cmd.add_argument("data")

    args = parser.parse_args(argv)
    conn = load_connection(args.state_dir)
    url, token = conn.get("url"), conn.get("token")
    if not url:
        raise SystemExit("Most działa tylko w trybie plikowym - użyj katalogu mailbox")

    if args.op == "run":
        body: Dict[str, Any] = {"command": " ".join(args.command), "session": args.session}
        if args.timeout:
            body["timeout"] = args.timeout
        status, data = request(url, token, "/run", "POST", body)
    elif args.op == "status":
        status, data = request(url, token, "/health")
    elif args.op == "sessions":
        status, data = request(url, token, "/sessions")
    elif args.op == "new":
        status, data = request(url, token, "/sessions", "POST", {"session": args.session})
    elif args.op == "restart":
        status, data = request(url, token, f"/sessions/{args.session}/restart", "POST", {})
    elif args.op == "kill":
        status, data = request(url, token, f"/sessions/{args.session}", "DELETE")
    elif args.op == "interrupt":
        status, data = request(url, token, f"/sessions/{args.session}/interrupt", "POST", {})
    elif args.op == "output":
        status, data = request(url, token, f"/sessions/{args.session}/output?since={args.since}")
    elif args.op == "stdin":
        status, data = request(url, token, f"/sessions/{args.session}/stdin", "POST",
                               {"data": args.data})
    else:  # pragma: no cover - argparse pilnuje
        parser.error("nieznana operacja")

    if args.raw or not isinstance(data, dict):
        print(json.dumps(data, ensure_ascii=False, indent=2)
              if isinstance(data, (dict, list)) else data)
    elif args.op == "run" and data.get("ok"):
        if data.get("output"):
            print(data["output"])
        print(f"[exit={data.get('exit_code')} cwd={data.get('cwd')} "
              f"czas={data.get('duration')}s]", file=sys.stderr)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))

    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
