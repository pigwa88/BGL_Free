@echo off
rem Uruchomienie mostu CMD Bridge na Windows (podwojne klikniecie).
rem Katalog roboczy sesji = katalog tego pliku. Dodatkowo wlaczony tryb plikowy.

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Nie znaleziono Pythona w PATH. Zainstaluj Python 3.8+ i sprobuj ponownie.
    pause
    exit /b 1
)

python -m cmdbridge --cwd "%~dp0" --mailbox %*

echo.
echo Most zakonczyl prace.
pause
endlocal
