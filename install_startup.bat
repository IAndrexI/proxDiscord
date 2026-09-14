@echo off
setlocal
echo Installing Proxmox Discord RPC to Windows Startup...

set "TARGET_DIR=%~dp0"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\Proxmox-Discord-RPC.lnk"
set "OLD_VBS=%STARTUP_DIR%\Proxmox-Discord-RPC.vbs"

if exist "%OLD_VBS%" del "%OLD_VBS%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%SHORTCUT_PATH%'); $sc.TargetPath = '%TARGET_DIR%.venv\Scripts\pythonw.exe'; $sc.Arguments = '\"%TARGET_DIR%proxmox_rpc.py\"'; $sc.WorkingDirectory = '%TARGET_DIR:~0,-1%'; $sc.Description = 'Proxmox Discord Rich Presence'; $sc.Save()"

if exist "%SHORTCUT_PATH%" (
    echo [SUCCESS] Proxmox Discord RPC added to Windows Startup!
    echo It will now run silently in the background whenever your PC turns on.
) else (
    echo [ERROR] Failed to create startup shortcut.
)
pause
