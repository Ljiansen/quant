# start_tunnel.ps1 - 启动 Dashboard + Cloudflare 隧道，打印外网访问地址
# 用法：右键 → 用 PowerShell 运行，或在 IDE 终端执行 .\start_tunnel.ps1

$ProjectDir    = "d:\miniqmt_quant"
$CloudflaredExe = "D:\cloudflared.exe"
$Port           = 8088
$LogFile        = "$env:TEMP\cf_tunnel.log"

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "   V3策略仪表盘  ·  外网隧道启动器   " -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# ── 步骤1：检查 Dashboard 是否在运行 ──────────────
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
    Write-Host "[..] 等待 Dashboard 就绪（最多 15 秒）..." -ForegroundColor Yellow
    $ready = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep 1
        try {
            Invoke-WebRequest -Uri "http://localhost:$Port" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop | Out-Null
            $ready = $true; break
        } catch {}
    }
    if ($ready) {
        Write-Host "[OK] Dashboard 已就绪 → http://localhost:$Port" -ForegroundColor Green
    } else {
        Write-Host "[!!] Dashboard 启动超时，继续建立隧道..." -ForegroundColor Yellow
    }
}

# ── 步骤2：启动 cloudflared 隧道 ──────────────────
if (-not (Test-Path $CloudflaredExe)) {
    Write-Host "[ERROR] 找不到 $CloudflaredExe，请检查路径" -ForegroundColor Red
    Read-Host "按回车退出"; exit 1
}
if (Test-Path $LogFile) { Remove-Item $LogFile -Force }

Write-Host "[..] 正在建立 Cloudflare 隧道，请稍候..." -ForegroundColor Yellow

$proc = Start-Process -FilePath $CloudflaredExe `
    -ArgumentList "tunnel --url http://localhost:$Port" `
    -RedirectStandardError $LogFile `
    -PassThru -WindowStyle Hidden

# ── 步骤3：等待 URL 出现（最多 30 秒）─────────────
$url = ""
$deadline = [DateTime]::Now.AddSeconds(30)
while ([DateTime]::Now -lt $deadline) {
    Start-Sleep 1
    if (Test-Path $LogFile) {
        $content = Get-Content $LogFile -Raw -ErrorAction SilentlyContinue
        if ($content -match "https://[a-z0-9\-]+\.trycloudflare\.com") {
            $url = $Matches[0]; break
        }
    }
}

Write-Host ""
if ($url) {
    Write-Host "+--------------------------------------------------+" -ForegroundColor Cyan
    Write-Host "|       外网访问地址（任意设备、任意网络）         |" -ForegroundColor Cyan
    Write-Host "|                                                  |" -ForegroundColor Cyan
    Write-Host "|  $url" -ForegroundColor Green
    Write-Host "|                                                  |" -ForegroundColor Cyan
    Write-Host "+--------------------------------------------------+" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  本地地址 : http://localhost:$Port" -ForegroundColor Gray
    Write-Host "  隧道 PID : $($proc.Id)" -ForegroundColor Gray
    Write-Host "  !! 关闭此窗口 = 断开隧道 !!" -ForegroundColor DarkYellow
    Write-Host ""
    # 保持脚本运行 = 保持隧道存活
    Wait-Process -Id $proc.Id -ErrorAction SilentlyContinue
} else {
    Write-Host "[ERROR] 30秒内未收到域名，可能原因：" -ForegroundColor Red
    Write-Host "  1. 网络无法访问 Cloudflare（检查科学上网）" -ForegroundColor Red
    Write-Host "  2. D:\cloudflared.exe 文件损坏" -ForegroundColor Red
    $proc | Stop-Process -Force -ErrorAction SilentlyContinue
    Read-Host "按回车退出"
}
