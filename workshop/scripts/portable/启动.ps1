#Requires -Version 5.1
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Workshop = Join-Path $Root "workshop"
$ImageApi = Join-Path $Root "image-api"
$Venv = Join-Path $Root ".venv"
$PidFile = Join-Path $Root ".runtime-pids.txt"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Find-Python {
    $candidates = @()
    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { $candidates += $found.Source }
    }
    foreach ($exe in $candidates) {
        try {
            $ver = & $exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 10) {
                return $exe
            }
        } catch { }
    }
    throw "未找到 Python 3.10+。请先安装 https://www.python.org/downloads/ 并勾选 Add python.exe to PATH。"
}

function Test-Port([int]$Port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $Port)
        $c.Close()
        return $true
    } catch {
        return $false
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
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    return $paths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

Write-Host ""
Write-Host "========== 剧本工坊 一键启动 =========="
Write-Host "目录: $Root"
Write-Host ""

if (-not (Test-Path (Join-Path $Workshop "run.py"))) {
    throw "缺少 workshop\run.py，请确认便携包完整。"
}
if (-not (Test-Path (Join-Path $ImageApi "app\main.py"))) {
    throw "缺少 image-api，请确认便携包完整。"
}

$py = Find-Python
Write-Host "Python: $py"

$venvPy = Join-Path $Venv "Scripts\python.exe"
$venvPip = Join-Path $Venv "Scripts\pip.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "首次运行：创建虚拟环境…"
    & $py -m venv $Venv
}
if (-not (Test-Path $venvPy)) {
    throw "虚拟环境创建失败。"
}

$marker = Join-Path $Venv ".deps-ok"
if (-not (Test-Path $marker)) {
    Write-Host "首次运行：安装依赖（可能需要几分钟）…"
    & $venvPip install --upgrade pip
    & $venvPip install -r (Join-Path $Workshop "requirements.txt")
    & $venvPip install -r (Join-Path $ImageApi "requirements.txt")
    Write-Host "安装 Playwright Chromium…"
    & $venvPy -m playwright install chromium
    "ok" | Set-Content -Encoding ascii $marker
}

$env:CHATGPT_API_KEY = "local-dev-key"
$env:CHATGPT_API_HOST = "127.0.0.1"
$env:CHATGPT_API_PORT = "8765"
$env:CHATGPT_BROWSER = "chrome"
$env:CHATGPT_IMAGE_MODE = "ui"
$env:CHATGPT_CDP_URL = "http://127.0.0.1:9222"

$chrome = Find-Chrome
$profile = Join-Path $env:LOCALAPPDATA "剧本工坊-chrome"
if ($chrome -and -not (Test-Port 9222)) {
    New-Item -ItemType Directory -Force -Path $profile | Out-Null
    Write-Host "启动调试 Chrome（首次请在弹出窗口登录 chatgpt.com，然后保持开着）…"
    Start-Process -FilePath $chrome -ArgumentList @(
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--user-data-dir=`"$profile`"",
        "--no-first-run",
        "--no-default-browser-check",
        "https://chatgpt.com/"
    )
} elseif (-not $chrome) {
    Write-Host "未找到 Chrome。出图需要 Chrome；工坊仍会启动，可勾选「跳过出图」。"
} else {
    Write-Host "已检测到 9222 调试端口，复用当前 Chrome。"
}

$pids = @()
if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force
}

if (-not (Test-Port 8765)) {
    Write-Host "启动出图服务 http://127.0.0.1:8765 …"
    $img = Start-Process -FilePath $venvPy -ArgumentList @("-m", "app.main") `
        -WorkingDirectory $ImageApi `
        -RedirectStandardOutput (Join-Path $LogDir "image-api.out.log") `
        -RedirectStandardError (Join-Path $LogDir "image-api.err.log") `
        -PassThru -WindowStyle Minimized
    $pids += $img.Id
} else {
    Write-Host "出图服务已在 8765 运行。"
}

if (-not (Test-Port 8000)) {
    Write-Host "启动剧本工坊 http://127.0.0.1:8000 …"
    $ws = Start-Process -FilePath $venvPy -ArgumentList @(
            "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
        ) `
        -WorkingDirectory $Workshop `
        -RedirectStandardOutput (Join-Path $LogDir "workshop.out.log") `
        -RedirectStandardError (Join-Path $LogDir "workshop.err.log") `
        -PassThru -WindowStyle Minimized
    $pids += $ws.Id
} else {
    Write-Host "工坊已在 8000 运行。"
}

$pids | Set-Content -Encoding ascii $PidFile

Write-Host "等待服务就绪…"
$okImg = Wait-Http "http://127.0.0.1:8765/docs" 50
$okWs = Wait-Http "http://127.0.0.1:8000" 50
if (-not $okImg) { Write-Host "出图服务未在 50 秒内就绪，可看 logs\image-api.err.log" }
if (-not $okWs) { Write-Host "工坊未在 50 秒内就绪，可看 logs\workshop.err.log" }

Start-Process "http://127.0.0.1:8000"

Write-Host ""
Write-Host "工坊:  http://127.0.0.1:8000"
Write-Host "出图:  http://127.0.0.1:8765"
Write-Host "停止:  双击 停止.bat"
Write-Host "出图前请确认 Chrome 已登录 ChatGPT，并保持调试窗口开着。"
Write-Host ""

