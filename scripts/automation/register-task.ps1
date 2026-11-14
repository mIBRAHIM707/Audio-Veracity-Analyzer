param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [string]$TaskName = 'AVA-Daily-Development',
    [switch]$Remove,
    [switch]$Inspect
)
$ErrorActionPreference = 'Stop'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    exit
}
if ($Inspect) {
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State,Actions,Triggers,Settings
    Get-ScheduledTaskInfo -TaskName $TaskName
    exit
}
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$config = Get-Content -LiteralPath $resolvedConfig -Raw | ConvertFrom-Json
$runner = Join-Path $config.worktree 'scripts\automation\daily_runner.py'
if (!(Test-Path -LiteralPath $config.python_path -PathType Leaf) -or !(Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'Python or runner missing'
}
if ($config.branch -ne 'feat/4-daily-development') { throw 'Unexpected automation branch' }
& $config.python_path $runner --config $resolvedConfig --dry-run
if ($LASTEXITCODE -ne 0) { throw 'Runner dry-run failed; task was not registered' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists. Inspect it and explicitly remove before replacing.'
}
# UTC timestamp fixes the schedule to 07:00 UTC = noon Pakistan, independent of host timezone.
# StartWhenAvailable lets a late wake still start; the runner itself refuses to launch
# outside its window and kills the session at its deadline, so late starts stay in-window.
# The window starts at noon and lasts config `window_hours` (2 to 10, default 2). The task's
# own limit (PT11H) is only a backstop above the longest allowed window.
$utcNow = [DateTime]::UtcNow
$start = $utcNow.Date.AddHours(7)
if ($start -le $utcNow) { $start = $start.AddDays(1) }
$boundary = $start.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$escape = { param($s) [Security.SecurityElement]::Escape([string]$s) }
$arguments = '"' + $runner + '" --config "' + $resolvedConfig + '"'
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AVA daily development, from noon Pakistan for the configured window; dedicated PR, never merge.</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$(& $escape $user)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT11H</ExecutionTimeLimit><Priority>7</Priority></Settings>
  <Actions Context="Author"><Exec><Command>$(& $escape $config.python_path)</Command><Arguments>$(& $escape $arguments)</Arguments><WorkingDirectory>$(& $escape $config.worktree)</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskName $TaskName -Xml $xml | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $TaskName
