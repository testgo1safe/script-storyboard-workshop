$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $env:CHATGPT_API_KEY) {
    $env:CHATGPT_API_KEY = "local-dev-key"
}
if (-not $env:CHATGPT_API_HOST) {
    $env:CHATGPT_API_HOST = "0.0.0.0"
}
if (-not $env:CHATGPT_BROWSER) {
    $env:CHATGPT_BROWSER = "chrome"
}
if (-not $env:CHATGPT_IMAGE_MODE) {
    $env:CHATGPT_IMAGE_MODE = "ui"
}

Write-Host "Installing dependencies..."
python -m pip install -r requirements.txt
python -m playwright install chromium

Write-Host "Starting API on http://0.0.0.0:8765 (LAN accessible)"
python -m app.main

