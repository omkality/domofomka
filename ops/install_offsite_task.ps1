param(
    [Parameter(Mandatory=$true)][string]$HostName,
    [Parameter(Mandatory=$true)][string]$KeyPath,
    [Parameter(Mandatory=$true)][string]$KnownHostsPath
)
$ErrorActionPreference='Stop'
$projectRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python=Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtime=Join-Path $projectRoot '.codex-local\backup-runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$script=Join-Path $runtime 'fetch_dev_backup.py'
$published=& git -C $projectRoot show HEAD:tools/fetch_dev_backup.py
if ($LASTEXITCODE -ne 0) { throw 'Cannot read published backup tool' }
[IO.File]::WriteAllText($script,($published -join "`n"),[Text.UTF8Encoding]::new($false))
$key=(Resolve-Path -LiteralPath $KeyPath).Path
$known=(Resolve-Path -LiteralPath $KnownHostsPath).Path
if ($HostName -notmatch '^[A-Za-z0-9.-]+$') { throw 'Invalid hostname' }
foreach($path in @($python,$script,$key,$known)) {
    if($path.Contains('"')) { throw 'Unexpected quote in path' }
}
$arguments='-X utf8 "'+$script+'" --host '+$HostName+' --key "'+$key+'" --known-hosts "'+$known+'"'
$action=New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $projectRoot
$account=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$triggers=@((New-ScheduledTaskTrigger -Daily -At '07:00'),(New-ScheduledTaskTrigger -AtLogOn -User $account))
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10) -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$principal=New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'DomofomkaDevOffsiteBackup' -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Verified local copy of dev queue: 7 daily and 4 weekly; no notifications' -Force | Out-Null
Get-ScheduledTask -TaskName 'DomofomkaDevOffsiteBackup' | Select-Object TaskName,State
