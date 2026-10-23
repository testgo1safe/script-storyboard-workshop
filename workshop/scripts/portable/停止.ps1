#Requires -Version 5.1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root ".runtime-pids.txt"

Write-Host "正在停止剧本工坊和出图服务…"

if (Test-Path $PidFile) {
    Get-Content $PidFile | ForEach-Object {
        $procId = $_.Trim()
        if (-not $procId) { return }
        try {
            $p = Get-Process -Id ([int]$procId) -ErrorAction Stop
            if ($p.ProcessName -match "python|py") {
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                Write-Host "已结束 PID $($p.Id)"
            }
        } catch { }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -like "*workshop*run.py*" -or
            $_.CommandLine -like "*uvicorn*app.main:app*" -or
            $_.CommandLine -like "*image-api*app.main*" -or
            $_.CommandLine -like "* -m app.main*"
        )
    } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "已结束相关进程 $($_.ProcessId)"
        } catch { }
    }

Write-Host "完成。Chrome 调试窗口请自行关闭。"
Start-Sleep -Seconds 1

