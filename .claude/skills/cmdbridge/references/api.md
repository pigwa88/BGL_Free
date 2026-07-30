# CMD Bridge — pełne API

Wszystkie żądania poza `GET /health` wymagają nagłówka `X-Bridge-Token: <token>`
(alternatywnie `Authorization: Bearer <token>` albo parametr `?token=`).
Treść żądań i odpowiedzi: JSON w UTF-8. Most nasłuchuje tylko na 127.0.0.1.

## Spis treści

1. [Endpointy](#endpointy)
2. [Parametry widoków](#parametry-widoków)
3. [Pola odpowiedzi](#pola-odpowiedzi)
4. [Tryb plikowy (mailbox)](#tryb-plikowy-mailbox)
5. [Kody stanu](#kody-stanu)

## Endpointy

| Metoda i ścieżka | Ciało / parametry | Opis |
|---|---|---|
| `GET /health` | — | wersja, powłoka, sesje, reguły, dostępne widoki. Bez tokenu |
| `GET /instructions` | — | krótka instrukcja obsługi mostu (markdown) |
| `POST /run` | `command`, `session`, `timeout`, parametry widoku | wykonuje polecenie i zwraca wynik |
| `POST /logs` | `session`, `seq`, parametry widoku | widok logu wcześniejszego polecenia |
| `GET /sessions` | — | lista sesji z ich stanem |
| `POST /sessions` | `session`, `cwd`, `env`, `shell` | tworzy nową sesję |
| `POST /sessions/<id>/restart` | — | nowy proces powłoki dla sesji (zachowuje `cwd`) |
| `DELETE /sessions/<id>` | — | zamyka sesję wraz z procesami potomnymi |
| `POST /sessions/<id>/stdin` | `data`, `newline` | pisze na wejście powłoki |
| `POST /sessions/<id>/interrupt` | — | przerywa procesy potomne (powłoka przeżywa) |
| `GET /sessions/<id>/output?since=N` | `since` | strumień wyjścia od offsetu (działa w trakcie polecenia) |
| `POST /sessions/<id>` | jak `/run` | skrót: uruchamia polecenie w tej sesji |

### `POST /run`

```json
{"command": "gradlew.bat assembleDebug --console=plain",
 "session": "build", "timeout": 1800, "view": "auto"}
```

- `command` — wymagany tekst; może zawierać `&&`, potoki, przekierowania.
- `session` — domyślnie `main`; nieistniejąca sesja jest tworzona automatycznie.
- `timeout` — sekundy, domyślnie 60, przycinane do zakresu 1–3600.
  Po przekroczeniu most przerywa procesy potomne i zwraca `timed_out: true`.

### `POST /logs`

```json
{"session": "build", "seq": 7, "view": "grep", "pattern": "Caused by", "context": 3}
```

- `seq` — numer polecenia z odpowiedzi `/run`; pominięty lub `"last"` oznacza
  ostatnie polecenie tej sesji.
- Reszta pól jak w widokach poniżej. Log jest czytany strumieniowo, więc
  przeszukanie kilkusetmegabajtowego pliku jest tanie.

## Parametry widoków

Wspólne dla `/run` i `/logs`:

| Pole | Typ | Dotyczy | Znaczenie |
|---|---|---|---|
| `view` | tekst | wszystkie | `auto`, `full`, `tail`, `head`, `grep`, `around`, `errors`, `summary`, `quiet` |
| `lines` | liczba | tail, head, grep | ile linii zwrócić (domyślnie 100, maks. 5000) |
| `pattern` | tekst | grep | wyrażenie regularne (składnia Pythona) |
| `context` | liczba | grep, around | ile linii otoczenia dołączyć |
| `case_sensitive` | bool | grep | domyślnie `false` |
| `invert` | bool | grep | zwróć linie **nie**pasujące |
| `line` | liczba | around | numer linii w logu (pole `log_line` ze znalezisk) |
| `max_findings` | liczba | errors, summary | ile znalezisk pokazać (domyślnie 20) |
| `profile` | tekst | errors, summary | ogranicz reguły: `gradle`, `kotlin`, `compiler`, `android`, `python`, `node`, `msbuild`; domyślnie `auto` (wszystkie) |
| `max_chars` | liczba | wszystkie prócz errors/summary | limit znaków tekstu (domyślnie 20000) |
| `raw` | bool | wszystkie | nie czyść kodów ANSI i pasków postępu |

`view: "auto"` rozstrzyga się po stronie mostu: `errors` gdy `exit_code != 0`
lub polecenie przekroczyło czas, w przeciwnym razie `tail` z 20 liniami.
W odpowiedzi pojawia się wtedy `view_auto: true` i faktycznie użyty `view`.

## Pola odpowiedzi

### Wspólne dla `/run`

| Pole | Znaczenie |
|---|---|
| `ok` | `true` przy powodzeniu żądania (nie mylić z sukcesem polecenia) |
| `exit_code` | kod wyjścia polecenia; `0` = sukces, `null` = nieznany (timeout) |
| `output` | tekst wyjścia **po zastosowaniu widoku** |
| `cwd` | katalog roboczy po wykonaniu polecenia |
| `seq` | numer polecenia w sesji — używaj go w `/logs` |
| `log_file` | ścieżka pełnego logu na dysku |
| `duration` | czas w sekundach |
| `timed_out` / `recovered` | czy przekroczono limit czasu i czy sesja się pozbierała |
| `lines_total` / `lines_shown` | ile linii miał log i ile zwrócono |
| `truncated` | czy widok coś pominął |
| `hint` | podpowiedź, co zrobić dalej |

### Dodatkowe dla `view: "grep"`

`matches` — liczba wszystkich dopasowań (także tych ponad limit `lines`),
`numbered: true` — linie w `output` są poprzedzone numerem.

### Dodatkowe dla `view: "errors"` / `"summary"`

| Pole | Znaczenie |
|---|---|
| `verdict` | `ok`, `failed` albo `unknown` (werdykt odczytany z logu) |
| `status_line` | np. `BUILD FAILED in 1m 12s`, o ile log ją zawiera |
| `counts` | liczba dopasowanych linii wg wagi: `{"error": 3, "warning": 12}` |
| `findings` | lista znalezisk (poniżej) |
| `findings_total` | liczba **różnych** znalezisk w całym logu |
| `profiles` | które zestawy reguł zadziałały, np. `["gradle", "kotlin"]` |
| `artifacts` | wykryte ścieżki `.apk`, `.aab`, `.jar`, `.exe`… |
| `used_generic_rules` | `true` = format nierozpoznany, pokazano linie „wyglądające na błąd" |

Pojedyncze znalezisko:

```json
{"severity": "error", "message": "Unresolved reference: bindig",
 "file": "C:/Projekt/app/src/main/java/MainActivity.kt", "line": 42,
 "log_line": 20003, "rule": "kotlin-error", "count": 1,
 "detail": ["Execution failed for task ':app:compileDebugKotlin'."]}
```

- `file` + `line` — miejsce w kodzie źródłowym.
- `log_line` — miejsce w logu; podaj je jako `line` w `view: "around"`.
- `count` — ile razy ten sam błąd wystąpił (pojawia się przy powtórzeniach).
- `detail` — treść bloku (np. sekcji `* What went wrong:` albo tracebacku).

## Tryb plikowy (mailbox)

Gdy nie możesz wysyłać żądań HTTP: zapisz plik `<mailbox>/inbox/<nazwa>.json`
z tym samym ciałem co w HTTP plus polem `op`. Odpowiedź pojawi się jako
`<mailbox>/outbox/<nazwa>.json` (zapis atomowy — nigdy nie zobaczysz połowy pliku).

```json
{"op": "run", "command": "gradlew.bat assembleDebug", "session": "build", "view": "auto"}
```

Dostępne `op`: `run`, `logs`, `new`, `restart`, `kill`, `sessions`, `stdin`,
`output`, `interrupt`, `health`. Pominięte `op` przy obecnym `command` znaczy
`run`. Odpowiedź zawiera dodatkowo `status` (kod HTTP), `request_file`
i `completed_at`. Przetworzone żądania trafiają do `<mailbox>/done/`.

## Kody stanu

| Kod | Znaczenie | Reakcja |
|---|---|---|
| 200 | wykonano | sprawdź `exit_code` — to on mówi o sukcesie polecenia |
| 201 | utworzono sesję | — |
| 400 | złe żądanie: brak `command`, wadliwy `pattern`, nieznany `view` | popraw ciało żądania |
| 401 | brak lub zły token | dopisz `X-Bridge-Token` |
| 403 | polityka bezpieczeństwa zablokowała polecenie albo użytkownik je odrzucił | nie obchodź blokady, wyjaśnij użytkownikowi potrzebę |
| 404 | nie ma sesji, ścieżki lub logu | `GET /sessions`, sprawdź `seq` |
| 409 | sesja zajęta, martwa lub niespójna po timeoucie | `interrupt`, `restart` albo nowa sesja |
| 500 | błąd wewnętrzny mostu | powtórz; jeśli wraca — poproś użytkownika o zajrzenie do konsoli mostu |
