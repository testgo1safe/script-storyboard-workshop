#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Workshop = Join-Path $Root "workshop"
$ImageApi = Join-Path $Root "image-api"
$Venv = Join-Path $Root ".venv"
$PidFile = Join-Path $Root ".runtime-pids.txt"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Find-Python {
    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        try {
            $ver = & $found.Source -c "import sys; print('%d.%d' % (sys.version_info.major, sys.version_info.minor))" 2>$null
            if ($ver -match "^3\.(10|11|12|13|14)") {
                return $found.Source
            }
        } catch { }
    }
    throw "Python 3.10+ not found. Install from https://www.python.org/downloads/ and check Add python.exe to PATH."
}

function Test-Port([int]$Port) {
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(300)
        if ($ok -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        if ($client) { $client.Close() }
    }
}

function Wait-Http([string]$Url, [int]$Seconds = 40) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -ge 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Find-Chrome {
    $paths = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    return $paths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

Write-Host ""
Write-Host "========== Storyboard Studio =========="
Write-Host "Root: $Root"
Write-Host ""

if (-not (Test-Path (Join-Path $Workshop "run.py"))) {
    throw "Missing workshop\run.py"
}
if (-not (Test-Path (Join-Path $ImageApi "app\main.py"))) {
    throw "Missing image-api"
}

$py = Find-Python
Write-Host "Python: $py"

$venvPy = Join-Path $Venv "Scripts\python.exe"
$venvPip = Join-Path $Venv "Scripts\pip.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "First run: creating venv..."
    & $py -m venv $Venv
}
if (-not (Test-Path $venvPy)) {
    throw "Failed to create venv"
}

$marker = Join-Path $Venv ".deps-ok"
if (-not (Test-Path $marker)) {
    Write-Host "First run: installing packages (a few minutes)..."
    & $venvPip install --upgrade pip
    & $venvPip install -r (Join-Path $Workshop "requirements.txt")
    & $venvPip install -r (Join-Path $ImageApi "requirements.txt")
    Write-Host "Installing Playwright Chromium..."
    & $venvPy -m playwright install chromium
    Set-Content -Path $marker -Value "ok" -Encoding Ascii
}

$env:CHATGPT_API_KEY = "local-dev-key"
$env:CHATGPT_API_HOST = "127.0.0.1"
$env:CHATGPT_API_PORT = "8765"
$env:CHATGPT_BROWSER = "chrome"
$env:CHATGPT_IMAGE_MODE = "ui"
$env:CHATGPT_CDP_URL = "http://127.0.0.1:9222"

$chrome = Find-Chrome
$profile = Join-Path $env:LOCALAPPDATA "storyboard-chrome"
if ($chrome -and -not (Test-Port 9222)) {
    New-Item -ItemType Directory -Force -Path $profile | Out-Null
    Write-Host "Starting Chrome debug window. Login chatgpt.com once and keep it open."
    Start-Process -FilePath $chrome -ArgumentList @(
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--user-data-dir=$profile",
        "--no-first-run",
        "--no-default-browser-check",
        "https://chatgpt.com/"
    )
} elseif (-not $chrome) {
    Write-Host "Chrome not found. Workshop will still start. Check skip-images if needed."
} else {
    Write-Host "Port 9222 already open, reuse Chrome."
}

$pids = @()
if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force
}

if (-not (Test-Port 8765)) {
    Write-Host "Starting image API http://127.0.0.1:8765"
    $img = Start-Process -FilePath $venvPy -ArgumentList @("-m", "app.main") `
        -WorkingDirectory $ImageApi `
        -RedirectStandardOutput (Join-Path $LogDir "image-api.out.log") `
        -RedirectStandardError (Join-Path $LogDir "image-api.err.log") `
        -PassThru -WindowStyle Minimized
    $pids += $img.Id
} else {
    Write-Host "Image API already on 8765"
}

if (-not (Test-Port 8000)) {
    Write-Host "Starting workshop http://127.0.0.1:8000"
    $ws = Start-Process -FilePath $venvPy -ArgumentList @(
            "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
        ) `
        -WorkingDirectory $Workshop `
        -RedirectStandardOutput (Join-Path $LogDir "workshop.out.log") `
        -RedirectStandardError (Join-Path $LogDir "workshop.err.log") `
        -PassThru -WindowStyle Minimized
    $pids += $ws.Id
} else {
    Write-Host "Workshop already on 8000"
}

Set-Content -Path $PidFile -Value ($pids | ForEach-Object { "$_" }) -Encoding Ascii

Write-Host "Waiting for services..."
$okImg = Wait-Http "http://127.0.0.1:8765/docs" 50
$okWs = Wait-Http "http://127.0.0.1:8000" 50
if (-not $okImg) { Write-Host "Image API not ready. See logs\image-api.err.log" }
if (-not $okWs) { Write-Host "Workshop not ready. See logs\workshop.err.log" }

Start-Process "http://127.0.0.1:8000"

Write-Host ""
Write-Host "Workshop: http://127.0.0.1:8000"
Write-Host "Images:   http://127.0.0.1:8765"
Write-Host "Stop:     double-click stop.bat"
Write-Host ""

