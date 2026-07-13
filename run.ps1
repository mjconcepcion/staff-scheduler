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

# Keep the data-privacy pre-commit hook wired up (blocks committing *.db files).
if (Test-Path ".git") {
    git config core.hooksPath githooks
}

Write-Host "Starting Staff Scheduler..." -ForegroundColor Green
& ".venv\Scripts\python.exe" -m streamlit run app.py
