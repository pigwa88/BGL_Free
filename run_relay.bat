@echo off
rem Uruchomienie publicznego relaya CMD Bridge.
rem Relay stawiasz tam, gdzie AI ma sie laczyc (publiczny serwer albo ten PC
rem wystawiony na swiat). Najlepiej za HTTPS/reverse-proxy.

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Nie znaleziono Pythona w PATH. Zainstaluj Python 3.8+ i sprobuj ponownie.
    pause
    exit /b 1
)

python -m cmdbridge.relay %*

echo.
echo Relay zakonczyl prace.
pause
endlocal
