# Strona-pośrednik (`console.html`)

Jeden plik HTML do wgrania na **własny hosting**. To przez niego wysyłasz
polecenia do CMD i odbierasz odpowiedzi — bez PHP, bazy danych i bibliotek
zewnętrznych. Działa na zwykłym hostingu statycznym (także GitHub Pages).

```
  console.html  ──AJAX──►  RELAY  ──kolejka──►  TUNEL na PC  ──►  CMD
   (twój hosting)         (publiczny)          (u ciebie)      (wykonanie)
```

Strona sama nie dotyka CMD — rozmawia wyłącznie z relayem. Całe wykonanie
i wszystkie blokady bezpieczeństwa zostają po stronie PC.

## Masz dwie możliwości

| | Wbudowana konsola relaya | Ten plik (`console.html`) |
|---|---|---|
| Adres | `https://relay/?token=…` | `https://twoja-strona/cmd/console.html?…` |
| Wgrywanie czegokolwiek | nie trzeba | wgrywasz 1 plik |
| Własna domena i wygląd | nie | tak |
| Wymaga CORS | nie | tak (patrz niżej) |

Jeśli wystarcza ci konsola pod adresem relaya — ten plik jest zbędny. Użyj go,
gdy chcesz mieć wejście **na własnej stronie**.

## Instalacja

1. Skopiuj `console.html` na hosting, np. do `public_html/cmd/console.html`.
2. Uruchom relay z zgodą dla swojej domeny:

   ```bat
   python -m cmdbridge.relay --host 0.0.0.0 --port 9000 ^
          --public-url https://relay.twoja-domena ^
          --allow-origin https://twoja-strona
   ```

3. Na PC podłącz tunel (token łącza wypisze relay):

   ```bat
   python -m cmdbridge --relay https://relay.twoja-domena --relay-token <TOKEN_ŁĄCZA>
   ```

4. Wejdź (albo daj AI) link z tokenem dostępu:

   ```
   https://twoja-strona/cmd/console.html?relay=https://relay.twoja-domena&token=<TOKEN_DOSTĘPU>
   ```

Adres relaya i token zapamiętują się w przeglądarce, więc kolejnym razem
wystarczy sam adres strony. Token znika z paska adresu zaraz po wczytaniu, żeby
nie został w historii ani na zrzucie ekranu.

Jeśli chcesz mieć adres relaya wpisany na sztywno (żeby link był krótszy),
ustaw `RELAY_DEFAULT` w skrypcie na początku sekcji `<script>`.

## Co potrafi

- uruchamianie poleceń (**Ctrl+Enter**), wybór sesji, `timeout`;
- wybór **widoku** (`auto`, `errors`, `grep`, `tail`, `full`…) — filtrowanie
  dzieje się po stronie mostu, więc z 40 000 linii buildu dostajesz kilkanaście;
- błędy z widoku `errors` wypisane na górze, z plikiem i numerem linii;
- **auto-odświeżanie** podglądu wyjścia w trakcie działania polecenia
  (checkbox „podgląd na żywo", regulowany interwał);
- `przerwij`, `restart sesji`, `sesje`, `/logs ostatniego`;
- pasek stanu z informacją, czy PC jest podłączony;
- historia poleceń pod **Ctrl+↑ / Ctrl+↓**.

## Gdy nie działa

| Objaw | Przyczyna | Naprawa |
|---|---|---|
| „Nie mogę połączyć się z relayem" | CORS albo zły adres | uruchom relay z `--allow-origin https://twoja-strona` |
| „Relay odrzucił token" | zły token dostępu | sprawdź token wypisany przez relay |
| `PC offline` | tunel nie działa | na PC: `python -m cmdbridge --relay … --relay-token …` |
| Strona z HTTPS nie widzi relaya z HTTP | mixed content | postaw relay za HTTPS (reverse-proxy) |

## Bezpieczeństwo

Ta strona to zdalne wykonywanie poleceń na twoim komputerze. Token dostępu
traktuj jak hasło: nie publikuj linku, nie wklejaj go w miejsca publiczne.
Strona świadomie trzyma token w `localStorage` (wygoda) — na komputerze
współdzielonym używaj przycisku **wyloguj**. Rozważ też uruchomienie mostu
z `--confirm`, wtedy każde polecenie zatwierdzasz ręcznie w konsoli PC.
