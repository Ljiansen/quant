$XtResultFile = "$env:TEMP\xt_check_result.txt"
$XtCheckScript = "$env:TEMP\check_xt.py"

@"
try:
    import xtquant.xtdata as d
    r = d.connect()
    status = 'ok' if r is not None else 'fail:None'
except Exception as e:
    status = f'fail:{type(e).__name__}'
open(r'$XtResultFile', 'w').write(status)
"@ | Set-Content $XtCheckScript -Encoding UTF8

Write-Host "=== 生成的 Python 脚本内容 ===" -ForegroundColor Cyan
Get-Content $XtCheckScript

Write-Host ""
Write-Host "=== 执行检测 ===" -ForegroundColor Cyan
if (Test-Path $XtResultFile) { Remove-Item $XtResultFile -Force }
python $XtCheckScript *>$null
$result = (Get-Content $XtResultFile -ErrorAction SilentlyContinue -Raw) -replace '\s',''
Write-Host "结果文件内容: '$result'"
if ($result -eq 'ok') {
    Write-Host "[OK] xtquant 检测通过" -ForegroundColor Green
} else {
    Write-Host "[FAIL] 检测失败: $result" -ForegroundColor Red
}
