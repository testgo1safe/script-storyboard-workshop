$ErrorActionPreference = "Stop"

# Use your existing Chrome profile (where ChatGPT is already logged in).
# You MUST fully quit Chrome before running this script.

$port = if ($env:CHATGPT_CDP_PORT) { $env:CHATGPT_CDP_PORT } else { "9222" }
$profile = if ($env:CHATGPT_BROWSER_PROFILE) {
    $env:CHATGPT_BROWSER_PROFILE
} else {
    Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
}

$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$browser = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) {
    throw "Chrome not found."
}

$chromeProcs = Get-Process chrome -ErrorAction SilentlyContinue
if ($chromeProcs) {
    Write-Host "WARNING: Chrome is still running. Please quit Chrome completely, then run this again."
    Write-Host "Running processes:"
    $chromeProcs | Select-Object Id, MainWindowTitle | Format-Table -AutoSize
    exit 1
}

Write-Host "Launching Chrome with your existing profile..."
Write-Host "Browser: $browser"
Write-Host "Profile: $profile"
Write-Host "CDP port: $port"
Write-Host "This should reuse your existing ChatGPT login."

Start-Process -FilePath $browser -ArgumentList @(
    "--remote-debugging-port=$port",
    "--remote-debugging-address=127.0.0.1",
    "--remote-allow-origins=*",
    "--user-data-dir=`"$profile`"",
    "--no-first-run",
    "https://chatgpt.com/"
)

