"""Zasady bezpieczeństwa: blokada poleceń nieodwracalnie niszczących system."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Pattern, Tuple

# Wzorce blokowane domyślnie. Celem nie jest piaskownica (powłoka z definicji
# daje pełne możliwości), tylko zatrzymanie oczywistych pomyłek AI, których nie
# da się cofnąć.
DEFAULT_RULES: List[Tuple[str, str]] = [
    ("format-drive", r"\bformat\s+[a-z]:"),
    ("diskpart", r"\bdiskpart\b"),
    ("wipe-disk", r"\b(cipher\s+/w|sdelete\b)"),
    ("delete-system-root", r"\b(del|erase)\b[^\n]*\s/s\b[^\n]*\s[a-z]:\\?(\s|$)"),
    ("rmdir-system-root", r"\b(rd|rmdir)\b[^\n]*\s/s\b[^\n]*\s[a-z]:\\?(\s|$)"),
    ("delete-windows-dir", r"\b(del|erase|rd|rmdir)\b[^\n]*[a-z]:\\windows(\\|\s|$)"),
    ("registry-hive-delete", r"\breg\s+delete\s+\"?hk(lm|cr|ey_local_machine|ey_classes_root)\b"),
    ("shutdown", r"\b(shutdown|logoff)\b(?!\s+/\?)"),
    ("bcdedit", r"\bbcdedit\b[^\n]*\s/(delete|deletevalue|set)\b"),
    ("vss-delete", r"\bvssadmin\s+delete\s+shadows\b"),
    ("rm-root", r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+(--no-preserve-root\s+)?/(\s|$)"),
    ("mkfs", r"\bmkfs(\.[a-z0-9]+)?\b"),
    ("dd-to-device", r"\bdd\b[^\n]*\bof=/dev/"),
    ("fork-bomb", r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"),
]


@dataclass
class Decision:
    """Wynik oceny polecenia przez politykę."""

    allowed: bool
    rule: Optional[str] = None
    reason: Optional[str] = None


class Policy:
    """Sprawdza polecenia względem listy wzorców zabronionych."""

    def __init__(
        self,
        *,
        allow_dangerous: bool = False,
        extra_rules: Optional[Iterable[Tuple[str, str]]] = None,
        disabled_rules: Optional[Iterable[str]] = None,
        max_command_length: int = 32_000,
    ) -> None:
        self.allow_dangerous = allow_dangerous
        self.max_command_length = max_command_length
        disabled = {r.strip().lower() for r in (disabled_rules or []) if r.strip()}
        rules = list(DEFAULT_RULES) + list(extra_rules or [])
        self._rules: List[Tuple[str, Pattern[str]]] = [
            (name, re.compile(pattern, re.IGNORECASE))
            for name, pattern in rules
            if name.lower() not in disabled
        ]

    @property
    def rule_names(self) -> List[str]:
        return [name for name, _ in self._rules]

    def check(self, command: str) -> Decision:
        if not command or not command.strip():
            return Decision(False, "empty", "Polecenie jest puste")
        if len(command) > self.max_command_length:
            return Decision(
                False,
                "too-long",
                f"Polecenie dłuższe niż {self.max_command_length} znaków",
            )
        if self.allow_dangerous:
            return Decision(True)
        for name, pattern in self._rules:
            if pattern.search(command):
                return Decision(
                    False,
                    name,
                    f"Polecenie zablokowane przez regułę {name!r}. "
                    f"Uruchom most z --allow-dangerous, jeśli to zamierzone.",
                )
        return Decision(True)


def parse_rule_file(text: str) -> List[Tuple[str, str]]:
    """Parsuje dodatkowe reguły z pliku (linie ``nazwa = wyrażenie``)."""
    rules: List[Tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, pattern = line.partition("=")
        if not sep:
            rules.append((f"custom-{len(rules) + 1}", line))
        else:
            rules.append((name.strip() or f"custom-{len(rules) + 1}", pattern.strip()))
    return rules
