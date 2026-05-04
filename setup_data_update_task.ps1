# setup_data_update_task.ps1
# Register scheduled task: update D:\daily_data every weekday at 15:35
# Run as Administrator

$TaskName    = "Quant_DailyDataUpdate"
$PythonExe   = "C:\Users\41898\AppData\Local\Programs\Python\Python312\python.exe"
$ScriptPath  = "D:\miniqmt_quant\update_daily_data.py"
$WorkDir     = "D:\miniqmt_quant"
$TriggerTime = "15:35"

Write-Host "=== Registering Daily Data Update Task ===" -ForegroundColor Cyan

# Remove old task if exists
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Old task removed." -ForegroundColor Yellow
}

# Create action
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $WorkDir

# Trigger: Mon-Fri at 15:35
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $TriggerTime

# Settings
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

# Register
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action   $Action `
    -Trigger  $Trigger `
    -Settings $Settings `
    -Description "Update D:\daily_data local market data every trading day at 15:35" `
    -Force

if ($?) {
    Write-Host ""
    Write-Host "Task registered successfully!" -ForegroundColor Green
    Write-Host "  Task Name : $TaskName"
    Write-Host "  Trigger   : Mon-Fri $TriggerTime"
    Write-Host "  Python    : $PythonExe"
    Write-Host "  Script    : $ScriptPath"
    Write-Host ""
    Write-Host "Tip: View/run manually in Task Scheduler -> Task Scheduler Library" -ForegroundColor Gray
} else {
    Write-Host "Registration failed. Please run as Administrator." -ForegroundColor Red
}
