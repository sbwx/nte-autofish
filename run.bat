@echo off
cd /d "%~dp0"

:: windows blocks synthetic key/mouse input from a lower-privilege
:: process to a higher one. if the game is running as admin (common
:: with anti-cheat games), our F presses get silently dropped. so we
:: re-launch ourselves with admin if we aren't already.
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo asking for admin so windows lets us send keys to the game...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: elevation drops us in System32, cd back
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo venv not found. run install.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe main.py %*
pause
