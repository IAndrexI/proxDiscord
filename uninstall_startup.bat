@echo off
setlocal
echo Removing Proxmox Discord RPC from Windows Startup...

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\Proxmox-Discord-RPC.lnk"
set "OLD_VBS=%STARTUP_DIR%\Proxmox-Discord-RPC.vbs"

if exist "%SHORTCUT_PATH%" del "%SHORTCUT_PATH%"
if exist "%OLD_VBS%" del "%OLD_VBS%"

echo [SUCCESS] Removed Proxmox Discord RPC from Windows Startup.
pause
