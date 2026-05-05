$pid34200 = 34200
for($i=0; $i -lt 200; $i++){
    Start-Sleep 30
    $pool = Test-Path 'd:\miniqmt_quant\pool_2015_2022.json'
    $p = Get-Process -Id $pid34200 -EA SilentlyContinue
    if($pool){
        if($p){ Stop-Process -Id $pid34200 -Force; Write-Host "[$i] pool JSON found! Killed PID $pid34200" }
        else { Write-Host "[$i] pool JSON found! Process already dead" }
        break
    }
    if(!$p){ Write-Host "[$i] PID $pid34200 DEAD (no pool JSON yet)"; break }
    if($i%2 -eq 0){ Write-Host "[$i] alive  CPU=$($p.CPU.ToString('F0'))s  pool=N" }
}
Write-Host "Monitor loop ended at i=$i"
# Start new process
Write-Host "Starting new process..."
cd 'd:\miniqmt_quant'
Start-Process python -ArgumentList '_overnight_2015_2022.py' -RedirectStandardOutput 'overnight_2015_2022.txt' -RedirectStandardError 'overnight_2015_2022_err.txt' -NoNewWindow
Start-Sleep 3
$newp = Get-Process python | Sort-Object StartTime | Select-Object -Last 1
Write-Host "New PID=$($newp.Id) started"
