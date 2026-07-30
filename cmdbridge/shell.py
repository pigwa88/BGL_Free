"""Definicje powłok obsługiwanych przez most (cmd.exe, PowerShell, sh/bash).

Każda powłoka opisana jest przez:
  * argv          - jak uruchomić proces powłoki czytający polecenia ze stdin,
  * marker_line   - polecenie drukujące znacznik końca komendy wraz z kodem
                    wyjścia i bieżącym katalogiem,
  * line_ending   - separator linii oczekiwany przez powłokę,
  * init_commands - polecenia "rozgrzewające" wykonywane po starcie sesji
                    (ich wyjście jest odrzucane).

Protokół znacznika: po każdym poleceniu użytkownika wysyłamy dodatkową linię,
która drukuje ``<MARKER> <kod_wyjścia> <katalog>``. Wszystko, co pojawi się na
wyjściu przed tą linią, jest odpowiedzią na polecenie.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import List

MARKER_PREFIX = "__CMDBRIDGE__"


@dataclass(frozen=True)
class ShellSpec:
    """Opis sposobu sterowania konkretną powłoką."""

    name: str
    argv: List[str]
    marker_line: str
    line_ending: str = "\n"
    encoding: str = "utf-8"
    init_commands: List[str] = field(default_factory=list)

    def marker_command(self, marker: str) -> str:
        """Zwraca linię drukującą znacznik ``marker``."""
        return self.marker_line.replace("{marker}", marker)


def _oem_encoding() -> str:
    """Strona kodowa konsoli Windows (np. cp852 na polskim systemie)."""
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - tylko Windows
        return "cp437"


def _cmd_spec(utf8: bool, encoding: str | None) -> ShellSpec:
    init = ["@echo off"]
    if utf8:
        init.append("chcp 65001>nul")
    return ShellSpec(
        name="cmd",
        # /Q wyłącza echo (brak promptu w wyjściu), /D pomija skrypty AutoRun.
        # Bez /C i /K cmd.exe czyta kolejne polecenia ze stdin aż do 'exit'.
        argv=["cmd.exe", "/Q", "/D"],
        marker_line="@echo {marker} %ERRORLEVEL% %CD%",
        line_ending="\r\n",
        encoding=encoding or ("utf-8" if utf8 else _oem_encoding()),
        init_commands=init,
    )


def _powershell_spec(encoding: str | None) -> ShellSpec:
    exe = shutil.which("pwsh") or "powershell.exe"
    marker = (
        "Write-Output \"{marker} "
        "$(if ($LASTEXITCODE -ne $null -and -not $?) { $LASTEXITCODE } "
        "elseif ($?) { 0 } else { 1 }) $($PWD.Path)\""
    )
    return ShellSpec(
        name="powershell",
        argv=[exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"],
        marker_line=marker,
        line_ending="\r\n",
        encoding=encoding or "utf-8",
        init_commands=["$ProgressPreference = 'SilentlyContinue'"],
    )


def _posix_spec(encoding: str | None) -> ShellSpec:
    exe = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return ShellSpec(
        name="sh",
        argv=[exe],
        marker_line='echo "{marker} $? $PWD"',
        line_ending="\n",
        encoding=encoding or "utf-8",
        init_commands=[],
    )


def available_shells() -> List[str]:
    """Nazwy powłok możliwych do uruchomienia na tym systemie."""
    if os.name == "nt":
        return ["cmd", "powershell"]
    return ["sh"]


def resolve_shell(name: str = "auto", *, utf8: bool = True,
                  encoding: str | None = None) -> ShellSpec:
    """Zwraca ``ShellSpec`` dla podanej nazwy powłoki.

    ``auto`` wybiera cmd.exe na Windows, a sh/bash na pozostałych systemach
    (dzięki temu most działa i jest testowalny także poza Windows).
    """
    name = (name or "auto").lower()
    if name == "auto":
        name = "cmd" if os.name == "nt" else "sh"

    if name == "cmd":
        if os.name != "nt":
            raise ValueError("Powłoka 'cmd' jest dostępna tylko na Windows")
        return _cmd_spec(utf8, encoding)
    if name in ("powershell", "pwsh"):
        return _powershell_spec(encoding)
    if name in ("sh", "bash"):
        if os.name == "nt":
            raise ValueError("Powłoka 'sh' nie jest dostępna na Windows")
        return _posix_spec(encoding)

    raise ValueError(
        f"Nieznana powłoka {name!r}; dostępne: {', '.join(available_shells())}"
    )
