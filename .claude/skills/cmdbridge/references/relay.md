# Zdalny dostęp przez relay (publiczny link z tokenem)

Domyślnie most słucha tylko na `127.0.0.1`, więc dosięgniesz go, będąc **na tym
samym komputerze**. Gdy działasz zdalnie (chmura, przeglądarka, inny host),
użytkownik może wystawić most przez **relay** — publiczny serwer-przekaźnik.

```
   ty (AI, zdalnie)  ──►  RELAY (publiczny, HTTPS)  ◄──  TUNEL (PC) ──► CMD Bridge
        link + token dostępu          token łącza (prywatny)        polityka + sesje
```

Relay **niczego nie wykonuje** — tylko kolejkuje żądania i wyniki. Całe
wykonanie i cała polityka bezpieczeństwa zostają po stronie PC (moduł `core`),
więc blokady poleceń działają tak samo jak lokalnie.

## Co dostajesz od użytkownika

Jedną z dwóch rzeczy (albo obie):

1. **Link do konsoli**: `https://domena/?token=<ACCESS>` — strona z AJAX,
   auto-odświeżaniem i podglądem sesji na żywo (dla człowieka lub agenta z
   przeglądarką).
2. **Bazowy adres + token dostępu** do API — i tego użyj jako agent HTTP.
   Endpointy i pola są **takie same jak w lokalnym moście**, zmienia się tylko
   adres bazowy i to, że autoryzujesz się tokenem dostępu relaya.

## API relaya (strona agenta)

Autoryzacja: nagłówek `X-Bridge-Token: <ACCESS>` (albo `?token=<ACCESS>`).

| Metoda i ścieżka | Opis |
|---|---|
| `GET /health` | żyje? czy PC (worker) jest online. Bez tokenu |
| `GET /api/status` | `worker_online`, `pending`, `worker_last_seen` |
| `POST /api/run?wait=30` | zgłoś polecenie i **poczekaj** na wynik (do `wait` s) |
| `POST /api/submit` | zgłoś polecenie, dostań `{"id": ...}` od razu |
| `GET /api/result?id=<id>&wait=25` | odbierz wynik zgłoszonego polecenia |

Ciało żądania jest **identyczne** jak w lokalnym `POST /run` / `POST /logs`,
tylko dodaj pole `op` (`run`, `logs`, `output`, `restart`, `interrupt`,
`sessions`, `health`, ...). Bez `op`, ale z `command`, przyjmuje się `run`.

### Ścieżka najprostsza — jedno żądanie

```json
POST /api/run?wait=60
{"op": "run", "command": "gradlew.bat assembleDebug --console=plain",
 "session": "build", "timeout": 1800, "view": "auto"}
```

- Kod **200** + zwykła odpowiedź mostu (`exit_code`, `output`, `findings`, ...)
  — gdy wynik zdążył wrócić w `wait` sekund.
- Kod **202** + `{"pending": true, "id": "..."}` — gdy jeszcze trwa. Dopytuj
  wtedy `GET /api/result?id=<id>&wait=25` w pętli, aż dostaniesz 200.

`wait` jest ograniczony do ~55 s (limit pojedynczego żądania). Dla długich
buildów: zgłoś raz i odpytuj `result` w pętli, albo puść build w tle i podglądaj
`op:"output"` (patrz `references/android.md`).

### Ścieżka rozłączna — zgłoś i odbierz

```json
POST /api/submit
{"op": "run", "command": "dir", "view": "full"}
      -> {"ok": true, "id": "18f...-7", "worker_online": true}

GET /api/result?id=18f...-7&wait=25
      -> 204 (jeszcze nie gotowe, pytaj dalej) albo 200 z wynikiem
```

Kod **204** znaczy „wciąż w toku" — po prostu ponów `GET /api/result`.

## Drążenie logów i sesje

Wszystko działa jak lokalnie, przez `op`:

```json
POST /api/run?wait=20
{"op": "logs", "session": "build", "view": "grep", "pattern": "Caused by", "context": 3}
{"op": "output", "session": "build", "since": 0}
{"op": "restart", "session": "build"}
{"op": "interrupt", "session": "build"}
```

## Zanim zaczniesz

- Sprawdź `GET /api/status` — jeśli `worker_online` jest `false`, PC nie jest
  podłączony do relaya. Poproś użytkownika o uruchomienie na PC:
  `python -m cmdbridge --relay <URL> --relay-token <CONNECT>`.
- Nie zobaczysz `connect-token` (jest prywatny na PC) i nie jest ci potrzebny —
  ty masz tylko `access-token` z linku.
- Kody błędów jak w API lokalnym: 401 = zły token dostępu, 403 = polityka
  zablokowała polecenie po stronie PC (nie obchodź, wyjaśnij użytkownikowi).

## Uwaga o bezpieczeństwie

Relay wystawia wykonywanie poleceń na PC do internetu. Traktuj token dostępu jak
hasło do zdalnego pulpitu: nie wklejaj go do logów, nie przekazuj dalej. Jeśli
polecenie jest nieodwracalne (kasowanie, `git reset --hard`, instalacje
systemowe), tak samo jak lokalnie — najpierw uzgodnij z użytkownikiem.
