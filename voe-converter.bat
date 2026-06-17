@echo off
chcp 65001 >nul
python -c "import patchright, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
    call "%~dp0install.bat"
    if errorlevel 1 exit /b %errorlevel%
)
python -X utf8 "%~dp0voe_converter.py" %*
