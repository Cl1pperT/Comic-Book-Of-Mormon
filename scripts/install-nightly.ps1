# Registers the "ComicBOM Nightly" task: every day at midnight, as the current user, while logged on.
# Re-run to update it. Remove with: Unregister-ScheduledTask -TaskName "ComicBOM Nightly"
$script = Join-Path $PSScriptRoot "nightly.cmd"
$action = New-ScheduledTaskAction -Execute $script -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
# WakeToRun brings the PC out of sleep at midnight (Windows' "Allow wake timers" power setting must be on).
# No StartWhenAvailable: a missed midnight shouldn't turn into a daytime render.
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ComicBOM Nightly" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Write Book of Mormon comic scenes ahead with Codex, then render chapters with ComfyUI until 7am." -Force
