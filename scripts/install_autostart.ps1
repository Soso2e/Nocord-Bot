param([switch]$StartNow)

$ErrorActionPreference = 'Stop'
$NocordRoot = Split-Path -Parent $PSScriptRoot
$NocordPython = Join-Path $NocordRoot '.venv\Scripts\pythonw.exe'
$NocordRunner = Join-Path $PSScriptRoot 'run_bot.py'
$NocordUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$NocordTaskName = 'NocordBot'

if (-not (Test-Path -LiteralPath $NocordPython)) {
    throw 'Nocord .venv Python is missing. Install dependencies first.'
}
$NocordExisting = Get-ScheduledTask -TaskName $NocordTaskName -ErrorAction SilentlyContinue
if ($NocordExisting -and ($NocordExisting.Actions.Execute -ne $NocordPython -or
    $NocordExisting.Actions.Arguments -ne ('"' + $NocordRunner + '"'))) {
    throw 'An unrelated NocordBot task exists; it was not overwritten.'
}

$NocordAction = New-ScheduledTaskAction -Execute $NocordPython `
    -Argument ('"' + $NocordRunner + '"') -WorkingDirectory $NocordRoot
$NocordTrigger = New-ScheduledTaskTrigger -AtLogOn -User $NocordUser
$NocordPrincipal = New-ScheduledTaskPrincipal -UserId $NocordUser -LogonType Interactive -RunLevel Limited
$NocordSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $NocordTaskName -Action $NocordAction `
    -Trigger $NocordTrigger -Principal $NocordPrincipal -Settings $NocordSettings `
    -Description 'Start local Nocord at user logon; retry failures and prevent duplicate bots.' `
    -Force | Select-Object TaskName, State

if ($StartNow) {
    Start-ScheduledTask -TaskName $NocordTaskName
}
