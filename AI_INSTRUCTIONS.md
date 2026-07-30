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
{"command": "dir", "session": "main", "timeout": 60}
```

Odpowiedź:

```json
{"ok": true, "exit_code": 0, "output": "...pełne wyjście...", "cwd": "C:\\Projekt",
 "duration": 0.08, "truncated": false, "timed_out": false, "log_file": "...\\00001.log"}
```

Zasady:
- `exit_code == 0` oznacza sukces. `output` zawiera połączone stdout i stderr.
- Sesja jest **trwała**: `cd`, `set`, aktywacja venv działają w kolejnych poleceniach.
- `session` jest opcjonalne (domyślnie `main`). Podaj inną nazwę, aby pracować
  równolegle w kilku oknach — nieistniejąca sesja tworzy się automatycznie.
- Gdy `truncated == true`, pełne wyjście jest w pliku `log_file` (możesz o nie
  poprosić użytkownika lub odczytać, jeśli masz dostęp do plików).

## Nowe / zresetowane okno CMD

| Cel | Żądanie |
|---|---|
| Nowa sesja | `POST /sessions` `{"session": "build", "cwd": "C:\\Projekt"}` |
| Nowy CMD w istniejącej sesji (reset) | `POST /sessions/main/restart` |
| Lista sesji | `GET /sessions` |
| Zamknięcie sesji | `DELETE /sessions/build` |

Restartuj sesję, gdy: powłoka zawiesiła się, polecenie przekroczyło limit czasu
i nie dało się go przerwać, zmienne środowiskowe są zepsute albo chcesz czystego
startu.

## Polecenia interaktywne i długie

- Program pyta o dane → `POST /sessions/main/stdin` `{"data": "tak"}`
  (znak nowej linii dodawany automatycznie).
- Przerwanie (Ctrl+Break) → `POST /sessions/main/interrupt`.
- Podgląd wyjścia w trakcie pracy → `GET /sessions/main/output?since=<offset>`;
  używaj `offset` z poprzedniej odpowiedzi, aby czytać tylko nowości.
- Długie zadania: ustaw większy `timeout` (maks. 3600 s) albo uruchom je w
  osobnej sesji i odpytuj `/output`.

## Kody błędów

| Kod | Znaczenie | Co zrobić |
|---|---|---|
| 400 | Złe żądanie | popraw JSON / pole `command` |
| 401 | Zły token | dopisz nagłówek `X-Bridge-Token` |
| 403 | Polecenie zablokowane przez politykę lub odrzucone przez użytkownika | nie omijaj blokady — poproś użytkownika |
| 409 | Sesja zajęta, martwa lub niespójna | `interrupt`, `restart` albo nowa sesja |

Pole `hint` w odpowiedzi zawsze mówi, co zrobić dalej.

## Tryb plikowy (gdy nie możesz wysyłać HTTP)

Zapisz żądanie jako plik `<mailbox>/inbox/<nazwa>.json`:

```json
{"op": "run", "command": "dir", "session": "main"}
```

Most odpowie plikiem o tej samej nazwie w `<mailbox>/outbox/`.
Dostępne `op`: `run`, `new`, `restart`, `kill`, `sessions`, `stdin`, `output`,
`interrupt`, `health`.

## Zasady pracy

1. Jedno polecenie na żądanie; łącz kroki przez `&&` tylko gdy mają sens razem.
2. Sprawdzaj `exit_code` przed kolejnym krokiem — nie zakładaj sukcesu.
3. Nie próbuj obchodzić blokad bezpieczeństwa ani czytać cudzych sekretów.
4. Operacje nieodwracalne (kasowanie, formatowanie, instalacje systemowe)
   najpierw uzgodnij z użytkownikiem.
5. Ścieżki z Windows podawaj w JSON z podwójnym ukośnikiem: `"C:\\Projekt"`.
