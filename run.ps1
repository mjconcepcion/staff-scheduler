# Staff Scheduler launcher (Windows PowerShell)
# First run creates a local virtual environment and installs dependencies.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}

Write-Host "Starting Staff Scheduler..." -ForegroundColor Green
& ".venv\Scripts\python.exe" -m streamlit run app.py
