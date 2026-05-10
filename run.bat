@echo off
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo venv not found. run install.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe main.py %*
pause
