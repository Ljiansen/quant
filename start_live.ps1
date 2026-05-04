# start_live.ps1 - 一键启动：miniQMT + Dashboard + V3实盘引擎
# 用法：右键 → 用 PowerShell 运行，或终端执行 .\start_live.ps1

$ProjectDir  = "d:\miniqmt_quant"
$MiniQMTExe  = "D:\迅投QMT交易终端浙商证券金桥版\bin.x64\XtMiniQmt.exe"
$Port        = 8088
$CheckScript = "$env:TEMP\check_xt.py"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   V3 实盘策略  ·  一键启动脚本        " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── 步骤 1：检查/启动 miniQMT ─────────────────────
$qmtProc = Get-Process "XtMiniQmt" -ErrorAction SilentlyContinue
if ($qmtProc) {
    Write-Host "[OK] miniQMT 已在运行 (PID=$($qmtProc.Id))" -ForegroundColor Green
} else {
    if (-not (Test-Path $MiniQMTExe)) {
        Write-Host "[ERROR] 找不到 miniQMT：$MiniQMTExe" -ForegroundColor Red
        Read-Host "按回车退出"; exit 1
    }
    Write-Host "[..] 正在启动 miniQMT..." -ForegroundColor Yellow
    Start-Process -FilePath $MiniQMTExe
    Write-Host ""
    Write-Host "  +-------------------------------------------------+" -ForegroundColor Yellow
    Write-Host "  |  请在弹出的 miniQMT 窗口中完成登录              |" -ForegroundColor Yellow
    Write-Host "  |  登录成功后回到此窗口，按回车继续               |" -ForegroundColor Yellow
    Write-Host "  +-------------------------------------------------+" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "  [ 登录完成后按回车 ]"
}

# ── 步骤 2：等待 xtquant 服务就绪 ────────────────────
Write-Host "[..] 检测 xtquant 连接状态..." -ForegroundColor Yellow

# 写检测脚本到临时文件（避免 PowerShell 引号转义问题）
@"
import sys
try:
    import xtquant.xtdata as d
    r = d.connect()
    print('ok' if r == 0 else 'fail:' + str(r))
except Exception as e:
    print('fail:' + str(e))
"@ | Set-Content $CheckScript -Encoding UTF8

$xtReady  = $false
$deadline = [DateTime]::Now.AddSeconds(60)
$dotCount = 0
while ([DateTime]::Now -lt $deadline) {
    $result = python $CheckScript 2>$null
    if ($result -eq 'ok') { $xtReady = $true; break }
    $dotCount++
    Write-Host -NoNewline "." -ForegroundColor Gray
    Start-Sleep 2
}
if ($dotCount -gt 0) { Write-Host "" }

if ($xtReady) {
    Write-Host "[OK] xtquant 服务已就绪" -ForegroundColor Green
} else {
    Write-Host "[!!] xtquant 连接超时（miniQMT 未登录或服务未就绪）" -ForegroundColor Yellow
    $ans = Read-Host "    仍然继续启动？(y/N)"
    if ($ans.ToLower() -ne 'y') { exit 1 }
}

# ── 步骤 3：启动 Dashboard ────────────────────────────
$dashRunning = $false
try {
    Invoke-WebRequest -Uri "http://localhost:$Port" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop | Out-Null
    $dashRunning = $true
} catch {}

if ($dashRunning) {
    Write-Host "[OK] Dashboard 已运行 → http://localhost:$Port" -ForegroundColor Green
} else {
    Write-Host "[..] 启动 Dashboard..." -ForegroundColor Yellow
    Start-Process python -ArgumentList "run_dashboard.py" -WorkingDirectory $ProjectDir -WindowStyle Hidden
    # 等待 Dashboard 就绪
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep 1
        try {
            Invoke-WebRequest -Uri "http://localhost:$Port" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop | Out-Null
            break
        } catch {}
    }
    Write-Host "[OK] Dashboard 已启动 → http://localhost:$Port" -ForegroundColor Green
}

# ── 步骤 4：启动实盘引擎（独立窗口，可查看日志）────
Write-Host "[..] 启动 V3 实盘引擎（将在新窗口显示日志）..." -ForegroundColor Yellow
$liveCmd = "cd '$ProjectDir'; python run_live_v3.py"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $liveCmd

Start-Sleep 2

# ── 完成 ──────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "   所有服务已启动！                     " -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  仪表盘（本地）: http://localhost:$Port" -ForegroundColor Cyan
Write-Host "  实盘引擎日志  : 见新打开的 PowerShell 窗口" -ForegroundColor Cyan
Write-Host ""
Write-Host "  提示：关闭此窗口不影响已启动的进程" -ForegroundColor Gray
Write-Host ""
Read-Host "按回车关闭"
