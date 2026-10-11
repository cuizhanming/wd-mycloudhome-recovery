# Build HomeDuoRecovery.exe (one folder app in dist\HomeDuoRecovery\).
# The exe requests administrator rights on start (needed to read a physical disk).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller
.\.venv\Scripts\pyinstaller --noconfirm --clean --windowed --uac-admin `
    --name HomeDuoRecovery `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.Qt3DCore `
    HomeDuoRecovery.py
Copy-Item README.md dist\HomeDuoRecovery\README.md
Compress-Archive -Path dist\HomeDuoRecovery -DestinationPath dist\HomeDuoRecovery-windows.zip -Force
Write-Host "Built: $PSScriptRoot\dist\HomeDuoRecovery-windows.zip (give this file to users)"
