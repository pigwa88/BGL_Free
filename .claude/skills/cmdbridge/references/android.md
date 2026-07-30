# Budowanie aplikacji na Androida przez CMD Bridge

Build Androida to najtrudniejszy przypadek dla mostu: trwa minuty, generuje
dziesiątki tysięcy linii i potrafi zawieść na kilkanaście różnych sposobów.
Ten dokument opisuje przepływ, który działa, i katalog typowych błędów.

## Spis treści

1. [Rekonesans środowiska](#1-rekonesans-środowiska)
2. [Licencje SDK](#2-licencje-sdk)
3. [Pierwszy build](#3-pierwszy-build)
4. [Czytanie wyniku](#4-czytanie-wyniku)
5. [Katalog błędów i naprawy](#5-katalog-błędów-i-naprawy)
6. [Przydatne wzorce grep](#6-przydatne-wzorce-grep)
7. [Instalacja i uruchomienie na urządzeniu](#7-instalacja-i-uruchomienie-na-urządzeniu)
8. [Inne toolchainy](#8-inne-toolchainy)

## 1. Rekonesans środowiska

Zanim uruchomisz build, sprawdź, czy jest czym budować. To krótkie polecenia —
tu `view: "full"` jest w porządku. Wykonaj je w dedykowanej sesji `build`
z `cwd` ustawionym na katalog projektu.

```json
{"command": "java -version", "session": "build", "view": "full"}
{"command": "echo %ANDROID_HOME% %ANDROID_SDK_ROOT%", "session": "build", "view": "full"}
{"command": "dir gradlew.bat local.properties", "session": "build", "view": "full"}
{"command": "type local.properties", "session": "build", "view": "full"}
```

Na co patrzeć:

- **JDK**: Android Gradle Plugin 8.x wymaga JDK 17, AGP 7.x — JDK 11.
  `Unsupported class file major version` w logu = zła wersja.
- **SDK**: albo zmienna `ANDROID_HOME`, albo `sdk.dir` w `local.properties`.
  Brak obu = `SDK location not found`.
- **Wrapper**: buduj przez `gradlew.bat`, nie przez globalne `gradle` — wrapper
  pobiera wersję Gradle zgodną z projektem.

## 2. Licencje SDK

Niezaakceptowane licencje wywracają build z komunikatem
`Failed to install the following Android SDK packages ... not accepted licenses`.

`sdkmanager --licenses` pyta interaktywnie, więc odpowiadaj przez `stdin`:

```json
{"command": "%ANDROID_HOME%\\cmdline-tools\\latest\\bin\\sdkmanager.bat --licenses",
 "session": "build", "timeout": 300, "view": "tail", "lines": 5}
```

a następnie, dla każdego pytania:

```json
POST /sessions/build/stdin
{"data": "y"}
```

Jeśli pytań jest wiele, prościej poprosić użytkownika o jednorazowe
zaakceptowanie licencji ręcznie — to czynność jednorazowa na maszynę.

## 3. Pierwszy build

```json
{"command": "gradlew.bat assembleDebug --console=plain",
 "session": "build", "timeout": 1800, "view": "auto"}
```

Dlaczego akurat tak:

- `--console=plain` — bez tego Gradle rysuje pasek postępu znakami `\r` i kodami
  ANSI; most je czyści, ale log i tak robi się nieczytelny.
- `timeout: 1800` — pierwszy build ściąga zależności i potrafi trwać 10–30 minut.
  Domyślne 60 s przerwałoby go w połowie. Kolejne buildy: 300–600 s wystarczy.
- `view: "auto"` — przy sukcesie dostaniesz ostatnie linie, przy porażce błędy.
- Sesja `build` osobno od `main`, bo build blokuje sesję na cały czas trwania.

Warianty zadań: `assembleDebug` (APK debug), `installDebug` (build + instalacja
na podłączonym urządzeniu), `bundleRelease` (AAB do sklepu, wymaga podpisu),
`testDebugUnitTest` (testy jednostkowe), `clean` (gdy podejrzewasz zepsuty cache).

Gdy twój kanał HTTP nie wytrzyma kilkunastominutowego żądania, uruchom build
w tle i odpytuj plik:

```json
{"command": "start /b cmd /c \"gradlew.bat assembleDebug --console=plain > build.log 2>&1\"",
 "session": "build", "view": "quiet"}
```

a potem co jakiś czas:

```json
{"command": "type build.log", "session": "build", "view": "tail", "lines": 5}
```

## 4. Czytanie wyniku

Przy `exit_code == 0` sprawdź, gdzie wylądował artefakt — most sam wyłapuje
ścieżki `.apk`/`.aab` do pola `artifacts`. Jeśli go nie ma:

```json
{"command": "dir /s /b app\\build\\outputs\\apk\\debug\\*.apk", "session": "build", "view": "full"}
```

Przy `exit_code != 0` masz w `findings` błędy z plikiem i linią. Kolejność
działań, która oszczędza czas:

1. Przeczytaj `status_line` i pierwsze `findings` — zwykle to wystarcza.
2. Potrzebujesz kontekstu? `POST /logs` z `view: "around"` i `log_line` błędu.
3. Podejrzewasz głębszą przyczynę? `view: "grep"`, `pattern: "Caused by"`.
4. Dopiero gdy to nie pomaga — powtórz build z `--stacktrace` albo `--info`
   i znów filtruj przez `grep`. Nigdy nie ciągnij całego logu „na wszelki wypadek".

## 5. Katalog błędów i naprawy

### Kompilacja Kotlin/Java

| Komunikat | Przyczyna | Naprawa |
|---|---|---|
| `e: ... Unresolved reference: X` | literówka, brak importu, nieistniejący symbol | otwórz plik z `file`+`line`; sprawdź import i nazwę |
| `e: ... Type mismatch: inferred type is A but B was expected` | zły typ | popraw typ albo konwersję w linii z `findings` |
| `error: cannot find symbol` (javac) | brak klasy/metody, brak zależności | sprawdź import i `dependencies` w `build.gradle` |
| `Unsupported class file major version 6x` | biblioteka zbudowana nowszym JDK niż używany | podnieś JDK albo obniż wersję zależności |

### Zasoby i manifest

| Komunikat | Przyczyna | Naprawa |
|---|---|---|
| `AAPT: error: resource drawable/x not found` | brak pliku zasobu lub literówka | sprawdź `app/src/main/res/...`; nazwy zasobów są bez rozszerzenia |
| `AAPT: error: attribute X not found` | atrybut spoza dostępnego API | sprawdź `compileSdk` |
| `Manifest merger failed : Attribute ... value=(a) from ... is also present at ... value=(b)` | konflikt manifestów aplikacji i biblioteki | dodaj `tools:replace="..."` w `<application>` albo ujednolić wartości |
| `Manifest merger failed : uses-sdk:minSdkVersion 21 cannot be smaller than version 24 declared in library` | biblioteka wymaga wyższego `minSdk` | podnieś `minSdk` w `app/build.gradle` |

### Zależności i dex

| Komunikat | Przyczyna | Naprawa |
|---|---|---|
| `Duplicate class a.b.C found in modules X and Y` | ta sama klasa w dwóch zależnościach | `gradlew.bat :app:dependencies` → wyklucz jedną: `exclude group: "...", module: "..."` |
| `Could not resolve com.example:lib:1.2.3` | brak sieci, zła wersja, brak repozytorium | sprawdź sieć; `--offline` jeśli jest w cache; sprawdź `repositories` |
| `Cannot fit requested classes in a single dex file` | przekroczony limit 64K metod | `multiDexEnabled true` w `defaultConfig` albo włącz `minifyEnabled` |
| `Connection timed out` / `Could not GET https://...` | proxy/firewall | zapytaj użytkownika o proxy; ewentualnie `--offline` |

### Środowisko i Gradle

| Komunikat | Przyczyna | Naprawa |
|---|---|---|
| `SDK location not found` | brak `ANDROID_HOME` i `local.properties` | utwórz `local.properties` z `sdk.dir=C\:\\Users\\...\\AppData\\Local\\Android\\Sdk` |
| `Failed to install the following SDK packages ... not accepted licenses` | licencje | patrz sekcja 2 |
| `java.lang.OutOfMemoryError: Java heap space` | za mały heap dla Gradle/Kotlin | `org.gradle.jvmargs=-Xmx4g` w `gradle.properties` |
| `Gradle daemon disappeared unexpectedly` | OOM lub ubity proces | jw. + `restart` sesji, potem build ponownie |
| `Execution failed for task ':app:lintVitalRelease'` | lint blokuje release | napraw wskazane problemy albo `-x lintVitalRelease` (świadomie, po uzgodnieniu) |
| `Keystore file not found` / `Failed to read key` | brak lub zły podpis release | build debug albo uzgodnij z użytkownikiem dane keystore — **nie zgaduj haseł** |

Zasada przy zmianach w plikach projektu: pokaż użytkownikowi, co i dlaczego
zmieniasz w `build.gradle`, `gradle.properties` czy manifeście. To pliki, które
wpływają na cały projekt, nie na jeden build.

## 6. Przydatne wzorce grep

Do użycia w `POST /logs` z `view: "grep"`:

| `pattern` | Co znajduje |
|---|---|
| `^> Task .* FAILED` | które zadanie Gradle padło |
| `Caused by` | prawdziwa przyczyna pod stosem wyjątków |
| `^e: ` | wszystkie błędy Kotlina |
| `AAPT.*error` | problemy z zasobami |
| `Could not resolve\|Could not GET` | problemy z pobieraniem zależności |
| `Duplicate class` | konflikty zależności |
| `\.apk\|\.aab` | ścieżki zbudowanych artefaktów |
| `Deprecated\|deprecation` | ostrzeżenia o przestarzałym API (zwykle da się zignorować) |

Dodaj `"context": 3`, gdy sam pasujący wiersz nie wystarcza.

## 7. Instalacja i uruchomienie na urządzeniu

```json
{"command": "adb devices", "session": "build", "view": "full"}
{"command": "adb install -r app\\build\\outputs\\apk\\debug\\app-debug.apk",
 "session": "build", "timeout": 300, "view": "auto"}
{"command": "adb logcat -d -s AndroidRuntime:E", "session": "build",
 "view": "tail", "lines": 40}
```

- `adb devices` puste → urządzenie niepodłączone albo brak zgody na debugowanie
  USB; poproś użytkownika o odblokowanie telefonu i potwierdzenie okna.
- `INSTALL_FAILED_UPDATE_INCOMPATIBLE` → aplikacja jest już zainstalowana z innym
  podpisem: `adb uninstall <package>` (za zgodą użytkownika — kasuje dane).
- `INSTALL_FAILED_INSUFFICIENT_STORAGE` → brak miejsca na urządzeniu.
- Do diagnozy crashy: `adb logcat -d` z filtrem, nigdy bez — pełny logcat to
  dziesiątki tysięcy linii.

## 8. Inne toolchainy

Zasady są te same (`view: auto`, długi `timeout`, drążenie przez `/logs`),
różnią się tylko polecenia:

- **Flutter**: `flutter build apk --debug`; błędy Dart rozpoznaje reguła ogólna,
  więc warto dopytać `grep` o `Error:` lub `^lib/`.
- **React Native**: `npx react-native run-android` — pod spodem i tak jest Gradle,
  więc `findings` działają jak wyżej; błędy Metro szukaj przez `grep` po `error`.
- **Buildozer (Python/Kivy)**: `buildozer android debug`, bardzo długi pierwszy
  build (NDK) — ustaw `timeout: 3600` albo uruchom w tle.
- **Cordova/Capacitor**: `npx cap sync android` + `gradlew.bat assembleDebug`.
