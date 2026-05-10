# install everything needed to run the script.
#
# usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# what this does:
#   1. checks for python, installs it via winget if missing
#   2. creates a venv in .\venv
#   3. installs the packages from requirements.txt into the venv
#
# if it fails, see the messages it prints. usually you just need to
# install python manually from https://www.python.org/downloads/ and
# rerun this script.

$ErrorActionPreference = "Stop"

function Has-Command($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# ---- 1. python ----
if (Has-Command python) {
    $ver = (python --version 2>&1)
    Write-Host "python: $ver"
} else {
    Write-Host "python not found. trying winget..."
    if (-not (Has-Command winget)) {
        Write-Host "winget isn't on this machine either."
        Write-Host "install python yourself: https://www.python.org/downloads/"
        Write-Host "tick 'add python to PATH' in the installer, then rerun this script."
        exit 1
    }
    winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
    # winget puts python on PATH for new sessions but not the one we're
    # in right now. refresh PATH so the rest of this script can find it.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Has-Command python)) {
        Write-Host "python installed but isn't on PATH yet."
        Write-Host "close this terminal, open a new one, and rerun the script."
        exit 1
    }
    $ver = (python --version 2>&1)
    Write-Host "python installed: $ver"
}

# ---- 2. venv ----
if (-not (Test-Path ".\venv")) {
    Write-Host "creating venv..."
    python -m venv venv
} else {
    Write-Host "venv already exists, skipping"
}

# ---- 3. packages ----
# call the venv's python directly so we don't have to activate first
$venvPython = ".\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "venv looks broken (no python.exe inside it). delete the venv folder and rerun."
    exit 1
}

Write-Host "installing packages..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host ""
Write-Host "done. to run the script:"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "  python main.py"
