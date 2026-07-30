"""Widoki wyjścia: zamiast całego logu most zwraca tylko to, o co poproszono.

Wszystkie widoki czytają log **strumieniowo**, linia po linii, i trzymają w
pamięci wyłącznie tyle, ile mają zwrócić. Dzięki temu przefiltrowanie logu
z buildu ważącego setki megabajtów kosztuje stałą ilość pamięci — zarówno po
stronie mostu, jak i w kontekście modelu AI.

Dostępne widoki (pole ``view`` w żądaniu):

======== ==============================================================
full     całość, przycięta do ``max_chars`` (początek + koniec)
tail     ostatnie ``lines`` linii
head     pierwsze ``lines`` linii
grep     linie pasujące do ``pattern`` (+ ``context`` linii otoczenia)
around   okolice linii ``line`` (nawigacja po numerach z widoku errors)
errors   wyciąg błędów/ostrzeżeń rozpoznanych przez ekstraktory
summary  jak errors, ale bez surowego tekstu - sam werdykt i liczby
quiet    nic poza statystykami (kod wyjścia, liczba linii)
======== ==============================================================
"""

from __future__ import annotations

import io
import re
from collections import deque
from typing import Any, Dict, Iterator, Optional, Tuple

from .shell import MARKER_PREFIX

HEADER_SENTINEL = "#!cmdbridge-log"
HEADER_END = "#--- output ---"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

DEFAULT_MAX_CHARS = 20_000
DEFAULT_LINES = 100
MAX_LINES = 5_000

VIEWS = ("full", "tail", "head", "grep", "around", "errors", "summary", "quiet")


class ViewError(ValueError):
    """Nieprawidłowe parametry widoku (zwracane jako 400)."""


def clean_line(line: str) -> str:
    """Usuwa kody ANSI i zwija paski postępu przepisywane znakiem ``\\r``."""
    line = line.rstrip("\n")
    if "\r" in line:
        segments = [seg for seg in line.split("\r") if seg.strip()]
        line = segments[-1] if segments else ""
    if "\x1b" in line:
        line = ANSI_RE.sub("", line)
    return line


def _open(source: Dict[str, Any]):
    path = source.get("path")
    if path:
        return open(path, "r", encoding="utf-8", errors="replace")
    return io.StringIO(source.get("text") or "")


def iter_lines(source: Dict[str, Any], *, raw: bool = False) -> Iterator[Tuple[int, str]]:
    """Zwraca kolejne linie wyjścia: ``(numer, treść)``, numerując od 1.

    Pomija nagłówek pliku logu oraz wewnętrzne linie znacznika mostu, więc
    numery linii są tym, co użytkownik faktycznie zobaczyłby w konsoli.
    """
    handle = _open(source)
    try:
        first = handle.readline()
        if first and first.rstrip("\n") == HEADER_SENTINEL:
            for line in handle:  # przewijamy nagłówek do separatora
                if line.rstrip("\n") == HEADER_END:
                    break
            pending = None
        else:
            pending = first if first else None

        number = 0
        while True:
            line = pending if pending is not None else handle.readline()
            pending = None
            if not line:
                break
            if MARKER_PREFIX in line:
                continue
            number += 1
            yield number, line.rstrip("\n") if raw else clean_line(line)
    finally:
        handle.close()


def _int(value: Any, default: int, *, minimum: int = 1, maximum: int = MAX_LINES) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


# --------------------------------------------------------------------- widoki


def _view_full(source, max_chars: int, raw: bool) -> Dict[str, Any]:
    head: list = []
    head_chars = 0
    tail: deque = deque()
    tail_chars = 0
    tail_budget = max(200, max_chars // 3)
    total_lines = 0
    total_chars = 0

    for _, line in iter_lines(source, raw=raw):
        total_lines += 1
        total_chars += len(line) + 1
        if head_chars <= max_chars:
            head.append(line)
            head_chars += len(line) + 1
        tail.append(line)
        tail_chars += len(line) + 1
        while tail_chars > tail_budget and len(tail) > 1:
            tail_chars -= len(tail.popleft()) + 1

    if total_chars <= max_chars:
        return {
            "text": "\n".join(head),
            "lines_total": total_lines,
            "lines_shown": total_lines,
            "truncated": False,
        }

    head_budget = max(0, max_chars - tail_budget)
    kept, kept_chars = [], 0
    for line in head:
        if kept_chars + len(line) + 1 > head_budget:
            break
        kept.append(line)
        kept_chars += len(line) + 1
    hidden = total_lines - len(kept) - len(tail)
    gap = (
        f"\n...[pominięto {max(hidden, 0)} linii z {total_lines}; "
        f"użyj view=errors, grep lub around zamiast czytać całość]...\n"
    )
    return {
        "text": "\n".join(kept) + gap + "\n".join(tail),
        "lines_total": total_lines,
        "lines_shown": len(kept) + len(tail),
        "truncated": True,
    }


def _view_tail(source, lines: int, raw: bool) -> Dict[str, Any]:
    buffer: deque = deque(maxlen=lines)
    total = 0
    for _, line in iter_lines(source, raw=raw):
        total += 1
        buffer.append(line)
    return {
        "text": "\n".join(buffer),
        "lines_total": total,
        "lines_shown": len(buffer),
        "truncated": total > len(buffer),
        "first_line_shown": max(1, total - len(buffer) + 1),
    }


def _view_head(source, lines: int, raw: bool) -> Dict[str, Any]:
    collected: list = []
    total = 0
    for _, line in iter_lines(source, raw=raw):
        total += 1
        if len(collected) < lines:
            collected.append(line)
    return {
        "text": "\n".join(collected),
        "lines_total": total,
        "lines_shown": len(collected),
        "truncated": total > len(collected),
    }


def _view_grep(source, spec: Dict[str, Any], raw: bool) -> Dict[str, Any]:
    pattern = spec.get("pattern")
    if not pattern:
        raise ViewError("Widok 'grep' wymaga pola 'pattern' (wyrażenie regularne)")
    flags = 0 if spec.get("case_sensitive") else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ViewError(f"Nieprawidłowe wyrażenie regularne: {exc}") from exc

    context = _int(spec.get("context"), 0, minimum=0, maximum=50)
    limit = _int(spec.get("lines"), DEFAULT_LINES)
    invert = bool(spec.get("invert"))

    before: deque = deque(maxlen=context)
    out: list = []
    after_left = 0
    matches = 0
    total = 0
    last_emitted = 0
    capped = False

    for number, line in iter_lines(source, raw=raw):
        total += 1
        hit = bool(regex.search(line)) != invert
        if hit:
            matches += 1
            if len(out) < limit:
                for offset, prev in enumerate(before, start=number - len(before)):
                    if offset > last_emitted:
                        out.append(f"{offset}: {prev}")
                        last_emitted = offset
                if number > last_emitted:
                    out.append(f"{number}: {line}")
                    last_emitted = number
                after_left = context
            else:
                capped = True
        elif after_left and len(out) < limit:
            if number > last_emitted:
                out.append(f"{number}: {line}")
                last_emitted = number
            after_left -= 1
        before.append(line)

    return {
        "text": "\n".join(out),
        "lines_total": total,
        "lines_shown": len(out),
        "matches": matches,
        "truncated": capped,
        "numbered": True,
    }


def _view_around(source, spec: Dict[str, Any], raw: bool) -> Dict[str, Any]:
    target = spec.get("line")
    if target is None:
        raise ViewError("Widok 'around' wymaga pola 'line' (numer linii w logu)")
    target = _int(target, 1, minimum=1, maximum=10_000_000)
    context = _int(spec.get("context"), 20, minimum=1, maximum=500)
    low, high = target - context, target + context

    out: list = []
    total = 0
    for number, line in iter_lines(source, raw=raw):
        total += 1
        if low <= number <= high:
            out.append(f"{number}: {line}")
    return {
        "text": "\n".join(out),
        "lines_total": total,
        "lines_shown": len(out),
        "truncated": total > len(out),
        "numbered": True,
        "around_line": target,
    }


def render(source: Dict[str, Any], spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Buduje żądany widok wyjścia. ``source`` to ``{"path": ...}`` lub ``{"text": ...}``."""
    spec = spec or {}
    view = str(spec.get("view") or "full").lower()
    if view not in VIEWS:
        raise ViewError(f"Nieznany widok {view!r}; dostępne: {', '.join(VIEWS)}")

    raw = bool(spec.get("raw"))
    max_chars = _int(spec.get("max_chars"), DEFAULT_MAX_CHARS, minimum=0, maximum=2_000_000)
    lines = _int(spec.get("lines"), DEFAULT_LINES)

    if view == "full":
        result = _view_full(source, max_chars or DEFAULT_MAX_CHARS, raw)
    elif view == "tail":
        result = _view_tail(source, lines, raw)
    elif view == "head":
        result = _view_head(source, lines, raw)
    elif view == "grep":
        result = _view_grep(source, spec, raw)
    elif view == "around":
        result = _view_around(source, spec, raw)
    elif view == "quiet":
        total = sum(1 for _ in iter_lines(source, raw=raw))
        result = {"text": "", "lines_total": total, "lines_shown": 0, "truncated": total > 0}
    else:  # errors / summary
        from .extractors import extract  # import lokalny: unikamy cyklu

        result = extract(source, spec, raw=raw, with_text=(view == "errors"))

    result["view"] = view
    # 'full' pilnuje własnego budżetu (początek + koniec), a w 'tail' liczy się
    # koniec - dlatego twarde przycięcie działa tu z różnych stron.
    text = result.get("text", "")
    if view not in ("full", "errors", "summary", "quiet") and max_chars > 0 and len(text) > max_chars:
        cut = len(text) - max_chars
        if view == "tail":
            result["text"] = f"...[ucięto {cut} znaków z początku]...\n" + text[-max_chars:]
        else:
            result["text"] = text[:max_chars] + f"\n...[ucięto {cut} znaków]..."
        result["truncated"] = True
    return result
