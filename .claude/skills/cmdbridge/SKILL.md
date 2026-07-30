---
name: cmdbridge
description: Praca przez CMD Bridge - most, przez który wykonujesz polecenia w CMD/PowerShell/bash na komputerze użytkownika i czytasz wyniki bez wciągania całych logów do kontekstu. Użyj zawsze, gdy pojawia się CMD Bridge, plik ~/.cmdbridge/bridge.json, katalog mailbox z inbox/outbox, publiczny link do relaya z tokenem dostępu (zdalny dostęp do CMD przez /api/run), albo gdy użytkownik prosi o uruchomienie poleceń na swoim PC, kompilację projektu, build APK/aplikacji na Androida (gradlew, sdkmanager, adb, flutter), diagnozę błędu kompilacji lub analizę wielkiego logu buildu. Zawiera zasady doboru widoku (auto/errors/grep/around) zamiast czytania całego logu, przepływ pracy dla Androida oraz zdalny dostęp przez relay.
---

# CMD Bridge

Most daje ci wiersz poleceń na komputerze użytkownika: wysyłasz polecenie, most
wykonuje je w **trwałej sesji** powłoki i odsyła wynik. Trwałej — czyli `cd`,
`set`, aktywacja venv czy `nvm use` obowiązują w kolejnych poleceniach, tak jak
w prawdziwym oknie CMD.

Najważniejsza rzecz do zrozumienia: **most potrafi filtrować wyjście po swojej
stronie**. Pełny log zawsze trafia na dysk, a ty decydujesz, co z niego dostajesz.
Build Androida ma 40 000 linii, z czego istotne są trzy — pobieranie całości
zapycha kontekst i tak czy owak nie pomaga w diagnozie.

## Nawiązanie połączenia

Adres i token są w pliku `bridge.json` w katalogu stanu mostu
(`~/.cmdbridge/bridge.json`, na Windows `C:\Users\<user>\.cmdbridge\bridge.json`):

```json
{"url": "http://127.0.0.1:8765", "token": "...", "mailbox": "...", "shell": "cmd"}
```

Kanały, którymi możesz rozmawiać z mostem — użyj tego, który masz pod ręką:

1. **HTTP lokalnie** — `POST` z nagłówkiem `X-Bridge-Token: <token>`, adres
   `127.0.0.1`. Preferowany, gdy działasz na tym samym komputerze co most.
2. **Relay (zdalny link z tokenem)** — gdy działasz poza komputerem użytkownika
   (chmura, przeglądarka). Użytkownik daje ci publiczny adres i **token
   dostępu**; wołasz `POST /api/run` na relayu. Endpointy i pola są takie same
   jak lokalnie, tylko z prefiksem `/api/` i polem `op`. Szczegóły:
   `references/relay.md`. Najpierw sprawdź `GET /api/status` — `worker_online`
   mówi, czy PC jest podłączony.
3. **Katalog wymiany (mailbox)** — gdy potrafisz tylko czytać i zapisywać pliki:
   zapisz żądanie jako `<mailbox>/inbox/<nazwa>.json`, odpowiedź pojawi się pod
   tą samą nazwą w `<mailbox>/outbox/`. Format identyczny jak w HTTP, plus pole
   `"op"` (`run`, `logs`, `restart`, ...).
4. **Przez użytkownika** — jeśli nie masz żadnego z powyższych, podaj gotowy JSON
   i poproś o wklejenie odpowiedzi. Zanim to zrobisz, sprawdź `GET /health` —
   często okazuje się, że kanał jednak działa.

Gdy nie wiesz, czy most działa: `GET /health` (jedyne żądanie bez tokenu) zwraca
wersję, powłokę, listę sesji i dostępne widoki.

## Pętla pracy

```
1. Wyślij polecenie z sensownym `view` i `timeout`
2. Sprawdź `exit_code` (0 = sukces) — nigdy nie zakładaj, że przeszło
3. Porażka? Przeczytaj `findings` z widoku errors
4. Za mało kontekstu? Dociągnij fragment przez POST /logs (nie powtarzaj polecenia!)
5. Napraw i wróć do 1
```

### Uruchomienie polecenia

```json
POST /run
{"command": "gradlew.bat assembleDebug --console=plain",
 "session": "build", "timeout": 1800, "view": "auto"}
```

Pola: `command` (wymagane), `session` (domyślnie `main`, tworzona automatycznie),
`timeout` (sekundy, domyślnie 60, maks. 3600), `view` + parametry widoku.

W odpowiedzi liczą się: `exit_code`, `output` (już przefiltrowany), `cwd`, `seq`
(numer polecenia — przyda się do `/logs`), `lines_total`, `log_file`, `hint`.

## Dobór widoku — to jest sedno

| `view` | Zwraca | Kiedy |
|---|---|---|
| `auto` | błędy przy porażce, ostatnie 20 linii przy sukcesie | **domyślny wybór** dla buildów, instalacji, testów |
| `errors` | rozpoznane błędy/ostrzeżenia z plikiem, linią i numerem linii w logu | gdy `exit_code != 0` |
| `summary` | to samo bez tekstu — werdykt, liczniki, artefakty | gdy interesuje cię wyłącznie „przeszło czy nie" |
| `grep` | linie pasujące do `pattern`, z `context` linii otoczenia | szukasz konkretnej treści |
| `around` | okolice linii `line` | masz `log_line` z `errors` i chcesz kontekst |
| `tail` / `head` | ostatnie / pierwsze `lines` linii | podsumowanie, początek instalatora |
| `quiet` | nic prócz statystyk | interesuje cię sam kod wyjścia |
| `full` | całość przyciętą do `max_chars` | krótkie polecenia: `dir`, `git status`, `python --version` |

Reguła kciuka: **`full` tylko wtedy, gdy spodziewasz się kilkudziesięciu linii.**
Dla wszystkiego, co kompiluje, instaluje lub testuje — `auto`.

Widok `errors` zwraca listę `findings`:

```json
{"exit_code": 1, "verdict": "failed", "status_line": "BUILD FAILED in 1m 12s",
 "counts": {"error": 3, "warning": 1}, "findings_total": 3, "lines_total": 40014,
 "findings": [{"severity": "error", "file": "C:/Projekt/app/.../MainActivity.kt",
               "line": 42, "message": "Unresolved reference: bindig",
               "log_line": 20003, "rule": "kotlin-error"}]}
```

Rozróżniaj dwa numery: `file` + `line` to miejsce **w kodzie** (tam idziesz
naprawiać), `log_line` to miejsce **w logu** (tam idziesz po kontekst).

Gdy `used_generic_rules` jest `true`, most nie rozpoznał formatu narzędzia i
pokazał linie wyglądające na błędy — traktuj je ostrożniej, warto potwierdzić
przez `grep`.

## Drążenie logu bez powtarzania polecenia

```json
POST /logs
{"session": "build", "view": "around", "line": 20003, "context": 10}
{"session": "build", "view": "grep", "pattern": "Unresolved reference", "context": 2}
{"session": "build", "seq": 7, "view": "tail", "lines": 40}
```

`seq` domyślnie wskazuje ostatnie polecenie sesji. Ponowne uruchamianie buildu
tylko po to, żeby zobaczyć więcej logu, kosztuje minuty i nic nie wnosi — log
leży na dysku i jest do przeszukania w milisekundach.

## Sesje

| Cel | Żądanie |
|---|---|
| Nowa sesja | `POST /sessions` `{"session": "build", "cwd": "C:\\Projekt"}` |
| Świeży CMD w istniejącej sesji | `POST /sessions/build/restart` |
| Lista sesji | `GET /sessions` |
| Zamknięcie | `DELETE /sessions/build` |

Trzymaj osobne sesje dla osobnych zadań: `build` dla kompilacji, `main` dla
zwykłych poleceń, `serwer` dla procesu w tle. Sesja wykonuje jedno polecenie
naraz — próba równoległego `run` w tej samej sesji zwróci 409.

Restartuj sesję, gdy powłoka się zawiesiła, timeout nie dał się przerwać
(`dirty: true`) albo środowisko jest zepsute i chcesz czystego startu.

## Programy interaktywne i długo działające

- Program pyta o dane: `POST /sessions/main/stdin` `{"data": "y"}` (znak nowej
  linii dopisywany automatycznie).
- Przerwanie: `POST /sessions/main/interrupt` — ubija procesy potomne, sama
  powłoka przeżywa, więc sesja zwykle nadaje się do dalszej pracy.
- Podgląd na żywo: `GET /sessions/build/output?since=<offset>` działa **także
  wtedy, gdy sesja liczy build**. Przekazuj `offset` z poprzedniej odpowiedzi,
  żeby dostawać tylko nowe linie.
- Naprawdę długie zadania: albo `timeout` do 3600 s, albo uruchom w tle
  (`start /b cmd /c "gradlew.bat assembleDebug > build.log 2>&1"`) i odpytuj
  plik — przydatne, gdy twój kanał HTTP nie wytrzyma kilkunastominutowego żądania.

## Gdy coś pójdzie nie tak

| Kod | Znaczenie | Reakcja |
|---|---|---|
| 400 | złe żądanie (np. wadliwy `pattern`) | popraw JSON, sprawdź komunikat |
| 401 | brak/zły token | dopisz `X-Bridge-Token` z `bridge.json` |
| 403 | polityka zablokowała polecenie albo użytkownik je odrzucił | **nie obchodź blokady** — wyjaśnij użytkownikowi, czego potrzebujesz i dlaczego |
| 404 | nie ma sesji lub logu | `GET /sessions`, sprawdź `seq` |
| 409 | sesja zajęta / martwa / niespójna | `interrupt`, `restart` lub nowa sesja |

`timed_out: true` w odpowiedzi 200 znaczy, że polecenie nie zdążyło: przy
`recovered: true` sesja żyje dalej, przy `false` wykonaj `restart`. Zanim
zwiększysz `timeout`, zastanów się, czy polecenie w ogóle powinno tyle trwać —
czasem to zawieszony program czekający na wejście.

## Etykieta

Most daje realny dostęp do komputera użytkownika, nie do piaskownicy. Zanim
zrobisz coś nieodwracalnego (kasowanie katalogów, `git reset --hard`, instalacje
systemowe, zmiany w rejestrze) — zapytaj. Nie czytaj plików z sekretami, jeśli
zadanie tego nie wymaga. Trzymaj się katalogu projektu.

## Materiały szczegółowe

- **Android / Gradle** — `references/android.md`: przygotowanie środowiska,
  pierwszy build, katalog typowych błędów (Kotlin, AAPT2, manifest merger,
  duplicate class, OOM, brak SDK) wraz z naprawami, instalacja APK przez adb.
  Przeczytaj, zanim zaczniesz cokolwiek budować na Androida.
- **Zdalny dostęp (relay)** — `references/relay.md`: publiczny link z tokenem,
  API `/api/run` i `/api/result`, sprawdzanie `worker_online`. Przeczytaj, gdy
  działasz poza komputerem użytkownika i dostałeś link zamiast `127.0.0.1`.
- **Pełne API** — `references/api.md`: wszystkie endpointy, parametry widoków,
  format trybu plikowego, pola odpowiedzi.
