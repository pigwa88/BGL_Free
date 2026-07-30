# CMD Bridge — pośrednik między CMD a AI

Aplikacja na PC (Python, wyłącznie biblioteka standardowa), która daje modelowi AI
kontrolowany dostęp do wiersza poleceń. AI nie uruchamia niczego samo — wysyła
polecenie do mostu, most wykonuje je w **trwałej sesji CMD** i zwraca pełne wyjście.

```
   AI  ──►  HTTP / pliki JSON  ──►  CMD Bridge  ──►  cmd.exe (sesja trwała)
       ◄──  wyjście + kod wyjścia ◄──            ◄──
```

## Co potrafi

- **Trwałe sesje** — `cd`, `set`, aktywacja venv obowiązują w kolejnych poleceniach.
- **Wiele równoległych sesji CMD** — np. `main` do pracy, `serwer` do procesu w tle.
- **Nowy CMD na żądanie** — `POST /sessions` (nowa sesja) lub `POST /sessions/<id>/restart`
  (świeża powłoka, gdy poprzednia się zawiesiła).
- **Prawdziwe kody wyjścia** (`%ERRORLEVEL%`) i scalone stdout + stderr w kolejności.
- **Filtrowanie wyjścia po stronie mostu** — zamiast całego logu AI dostaje same
  błędy, wynik `grep` albo ogon logu. Z 40 000 linii buildu Gradle robi się
  kilkanaście linii konkretu (patrz [Widoki wyjścia](#widoki-wyjścia)).
- **Stała pamięć** — pełny log strumieniowany jest na dysk w trakcie wykonania,
  w pamięci zostaje tylko ruchome okno; build sypiący setkami megabajtów nie
  rozsadza procesu.
- **Programy interaktywne** — pisanie na stdin, przerywanie (`interrupt`), podgląd
  wyjścia w trakcie działania (`/output?since=`).
- **Dwa kanały komunikacji**: lokalne API HTTP oraz katalog plików (`mailbox`) dla
  modeli, które potrafią tylko czytać i zapisywać pliki.
- **Bezpiecznik** — blokada poleceń nieodwracalnie niszczących system, opcjonalne
  ręczne zatwierdzanie każdego polecenia, token dostępu, nasłuch tylko na 127.0.0.1.
- **Audyt** — `audit.jsonl` z każdym poleceniem oraz pełne logi wyjścia na dysku.

## Wymagania

Python 3.8+ (Windows, Linux lub macOS). Zero zależności zewnętrznych.
Na Windows domyślną powłoką jest `cmd.exe`, na pozostałych systemach `bash`/`sh`
— dzięki temu most działa i daje się testować także poza Windows.

## Start

```bat
python -m cmdbridge
```

Po starcie konsola wypisze adres, token i gotową linijkę do wklejenia w czat z AI:

```
 API HTTP     : http://127.0.0.1:8765
 Token        : 9dQ...
 Instrukcja AI: C:\Users\ty\.cmdbridge\AI_INSTRUCTIONS.md
```

Najczęstsze warianty uruchomienia:

```bat
python -m cmdbridge --cwd C:\Projekt          :: katalog startowy sesji
python -m cmdbridge --confirm                 :: każde polecenie zatwierdzasz ręcznie
python -m cmdbridge --mailbox                 :: dodatkowo tryb plikowy
python -m cmdbridge --no-server --mailbox D:\wymiana   :: wyłącznie pliki
python -m cmdbridge --shell powershell        :: PowerShell zamiast cmd.exe (eksperymentalnie)
python -m cmdbridge --print-instructions      :: wypisz instrukcję dla AI i wyjdź
```

Na Windows wygodny jest `run_bridge.bat` (podwójne kliknięcie).

## Jak dać dostęp modelowi AI

1. Uruchom most.
2. Wklej modelowi treść `AI_INSTRUCTIONS.md` (albo `python -m cmdbridge --print-instructions`)
   razem z adresem i tokenem. Model pobierze też instrukcję sam: `GET /instructions`.
3. Model wysyła `POST /run` i czyta odpowiedź.

Jeśli model nie może wykonywać żądań HTTP, uruchom most z `--mailbox` i wskaż mu
katalog: żądanie to plik JSON w `inbox/`, odpowiedź pojawia się pod tą samą nazwą
w `outbox/`.

## Widoki wyjścia

Pole `view` w żądaniu decyduje, co most wytnie z logu **po swojej stronie** —
zanim cokolwiek trafi do modelu:

| `view` | Zwraca | Kiedy |
|---|---|---|
| `auto` | błędy przy porażce, ogon przy sukcesie | domyślny wybór dla buildów |
| `errors` | rozpoznane błędy z plikiem, linią i numerem linii w logu | `exit_code != 0` |
| `summary` | sam werdykt i liczniki, bez tekstu | „przeszło czy nie" |
| `grep` | linie pasujące do `pattern` (+ `context`) | szukanie konkretu |
| `around` | okolice wskazanej linii logu | kontekst wokół błędu |
| `tail` / `head` | ostatnie / pierwsze `lines` linii | podsumowania |
| `quiet` | nic prócz statystyk | liczy się tylko kod wyjścia |
| `full` | całość przyciętą do `max_chars` | krótkie polecenia |

Ekstraktory rozpoznają komunikaty Gradle, Kotlina, javac, AAPT2, Androida
(manifest merger, duplicate class, dex), Pythona, npm/TypeScript i MSBuild.
Przykład — z buildu, który wypluł 40 014 linii (1,4 MB na dysku), model dostaje
1,7 KB:

```
BUILD FAILED in 1m 12s
[error] Task :app:compileDebugKotlin FAILED  (log:20001)
[error] C:/Projekt/app/src/main/java/MainActivity.kt:42 Unresolved reference: bindig  (log:20003)
[error] * What went wrong:  (log:40007)
    Execution failed for task ':app:compileDebugKotlin'.
```

Log zostaje na dysku, więc można go drążyć bez powtarzania polecenia:

```json
POST /logs
{"session": "build", "view": "around", "line": 20003, "context": 5}
{"session": "build", "view": "grep", "pattern": "Caused by", "context": 3}
```

## API HTTP (skrót)

| Metoda i ścieżka | Opis |
|---|---|
| `GET /health` | stan mostu, lista sesji (bez tokenu) |
| `GET /instructions` | instrukcja dla AI |
| `POST /run` | `{"command": "dir", "session": "main", "timeout": 60, "view": "auto"}` |
| `POST /logs` | widok logu wcześniejszego polecenia: `{"session": "build", "view": "grep", "pattern": "..."}` |
| `GET /sessions` | lista sesji |
| `POST /sessions` | nowa sesja: `{"session": "build", "cwd": "C:\\Projekt"}` |
| `POST /sessions/<id>/restart` | nowy proces CMD dla sesji |
| `DELETE /sessions/<id>` | zamknięcie sesji |
| `POST /sessions/<id>/stdin` | `{"data": "tak"}` — odpowiedź na pytanie programu |
| `POST /sessions/<id>/interrupt` | przerwanie działającego programu |
| `GET /sessions/<id>/output?since=N` | wyjście od offsetu N (podgląd na żywo) |

Autoryzacja: nagłówek `X-Bridge-Token: <token>` (albo `Authorization: Bearer <token>`).

Przykładowa odpowiedź `POST /run`:

```json
{
  "ok": true, "session": "main", "seq": 7, "command": "dir",
  "output": "...", "exit_code": 0, "cwd": "C:\\Projekt",
  "duration": 0.08, "timed_out": false, "truncated": false,
  "log_file": "C:\\Users\\ty\\.cmdbridge\\logs\\main\\00007.log"
}
```

## Klient do testów

```bat
python -m cmdbridge.client status
python -m cmdbridge.client run "dir"
python -m cmdbridge.client run --session build --view auto "gradlew.bat assembleDebug"
python -m cmdbridge.client logs --session build --view grep --pattern "Caused by"
python -m cmdbridge.client restart --session main
```

Klient sam wczytuje adres i token z `~/.cmdbridge/bridge.json`.

## Bezpieczeństwo

Most z założenia daje AI realny dostęp do komputera — traktuj go jak zdalny pulpit,
nie jak piaskownicę. Wbudowane zabezpieczenia:

- nasłuch **wyłącznie na 127.0.0.1** i losowy token przy każdym starcie;
- lista poleceń blokowanych (`format`, `diskpart`, `rm -rf /`, `mkfs`, `shutdown`,
  kasowanie `C:\` i gałęzi HKLM…), zwracanych z kodem 403 — patrz `cmdbridge/policy.py`;
- `--confirm` — każde polecenie wymaga potwierdzenia w konsoli mostu;
- `--allow-dangerous` / `--disable-rule NAZWA` / `--rules-file` — świadome poluzowanie
  lub zaostrzenie reguł;
- pełny audyt w `~/.cmdbridge/audit.jsonl`.

Zalecenia: uruchamiaj most na koncie bez uprawnień administratora, ustaw `--cwd`
na katalog projektu, a przy pierwszych eksperymentach używaj `--confirm`.
Token nie chroni przed innym programem działającym na tym samym koncie — plik
`bridge.json` ma prawa tylko dla właściciela, ale most jest tak bezpieczny,
jak samo konto użytkownika.

## Struktura projektu

```
cmdbridge/
  app.py        uruchamianie, argumenty wiersza poleceń, plik połączenia
  core.py       logika operacji wspólna dla HTTP i trybu plikowego
  server.py     API HTTP (http.server)
  mailbox.py    tryb wymiany plików JSON
  session.py    trwałe sesje powłoki, protokół znacznika, timeouty
  outputview.py silnik widoków (full/tail/grep/around/errors...) - czyta strumieniowo
  extractors.py rozpoznawanie błędów: Gradle, Kotlin, javac, AAPT2, Python, npm, MSBuild
  shell.py      definicje powłok (cmd.exe, PowerShell, sh/bash)
  procutil.py   drzewo procesów potomnych (przerywanie bez zabijania powłoki)
  policy.py     reguły bezpieczeństwa
  audit.py      dziennik JSONL
  client.py     klient CLI
AI_INSTRUCTIONS.md   instrukcja do wklejenia modelowi
.claude/skills/cmdbridge/   skill dla Claude Code (obsługa mostu + build Androida)
tests/               testy (unittest, bez zależności)
```

### Skill dla Claude

W `.claude/skills/cmdbridge/` leży skill uczący Claude'a obsługi mostu:
`SKILL.md` (zasady doboru widoku i pętla pracy), `references/android.md`
(przepływ budowania APK i katalog typowych błędów Gradle/Kotlin/AAPT wraz
z naprawami) oraz `references/api.md` (pełne API). Claude Code w tym repozytorium
załaduje go automatycznie; w innych środowiskach można go skopiować do
`~/.claude/skills/`.

### Jak to działa w środku

Po każdym poleceniu most dopisuje własną linię drukującą unikalny znacznik wraz z
`%ERRORLEVEL%` i `%CD%`. Wszystko, co powłoka wypisze przed tą linią, jest wyjściem
polecenia — stąd znany kod wyjścia i aktualny katalog bez psucia sesji. Gdy polecenie
przekroczy limit czasu, most przerywa **tylko procesy potomne** powłoki (sama powłoka
przeżywa, więc sesja zwykle wraca do użytku); jeśli to nie pomoże, sesja jest oznaczana
jako niespójna i wymaga `restart`.

## Testy

```bat
python -m unittest discover -s tests -t .
```

113 testów: sesje, filtrowanie znaczników, zarządzanie sesjami, polityka
bezpieczeństwa, silnik widoków, ekstraktory błędów, API HTTP i tryb plikowy.
