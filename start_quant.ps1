# start_quant.ps1 - 量化一键启动脚本
# 启动顺序：Dashboard → Cloudflare隧道 → miniQMT → V3实盘引擎
# 用法：右键 → 用 PowerShell 运行，或终端执行 .\start_quant.ps1

# ══════════════════════════════════════════════
#  配置区（路径有变动时只改这里）
# ══════════════════════════════════════════════
$ProjectDir     = "d:\miniqmt_quant"
$MiniQMTExe     = "D:\迅投QMT交易终端浙商证券金桥版\bin.x64\XtMiniQmt.exe"
$CloudflaredExe = "D:\cloudflared.exe"
$DashPort       = 8088
$CfLogFile      = "$env:TEMP\cf_quant.log"
$XtCheckScript  = "$env:TEMP\check_xt.py"

# ══════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════
function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "  [$n/4] $msg" -ForegroundColor Cyan
    Write-Host "  $(('-' * 44))" -ForegroundColor DarkGray
}
function Write-OK($msg)   { Write-Host "        [OK] $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "        [..] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "        [!!] $msg" -ForegroundColor Red }

# 按命令行关键词终止 Python 进程（防止重复实例）
function Stop-PythonScript($scriptName) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*$scriptName*" }
    foreach ($p in $procs) {
        Write-Info "终止旧进程: $scriptName (PID=$($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($procs) { Start-Sleep 1 }
}

# ══════════════════════════════════════════════
#  标题
# ══════════════════════════════════════════════
Clear-Host
Write-Host ""
Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║     V3 量化策略  ·  一键启动脚本        ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ══════════════════════════════════════════════
#  前置清理：终止所有可能残留的旧进程
#  （miniQMT 除外：保持登录状态）
# ══════════════════════════════════════════════
Write-Host "  [0] 清理残留旧进程..." -ForegroundColor DarkGray
Write-Host "  $(('-' * 44))" -ForegroundColor DarkGray

# 清理旧 Dashboard
Stop-PythonScript "run_dashboard"

# 清理旧 watchdog
Stop-PythonScript "watchdog_dashboard"

# 清理旧实盘引擎（最重要：防止双实例同时下单）
Stop-PythonScript "run_live_v3"

# 清理旧 cloudflared 进程（防止多个隧道并存）
$oldCf = Get-Process "cloudflared" -ErrorAction SilentlyContinue
if ($oldCf) {
    Write-Info "终止旧 cloudflared 进程 (PID=$($oldCf.Id -join ','))"
    $oldCf | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 1
}

# 清理旧 miniQMT（确保窗口能正常弹出，避免单例冲突）
$oldQmt = Get-Process -Name "XtMiniQmt" -ErrorAction SilentlyContinue
if (-not $oldQmt) {
    # 用更宽泛的方式再搜一次（防进程名变体）
    $oldQmt = Get-CimInstance Win32_Process -Filter "Name='XtMiniQmt.exe'" -ErrorAction SilentlyContinue |
              ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
}
if ($oldQmt) {
    Write-Info "终止旧 miniQMT 进程 (PID=$($oldQmt.Id -join ','))，稍后将重新启动"
    $oldQmt | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 2  # 等待进程完全退出，释放单例锁
}

Write-Host "        [OK] 清理完成" -ForegroundColor DarkGray

# ══════════════════════════════════════════════
#  1. 启动 Dashboard
# ══════════════════════════════════════════════
Write-Step 1 "启动 Dashboard"

Write-Info "启动 run_dashboard.py..."
Start-Process python -ArgumentList "run_dashboard.py" -WorkingDirectory $ProjectDir -WindowStyle Hidden

Write-Info "等待 Dashboard 就绪（最多 15 秒）..."
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep 1
    try {
        Invoke-WebRequest -Uri "http://localhost:$DashPort" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop | Out-Null
        $ready = $true; break
    } catch {}
}
if ($ready) { Write-OK "Dashboard 已就绪 → http://localhost:$DashPort" }
else        { Write-Err "Dashboard 启动超时，继续下一步..." }

Write-Info "启动 Dashboard 守护进程..."
Start-Process python -ArgumentList "watchdog_dashboard.py" -WorkingDirectory $ProjectDir -WindowStyle Hidden
Write-OK "watchdog_dashboard 已后台启动（崩溃自动拉起 + 钉钉告警）"

# ══════════════════════════════════════════════
#  2. 启动 Cloudflare 隧道
# ══════════════════════════════════════════════
Write-Step 2 "建立 Cloudflare 外网隧道"

$tunnelUrl = ""
$cfProc    = $null

if (-not (Test-Path $CloudflaredExe)) {
    Write-Err "找不到 $CloudflaredExe，跳过隧道步骤"
} else {
    if (Test-Path $CfLogFile) { Remove-Item $CfLogFile -Force }
    Write-Info "正在连接 Cloudflare，请稍候..."

    $cfProc = Start-Process -FilePath $CloudflaredExe `
        -ArgumentList "tunnel --url http://localhost:$DashPort" `
        -RedirectStandardError $CfLogFile `
        -PassThru -WindowStyle Hidden

    $deadline = [DateTime]::Now.AddSeconds(35)
    while ([DateTime]::Now -lt $deadline) {
        Start-Sleep 1
        if (Test-Path $CfLogFile) {
            $content = Get-Content $CfLogFile -Raw -ErrorAction SilentlyContinue
            if ($content -match "https://[a-z0-9\-]+\.trycloudflare\.com") {
                $tunnelUrl = $Matches[0]; break
            }
        }
    }

    if ($tunnelUrl) {
        Write-Host ""
        Write-Host "  ┌─────────────────────────────────────────────┐" -ForegroundColor Green
        Write-Host "  │  外网访问地址（任意设备可访问）              │" -ForegroundColor Green
        Write-Host "  │                                             │" -ForegroundColor Green
        Write-Host "  │  $tunnelUrl" -ForegroundColor White
        Write-Host "  │                                             │" -ForegroundColor Green
        Write-Host "  └─────────────────────────────────────────────┘" -ForegroundColor Green
    } else {
        Write-Err "35秒内未收到域名（检查网络/科学上网）"
        Write-Info "跳过隧道，继续启动交易程序..."
    }
}

# ══════════════════════════════════════════════
#  3. 启动 miniQMT
# ══════════════════════════════════════════════
Write-Step 3 "启动 miniQMT 交易客户端"

# 清理后一定需要重新启动（旧实例已在 [0] 中清理）
$qmtProc = Get-Process "XtMiniQmt" -ErrorAction SilentlyContinue
if ($qmtProc) {
    # 极少数情况：清理步骤未能终止（如权限问题），直接复用
    Write-OK "miniQMT 已在运行 (PID=$($qmtProc.Id))，跳过重启"
} else {
    if (-not (Test-Path $MiniQMTExe)) {
        Write-Err "找不到 miniQMT：$MiniQMTExe"
        if ($cfProc -and -not $cfProc.HasExited) { $cfProc | Stop-Process -Force }
        Read-Host "  按回车退出"; exit 1
    }
    Write-Info "正在启动 miniQMT..."
    Start-Process -FilePath $MiniQMTExe
    Write-Host ""
    Write-Host "  ┌─────────────────────────────────────────────┐" -ForegroundColor Yellow
    Write-Host "  │  请在弹出的 miniQMT 窗口中完成账号登录      │" -ForegroundColor Yellow
    Write-Host "  │  登录成功后，回到此窗口按回车继续           │" -ForegroundColor Yellow
    Write-Host "  └─────────────────────────────────────────────┘" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "        [ 登录完成后按回车 ]"
}

# 检测 xtquant 连接
Write-Info "检测 xtquant 服务连接..."
@"
try:
    import xtquant.xtdata as d
    r = d.connect()
    print('ok' if r == 0 else 'fail')
except Exception as e:
    print('fail')
"@ | Set-Content $XtCheckScript -Encoding UTF8

$xtReady  = $false
$deadline = [DateTime]::Now.AddSeconds(60)
while ([DateTime]::Now -lt $deadline) {
    $r = python $XtCheckScript 2>$null
    if ($r -eq 'ok') { $xtReady = $true; break }
    Write-Host -NoNewline "." -ForegroundColor DarkGray
    Start-Sleep 2
}
Write-Host ""

if ($xtReady) {
    Write-OK "xtquant 服务已就绪"
} else {
    Write-Err "xtquant 连接超时（miniQMT 是否已登录？）"
    $ans = Read-Host "        仍然继续启动交易引擎？(y/N)"
    if ($ans.ToLower() -ne 'y') {
        if ($cfProc -and -not $cfProc.HasExited) { $cfProc | Stop-Process -Force }
        exit 1
    }
}

# ══════════════════════════════════════════════
#  4. 启动 V3 实盘引擎
# ══════════════════════════════════════════════
Write-Step 4 "启动 V3 实盘交易引擎"

Write-Info "在新窗口中启动 run_live_v3.py..."
$liveCmd = "cd '$ProjectDir'; Write-Host '=== V3 实盘引擎 ===' -ForegroundColor Cyan; python run_live_v3.py"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $liveCmd
Start-Sleep 2
Write-OK "实盘引擎已在独立窗口中启动"

# ══════════════════════════════════════════════
#  启动完成汇总
# ══════════════════════════════════════════════
Write-Host ""
Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║            所有服务已启动！              ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  本地仪表盘 : http://localhost:$DashPort" -ForegroundColor Cyan
if ($tunnelUrl) {
    Write-Host "  外网仪表盘 : $tunnelUrl" -ForegroundColor Cyan
}
Write-Host "  实盘引擎   : 见独立的 PowerShell 窗口" -ForegroundColor Cyan
Write-Host ""
Write-Host "  ⚠  保持此窗口不关闭，关闭 = Cloudflare 隧道断开" -ForegroundColor DarkYellow
Write-Host ""

# 保持脚本存活（= 保持 Cloudflare 隧道）
if ($cfProc -and -not $cfProc.HasExited) {
    Write-Host "  [ 按 Ctrl+C 或关闭窗口可停止隧道 ]" -ForegroundColor DarkGray
    Wait-Process -Id $cfProc.Id -ErrorAction SilentlyContinue
} else {
    Read-Host "  按回车关闭"
}
