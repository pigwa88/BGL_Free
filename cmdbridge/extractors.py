"""Ekstraktory: wyciągają z logu to, co naprawdę istotne — błędy i werdykt.

Model AI nie powinien czytać 50 000 linii logu Gradle po to, by znaleźć trzy
linijki błędu kompilacji. Ekstraktor przechodzi log **raz, strumieniowo**,
rozpoznaje typowe formaty komunikatów (Kotlin, javac, AAPT2, Gradle, Python,
npm/TypeScript, MSBuild) i zwraca listę znalezisk z numerami linii w logu —
po numerze można potem dociągnąć kontekst widokiem ``around``.

Reguły są celowo *dodatkowe*, a nie wykluczające się: log Androida potrafi
zawierać naraz błędy Kotlina, AAPT i Gradle. Reguła ogólna (``generic``) wchodzi
do gry dopiero wtedy, gdy nic konkretnego się nie znalazło — inaczej zalałaby
wynik szumem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern

MAX_FINDINGS = 20
TAIL_WHEN_EMPTY = 15


@dataclass(frozen=True)
class Rule:
    """Pojedynczy wzorzec rozpoznawania komunikatu w logu."""

    name: str
    profile: str
    severity: str
    pattern: Pattern
    block_until: Optional[Pattern] = None
    block_max: int = 20
    block_inclusive: bool = False


def _rx(pattern: str, flags: int = 0) -> Pattern:
    return re.compile(pattern, flags)


# Kolejność ma znaczenie: pierwsza pasująca reguła wygrywa dla danej linii.
RULES: List[Rule] = [
    # ---------------------------------------------------------------- Gradle
    Rule(
        "gradle-what-went-wrong", "gradle", "error",
        _rx(r"^\* What went wrong:"),
        block_until=_rx(r"^\* (Try|Get more help|Exception is:)"),
        block_max=25,
    ),
    Rule("gradle-task-failed", "gradle", "error", _rx(r"^> Task :(?P<task>\S+) FAILED")),
    Rule("gradle-failure", "gradle", "error", _rx(r"^FAILURE: (?P<msg>.+)$")),
    Rule(
        "gradle-configuration", "gradle", "error",
        _rx(r"^A problem (occurred|was found) (configuring|evaluating).*$"),
        block_until=_rx(r"^\s*$"), block_max=12,
    ),
    Rule("gradle-caused-by", "gradle", "error", _rx(r"^\s*Caused by: (?P<msg>.+)$")),

    # ---------------------------------------------------- Kotlin (nowy format)
    Rule(
        "kotlin-error", "kotlin", "error",
        _rx(r"^e:\s+(?:file://)?(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)[:\s]\s*(?P<msg>.+)$"),
    ),
    Rule(
        "kotlin-error-legacy", "kotlin", "error",
        _rx(r"^e:\s+(?P<file>.+?):\s*\((?P<line>\d+),\s*(?P<col>\d+)\):\s*(?P<msg>.+)$"),
    ),
    Rule(
        "kotlin-warning", "kotlin", "warning",
        _rx(r"^w:\s+(?:file://)?(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)[:\s]\s*(?P<msg>.+)$"),
    ),
    Rule("kotlin-error-plain", "kotlin", "error", _rx(r"^e:\s+(?P<msg>.+)$")),

    # ------------------------------------------- javac / AAPT2 / clang / ndk
    Rule(
        "compiler-error", "compiler", "error",
        _rx(r"^(?P<file>\S+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:fatal\s+)?error:\s*(?P<msg>.+)$"),
    ),
    Rule(
        "compiler-warning", "compiler", "warning",
        _rx(r"^(?P<file>\S+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*warning:\s*(?P<msg>.+)$"),
    ),
    Rule(
        "aapt-error", "android", "error",
        _rx(r"AAPT:?\s*error:\s*(?P<msg>.+)$", re.IGNORECASE),
    ),
    Rule("android-duplicate-class", "android", "error", _rx(r"^\s*Duplicate class (?P<msg>.+)$")),
    Rule(
        "android-manifest-merger", "android", "error",
        _rx(r"^Manifest merger failed\s*:?\s*(?P<msg>.*)$"),
    ),
    Rule("android-dex", "android", "error", _rx(r"^\s*(?P<msg>Cannot fit requested classes.+)$")),
    Rule("adb-install-failed", "android", "error", _rx(r"^adb: (?P<msg>failed to install.+)$")),
    Rule("jvm-oom", "jvm", "error", _rx(r"(?P<msg>java\.lang\.OutOfMemoryError.*)$")),

    # -------------------------------------------------------------- Python
    Rule(
        "python-traceback", "python", "error",
        _rx(r"^\s*Traceback \(most recent call last\):"),
        block_until=_rx(r"^\s*\w+(\.\w+)*(Error|Exception|Interrupt)\b.*$"),
        block_max=30, block_inclusive=True,
    ),

    # ------------------------------------------------------ node / TypeScript
    Rule("npm-error", "node", "error", _rx(r"^npm ERR!\s*(?P<msg>.+)$")),
    Rule("webpack-error", "node", "error", _rx(r"^ERROR in (?P<msg>.+)$")),
    Rule("ts-error", "node", "error", _rx(r"(?P<msg>error TS\d+:.+)$")),
    Rule("module-not-found", "node", "error", _rx(r"^\s*(?P<msg>Module not found:.+)$")),

    # ------------------------------------------------------------- .NET/MSBuild
    Rule("msbuild-error", "msbuild", "error", _rx(r"(?P<msg>error (?:CS|MSB|NETSDK)\d+:.+)$")),

    # ---------------------------------------------------------------- ogólne
    Rule("generic-error", "generic", "error",
         _rx(r"\b(?:error|fatal|failed|exception|not found|cannot find|no such file)\b",
             re.IGNORECASE)),
    Rule("generic-warning", "generic", "warning", _rx(r"\bwarning\b", re.IGNORECASE)),
]

STATUS_RE = _rx(r"^(?P<msg>BUILD (?P<result>SUCCESSFUL|FAILED)(?: in .+)?)$")
GRADLE_SUMMARY_RE = _rx(r"^\d+ actionable tasks?:")
ARTIFACT_RE = _rx(r"(?P<path>[^\s'\"()]+\.(?:apk|aab|aar|jar|ipa|exe|whl))\b", re.IGNORECASE)


@dataclass
class Finding:
    """Pojedyncze znalezisko w logu."""

    severity: str
    message: str
    rule: str
    profile: str
    log_line: int
    file: Optional[str] = None
    line: Optional[int] = None
    detail: List[str] = field(default_factory=list)
    count: int = 1

    def key(self) -> tuple:
        return (self.severity, self.file, self.line, self.message[:160])

    def to_dict(self) -> dict:
        data: Dict[str, Any] = {
            "severity": self.severity,
            "message": self.message,
            "log_line": self.log_line,
            "rule": self.rule,
        }
        if self.file:
            data["file"] = self.file
        if self.line:
            data["line"] = self.line
        if self.detail:
            data["detail"] = self.detail
        if self.count > 1:
            data["count"] = self.count
        return data

    def render(self) -> str:
        where = ""
        if self.file:
            where = f" {self.file}" + (f":{self.line}" if self.line else "")
        repeat = f" (x{self.count})" if self.count > 1 else ""
        head = f"[{self.severity}]{where} {self.message}{repeat}  (log:{self.log_line})"
        if self.detail:
            return head + "\n" + "\n".join(f"    {line}" for line in self.detail)
        return head


class FindingBucket:
    """Zbiera znaleziska: deduplikuje, ogranicza pamięć, liczy pominięte.

    Log potrafi powtórzyć ten sam błąd tysiące razy (np. w każdym module), więc
    trzymamy jeden wpis z licznikiem. Zapamiętujemy też, ile *różnych* błędów
    było w całym logu - inaczej model dostałby mylące „3 błędy", gdy było ich 300.
    """

    KEY_LIMIT = 10_000

    def __init__(self, max_findings: int) -> None:
        self.max_findings = max_findings
        self._items: Dict[tuple, Finding] = {}
        self._keys: set = set()
        self._distinct = 0

    def add(self, finding: Finding) -> None:
        key = finding.key()
        existing = self._items.get(key)
        if existing is not None:
            existing.count += 1
            return
        if key not in self._keys:
            self._distinct += 1
            if len(self._keys) < self.KEY_LIMIT:
                self._keys.add(key)
        if len(self._items) < max(200, self.max_findings * 4):
            self._items[key] = finding

    def values(self) -> List[Finding]:
        return list(self._items.values())

    @property
    def distinct(self) -> int:
        return self._distinct

    def __bool__(self) -> bool:
        return bool(self._items)


DRIVE_PREFIX_RE = _rx(r"^/([A-Za-z]:)")


def normalize_path(path: Optional[str]) -> Optional[str]:
    """Sprowadza ścieżkę z komunikatu do postaci, którą da się otworzyć.

    Kotlin raportuje ``file:///C:/Projekt/A.kt``; po zdjęciu schematu zostaje
    ``/C:/Projekt/A.kt``, czego nie otworzy żaden edytor na Windows.
    """
    if not path:
        return None
    path = path.strip().strip('"').strip("'")
    if path.startswith("file://"):
        path = path[7:]
    path = DRIVE_PREFIX_RE.sub(r"\1", path)
    return path or None


def _select_rules(profile: str) -> List[Rule]:
    profile = (profile or "auto").lower()
    if profile in ("auto", "all", ""):
        return RULES
    return [rule for rule in RULES if rule.profile == profile] or RULES


def extract(
    source: Dict[str, Any],
    spec: Optional[Dict[str, Any]] = None,
    *,
    raw: bool = False,
    with_text: bool = True,
) -> Dict[str, Any]:
    """Przechodzi log raz i zwraca strukturalny wyciąg błędów oraz werdykt."""
    from .outputview import iter_lines  # import lokalny: unikamy cyklu

    spec = spec or {}
    max_findings = int(spec.get("max_findings") or MAX_FINDINGS)
    rules = _select_rules(str(spec.get("profile") or "auto"))
    specific = [rule for rule in rules if rule.profile != "generic"]
    generic = [rule for rule in rules if rule.profile == "generic"]

    findings = FindingBucket(max_findings)
    fallback = FindingBucket(max_findings)
    counts: Dict[str, int] = {"error": 0, "warning": 0}
    profiles: List[str] = []
    artifacts: List[str] = []
    tail: List[str] = []
    status: Optional[str] = None
    status_line: Optional[str] = None
    total = 0

    block: Optional[Finding] = None
    block_rule: Optional[Rule] = None

    def store(bucket: "FindingBucket", finding: Finding) -> None:
        bucket.add(finding)

    for number, line in iter_lines(source, raw=raw):
        total += 1
        if len(tail) >= TAIL_WHEN_EMPTY:
            tail.pop(0)
        tail.append(line)

        if block is not None and block_rule is not None:
            stop = block_rule.block_until and block_rule.block_until.search(line)
            if stop and block_rule.block_inclusive:
                block.detail.append(line.strip())
            if stop or len(block.detail) >= block_rule.block_max:
                store(findings, block)
                block, block_rule = None, None
                if stop:
                    continue
            else:
                if line.strip():
                    block.detail.append(line.strip())
                continue

        match = STATUS_RE.match(line)
        if match:
            status_line = match.group("msg")
            status = "failed" if match.group("result") == "FAILED" else "ok"
            if "gradle" not in profiles:
                profiles.append("gradle")
            continue

        if len(artifacts) < 5:
            artifact = ARTIFACT_RE.search(line)
            if artifact and artifact.group("path") not in artifacts:
                artifacts.append(artifact.group("path"))

        matched = False
        for rule in specific:
            found = rule.pattern.search(line)
            if not found:
                continue
            groups = found.groupdict()
            message = (groups.get("msg") or groups.get("task") or line).strip()
            if rule.name == "gradle-task-failed":
                message = f"Task :{groups.get('task')} FAILED"
            finding = Finding(
                severity=rule.severity,
                message=message[:500],
                rule=rule.name,
                profile=rule.profile,
                log_line=number,
                file=normalize_path(groups.get("file")),
                line=int(groups["line"]) if groups.get("line", "").isdigit() else None,
            )
            counts[rule.severity] = counts.get(rule.severity, 0) + 1
            if rule.profile not in profiles:
                profiles.append(rule.profile)
            if rule.block_until is not None:
                block, block_rule = finding, rule
            else:
                store(findings, finding)
            matched = True
            break

        if matched or GRADLE_SUMMARY_RE.match(line):
            continue

        for rule in generic:
            if rule.pattern.search(line):
                counts[rule.severity] = counts.get(rule.severity, 0) + 1
                store(
                    fallback,
                    Finding(rule.severity, line.strip()[:500], rule.name, rule.profile, number),
                )
                break

    if block is not None:  # log urwał się w środku bloku
        store(findings, block)

    selected = findings.values()
    distinct = findings.distinct
    used_fallback = False
    if not selected:
        selected = [f for f in fallback.values() if f.severity == "error"] or fallback.values()
        distinct = fallback.distinct
        used_fallback = bool(selected)

    selected.sort(key=lambda f: (f.severity != "error", f.log_line))
    shown = selected[:max_findings]
    hidden = max(0, distinct - len(shown))

    if status is None and any(f.severity == "error" for f in shown):
        status = "failed"
    elif status is None:
        status = "unknown"

    result: Dict[str, Any] = {
        # 'verdict', a nie 'status' - żeby nie mylić się z kodem stanu odpowiedzi
        "verdict": status,
        "status_line": status_line,
        "findings": [f.to_dict() for f in shown],
        "findings_total": distinct,
        "counts": {k: v for k, v in counts.items() if v},
        "profiles": profiles,
        "lines_total": total,
        "lines_shown": len(shown),
        "truncated": hidden > 0,
        "used_generic_rules": used_fallback,
    }
    if artifacts:
        result["artifacts"] = artifacts

    if with_text:
        parts: List[str] = []
        if status_line:
            parts.append(status_line)
        parts.extend(finding.render() for finding in shown)
        if hidden:
            parts.append(f"...[jeszcze {hidden} znalezisk; "
                         f"zwiększ max_findings albo użyj view=grep]...")
        if not shown:
            parts.append("Nie rozpoznano komunikatów o błędach. Ostatnie linie logu:")
            parts.extend(tail)
        result["text"] = "\n".join(parts)
    else:
        result["text"] = ""
    return result
