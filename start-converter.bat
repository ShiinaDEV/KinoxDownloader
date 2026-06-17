@echo off
chcp 65001 >nul
python -c "import patchright, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
    call "%~dp0install.bat"
    if errorlevel 1 exit /b %errorlevel%
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0voe-converter-menu.ps1"
