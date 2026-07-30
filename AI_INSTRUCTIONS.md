# CMD Bridge — instrukcja dla AI

Masz dostęp do powłoki systemowej (CMD) przez lokalny most HTTP.
Nie wykonujesz poleceń sam — wysyłasz je do mostu i czytasz odpowiedź.

## Połączenie

- Adres: `http://127.0.0.1:8765` (podany przez użytkownika przy starcie mostu)
- Każde żądanie poza `GET /health`: nagłówek `X-Bridge-Token: <token>`
- Treść żądań i odpowiedzi: JSON, UTF-8

## Wykonanie polecenia

```http
POST /run
{"command": "dir", "session": "main", "view": "auto", "timeout": 60}
```

Odpowiedź:

```json
{"ok": true, "exit_code": 0, "output": "...", "cwd": "C:\\Projekt", "seq": 7,
 "view": "tail", "lines_total": 12, "duration": 0.08, "timed_out": false,
 "log_file": "...\\00007.log"}
```

Zasady:
- `exit_code == 0` oznacza sukces. `output` zawiera połączone stdout i stderr.
- Sesja jest **trwała**: `cd`, `set`, aktywacja venv działają w kolejnych poleceniach.
- `session` jest opcjonalne (domyślnie `main`); nieistniejąca sesja tworzy się sama.
- Pełne wyjście zawsze ląduje w pliku `log_file` — nie musisz go pobierać w całości.

## Najważniejsze: wybierz widok, nie ciągnij całego logu

Build Androida potrafi mieć 40 000 linii, z czego istotne są trzy. Pole `view`
mówi mostowi, co ma wyciąć z logu **po swojej stronie**:

| `view` | Co zwraca | Kiedy używać |
|---|---|---|
| `auto` | błędy przy porażce, ogon przy sukcesie | domyślny wybór dla buildów i długich poleceń |
| `errors` | rozpoznane błędy i ostrzeżenia z plikiem, linią i numerem linii logu | gdy coś się nie zbudowało |
| `summary` | jak `errors`, ale bez tekstu — sam werdykt i liczby | gdy chcesz tylko wiedzieć, czy przeszło |
| `tail` / `head` | ostatnie / pierwsze `lines` linii | krótkie podsumowania, początek instalacji |
| `grep` | linie pasujące do `pattern` (+ `context`) | szukanie konkretnej treści |
| `around` | okolice linii `line` (`context`) | kontekst wokół błędu z `errors` |
| `quiet` | nic — tylko `exit_code` i statystyki | gdy liczy się wyłącznie sukces/porażka |
| `full` | całość przycięta do `max_chars` | krótkie polecenia (`dir`, `git status`) |

Przykład — build, z którego dostajesz kilka linii zamiast megabajtów:

```json
{"command": "gradlew.bat assembleDebug --console=plain", "session": "build",
 "timeout": 1800, "view": "auto"}
```

Odpowiedź przy błędzie zawiera `findings`:

```json
{"exit_code": 1, "verdict": "failed", "status_line": "BUILD FAILED in 1m 12s",
 "counts": {"error": 3, "warning": 1}, "lines_total": 40014,
 "findings": [{"severity": "error", "file": "C:/Projekt/app/.../MainActivity.kt",
               "line": 42, "message": "Unresolved reference: bindig", "log_line": 20003}]}
```

`file` i `line` wskazują miejsce w **kodzie**, `log_line` — miejsce w **logu**.

## Drążenie logu bez powtarzania polecenia

```http
POST /logs
{"session": "build", "seq": 7, "view": "around", "line": 20003, "context": 5}
{"session": "build", "view": "grep", "pattern": "Unresolved reference"}
```

`seq` domyślnie wskazuje ostatnie polecenie sesji. To jest właściwy sposób na
zajrzenie głębiej — ponowne uruchamianie buildu tylko po to, by zobaczyć więcej
logu, kosztuje minuty i niczego nie wnosi.

## Nowe / zresetowane okno CMD

| Cel | Żądanie |
|---|---|
| Nowa sesja | `POST /sessions` `{"session": "build", "cwd": "C:\\Projekt"}` |
| Nowy CMD w istniejącej sesji (reset) | `POST /sessions/main/restart` |
| Lista sesji | `GET /sessions` |
| Zamknięcie sesji | `DELETE /sessions/build` |

Restartuj sesję, gdy: powłoka się zawiesiła, polecenie przekroczyło limit czasu
i nie dało się go przerwać, zmienne środowiskowe są zepsute albo chcesz czystego
startu.

## Polecenia interaktywne i długie

- Program pyta o dane → `POST /sessions/main/stdin` `{"data": "tak"}`.
- Przerwanie działającego programu → `POST /sessions/main/interrupt`.
- Podgląd w trakcie pracy → `GET /sessions/main/output?since=<offset>`; działa
  także wtedy, gdy sesja liczy build (przekazuj `offset` z poprzedniej odpowiedzi).
- Długie zadania: ustaw większy `timeout` (maks. 3600 s) albo uruchom je w tle
  (`start /b ... > build.log 2>&1`) i odpytuj plik.

## Kody błędów

| Kod | Znaczenie | Co zrobić |
|---|---|---|
| 400 | Złe żądanie (np. błędny `pattern`) | popraw JSON |
| 401 | Zły token | dopisz nagłówek `X-Bridge-Token` |
| 403 | Polecenie zablokowane przez politykę lub odrzucone przez użytkownika | nie omijaj blokady — poproś użytkownika |
| 404 | Nie ma takiej sesji lub logu | sprawdź `GET /sessions` |
| 409 | Sesja zajęta, martwa lub niespójna | `interrupt`, `restart` albo nowa sesja |

Pole `hint` w odpowiedzi zawsze mówi, co zrobić dalej.

## Tryb plikowy (gdy nie możesz wysyłać HTTP)

Zapisz żądanie jako plik `<mailbox>/inbox/<nazwa>.json`:

```json
{"op": "run", "command": "gradlew.bat assembleDebug", "session": "build", "view": "auto"}
```

Most odpowie plikiem o tej samej nazwie w `<mailbox>/outbox/`.
Dostępne `op`: `run`, `logs`, `new`, `restart`, `kill`, `sessions`, `stdin`,
`output`, `interrupt`, `health`.

## Zasady pracy

1. Jedno polecenie na żądanie; łącz kroki przez `&&` tylko gdy mają sens razem.
2. Sprawdzaj `exit_code` przed kolejnym krokiem — nie zakładaj sukcesu.
3. Przy długim wyjściu najpierw `view=errors`/`grep`, dopiero potem `around`.
   Czytanie całego logu marnuje kontekst i zwykle niczego nie wyjaśnia.
4. Nie próbuj obchodzić blokad bezpieczeństwa ani czytać cudzych sekretów.
5. Operacje nieodwracalne (kasowanie, formatowanie, instalacje systemowe)
   najpierw uzgodnij z użytkownikiem.
6. Ścieżki z Windows podawaj w JSON z podwójnym ukośnikiem: `"C:\\Projekt"`.
