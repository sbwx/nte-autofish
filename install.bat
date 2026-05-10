@echo off
setlocal

cd /d "%~dp0"

echo checking for python...
where python >nul 2>nul
if %ERRORLEVEL% neq 0 goto need_python

for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo python found: %%i
goto setup_venv

:need_python
echo python not found. trying winget...
where winget >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo.
    echo winget isn't on this machine either.
    echo install python yourself: https://www.python.org/downloads/
    echo tick "add python to PATH" in the installer, then run install.bat again.
    echo.
    pause
    exit /b 1
)

winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
if %ERRORLEVEL% neq 0 (
    echo.
    echo winget install failed.
    pause
    exit /b 1
)

echo.
echo python installed. close this window, open a new one, and run install.bat again
echo so the new python is on your PATH.
pause
exit /b 0

:setup_venv
if not exist venv (
    echo creating venv...
    python -m venv venv
    if %ERRORLEVEL% neq 0 (
        echo venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo venv already exists, skipping
)

echo installing packages...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo package install failed.
    pause
    exit /b 1
)

echo.
echo done. double-click run.bat to start the script.
pause
