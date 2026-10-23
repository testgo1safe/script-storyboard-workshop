$ErrorActionPreference = "Stop"
Set-Location "E:\aippt\gpt-5.6-instruct\chatgpt-web-api"
$env:CHATGPT_BROWSER = "chrome"
$env:CHATGPT_API_KEY = "local-dev-key"

Write-Host "[1/4] Stop Chrome/Edge for cookie read..."
Stop-Process -Name chrome,msedge -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "[2/4] Restart API..."
$api = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess
if ($api) { Stop-Process -Id $api -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
Start-Process -WindowStyle Hidden -FilePath python -ArgumentList "-m","app.main" -WorkingDirectory (Get-Location)
Start-Sleep -Seconds 3

$headers = @{ Authorization = "Bearer local-dev-key" }

Write-Host "[3/4] Test session..."
try {
    $session = Invoke-RestMethod "http://127.0.0.1:8765/v1/session" -Headers $headers -TimeoutSec 60
    $session | ConvertTo-Json -Compress
} catch {
    Write-Host "SESSION FAIL:" $_.ErrorDetails.Message
}

Write-Host "[4/4] Test chat..."
$body = '{"model":"auto","messages":[{"role":"user","content":"只回复OK"}]}'
try {
    $chat = Invoke-RestMethod "http://127.0.0.1:8765/v1/chat/completions" -Method POST -Headers (@{Authorization='Bearer local-dev-key';'Content-Type'='application/json'}) -Body $body -TimeoutSec 120
    Write-Host "CHAT OK:" $chat.choices[0].message.content
} catch {
    Write-Host "CHAT FAIL:" $_.ErrorDetails.Message
}

Write-Host "Image test skipped in quick script (slow). Run manually if chat OK."

