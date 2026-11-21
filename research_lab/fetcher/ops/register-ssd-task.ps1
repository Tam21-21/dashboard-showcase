# Register a logon Scheduled Task that re-attaches the eodhd SSD to WSL2 after any reboot.
# Must be run elevated (it registers a Highest-runlevel task, which only an admin can create).
# Idempotent: removes any prior task of the same name first.
$ErrorActionPreference = 'Stop'
$taskName = 'EodhdSsdRemount'
$script   = 'C:\Users\tamer\ssd-remount.ps1'

if (-not (Test-Path $script)) { Write-Error "remount helper not found: $script"; exit 1 }

# Drop any existing registration so re-running this is safe.
Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false

# Run the remount helper elevated, at this user's logon, hidden.
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
              -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
              -LogonType Interactive -RunLevel Highest
# Let it wait a bit for USB/WSL to settle at logon; don't kill it on a slow attach.
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Description 'Attach the eodhd USB SSD to WSL2 at logon' | Out-Null

$t = Get-ScheduledTask -TaskName $taskName
Write-Output "Registered task '$taskName' state=$($t.State) runLevel=Highest trigger=AtLogon"
