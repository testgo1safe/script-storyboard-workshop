$ErrorActionPreference = "Stop"

$port = if ($env:CHATGPT_CDP_PORT) { $env:CHATGPT_CDP_PORT } else { "9222" }
$browserKind = if ($env:CHATGPT_BROWSER) { $env:CHATGPT_BROWSER.ToLower() } else { "chrome" }

if ($env:CHATGPT_BROWSER_PROFILE) {
    $profile = $env:CHATGPT_BROWSER_PROFILE
} elseif ($browserKind -eq "edge") {
    $profile = Join-Path $env:LOCALAPPDATA "chatgpt-web-api-edge"
} else {
    $profile = Join-Path $env:LOCALAPPDATA "chatgpt-web-api-chrome"
}

$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$edgeCandidates = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
)

if ($browserKind -eq "edge") {
    $browser = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
} else {
    $browser = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $browser) {
    throw "Browser not found for CHATGPT_BROWSER=$browserKind"
}

New-Item -ItemType Directory -Force -Path $profile | Out-Null

Write-Host "Launching debug browser (dedicated profile)..."
Write-Host "Browser: $browser"
Write-Host "Profile: $profile"
Write-Host "CDP port: $port"
Write-Host "First run: login https://chatgpt.com in this window, then keep it open."

Start-Process -FilePath $browser -ArgumentList @(
    "--remote-debugging-port=$port",
    "--remote-debugging-address=127.0.0.1",
    "--remote-allow-origins=*",
    "--user-data-dir=`"$profile`"",
    "--no-first-run",
    "--no-default-browser-check",
    "https://chatgpt.com/"
)

