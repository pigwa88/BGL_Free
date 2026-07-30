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

## API HTTP (skrót)

| Metoda i ścieżka | Opis |
|---|---|
| `GET /health` | stan mostu, lista sesji (bez tokenu) |
| `GET /instructions` | instrukcja dla AI |
| `POST /run` | `{"command": "dir", "session": "main", "timeout": 60}` |
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
python -m cmdbridge.client run --session build "cd .. && dir"
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
  shell.py      definicje powłok (cmd.exe, PowerShell, sh/bash)
  procutil.py   drzewo procesów potomnych (przerywanie bez zabijania powłoki)
  policy.py     reguły bezpieczeństwa
  audit.py      dziennik JSONL
  client.py     klient CLI
AI_INSTRUCTIONS.md   instrukcja do wklejenia modelowi
tests/               testy (unittest, bez zależności)
```

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

63 testy: sesje, filtrowanie znaczników, zarządzanie sesjami, polityka bezpieczeństwa, API HTTP, tryb plikowy.
