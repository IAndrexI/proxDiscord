@echo off
setlocal
echo Installing Proxmox Discord RPC to Windows Auto-Run and Background Services...

set "TARGET_DIR=%~dp0"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\Proxmox-Discord-RPC.lnk"
set "OLD_VBS=%STARTUP_DIR%\Proxmox-Discord-RPC.vbs"

if exist "%OLD_VBS%" del "%OLD_VBS%"

rem 1. Windows Startup Shortcut
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%SHORTCUT_PATH%'); $sc.TargetPath = '%TARGET_DIR%.venv\Scripts\pythonw.exe'; $sc.Arguments = '\"%TARGET_DIR%proxmox_rpc.py\"'; $sc.WorkingDirectory = '%TARGET_DIR:~0,-1%'; $sc.Description = 'Proxmox Discord Rich Presence'; $sc.Save()"

rem 2. Registry Run Key
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProxmoxDiscordRPC" /t REG_SZ /d "\"%TARGET_DIR%.venv\Scripts\pythonw.exe\" \"%TARGET_DIR%proxmox_rpc.py\"" /f >nul 2>&1

rem 3. Windows Task Scheduler
powershell -NoProfile -ExecutionPolicy Bypass -Command "$action = New-ScheduledTaskAction -Execute '%TARGET_DIR%.venv\Scripts\pythonw.exe' -Argument '%TARGET_DIR%proxmox_rpc.py' -WorkingDirectory '%TARGET_DIR:~0,-1%'; $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1); Register-ScheduledTask -TaskName 'ProxmoxDiscordRPC' -Action $action -Trigger $trigger -Settings $settings -Description 'Proxmox Discord Rich Presence background service' -Force" >nul 2>&1

rem 4. Start silently now if not already running
start "" "%TARGET_DIR%.venv\Scripts\pythonw.exe" "%TARGET_DIR%proxmox_rpc.py"

echo [SUCCESS] Proxmox Discord RPC installed to auto-run in the background!
echo It will now run silently and automatically without any terminal window.
pause
