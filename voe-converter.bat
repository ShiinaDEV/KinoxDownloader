@echo off
chcp 65001 >nul
python -X utf8 "%~dp0voe_converter.py" %*
