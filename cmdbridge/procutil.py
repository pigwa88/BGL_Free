"""Operacje na drzewie procesów potomnych powłoki (bez zależności zewnętrznych).

Przerwanie polecenia musi ubić *program uruchomiony w powłoce*, a nie samą
powłokę - inaczej każdy timeout kończyłby sesję. Dlatego szukamy potomków
procesu powłoki i sygnał wysyłamy tylko do nich.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Dict, List


def _ps_parent_map() -> Dict[int, int]:
    """Mapa pid -> ppid zbudowana na podstawie ``ps`` (POSIX)."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return _proc_parent_map()
    parents: Dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                parents[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    return parents or _proc_parent_map()


def _proc_parent_map() -> Dict[int, int]:
    """Zapasowa mapa pid -> ppid czytana z /proc (Linux)."""
    parents: Dict[int, int] = {}
    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return parents
    for entry in os.listdir(proc_dir):
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc_dir}/{entry}/stat", "r", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[-1].split()
            parents[int(entry)] = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
    return parents


def _windows_children(pid: int) -> List[int]:
    commands = [
        ["wmic", "process", "where", f"ParentProcessId={pid}", "get", "ProcessId"],
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' "
         "| Select-Object -ExpandProperty ProcessId"],
    ]
    for command in commands:
        try:
            out = subprocess.run(
                command, capture_output=True, text=True, timeout=20, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = [int(tok) for tok in out.split() if tok.isdigit() and int(tok) != pid]
        if found:
            return found
    return []


def descendants(pid: int) -> List[int]:
    """Zwraca potomków procesu, od najgłębszych do najpłytszych."""
    if os.name == "nt":
        return _windows_children(pid)

    parents = _ps_parent_map()
    children: Dict[int, List[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)

    ordered: List[int] = []
    stack = list(children.get(pid, []))
    while stack:
        current = stack.pop()
        ordered.append(current)
        stack.extend(children.get(current, []))
    ordered.reverse()  # najgłębsze procesy jako pierwsze
    return ordered


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def interrupt_children(pid: int, *, grace: float = 1.5) -> int:
    """Przerywa programy uruchomione w powłoce, zostawiając samą powłokę.

    Zwraca liczbę procesów, do których wysłano sygnał.
    """
    targets = descendants(pid)
    if not targets:
        return 0

    if os.name == "nt":
        for target in targets:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return len(targets)

    for target in targets:
        try:
            os.kill(target, signal.SIGINT)
        except (OSError, ProcessLookupError):
            continue

    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(_alive(target) for target in targets):
            return len(targets)
        time.sleep(0.1)

    for escalation in (signal.SIGTERM, signal.SIGKILL):
        for target in targets:
            if _alive(target):
                try:
                    os.kill(target, escalation)
                except (OSError, ProcessLookupError):
                    continue
        time.sleep(0.2)
    return len(targets)
