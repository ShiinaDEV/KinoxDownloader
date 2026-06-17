@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>nul
if errorlevel 1 (
    echo Python wurde nicht gefunden. Bitte Python 3.12 oder neuer installieren.
    exit /b 1
)

echo Installiere Python-Abhaengigkeiten...
python -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%

python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 exit /b %errorlevel%

echo Installiere Chromium fuer Browsermodus...
python -m patchright install chromium
if errorlevel 1 exit /b %errorlevel%

echo.
echo Installation fertig.
exit /b 0
