@echo off
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Proxmox-Discord-RPC.vbs"
if exist "%STARTUP_VBS%" (
    del "%STARTUP_VBS%"
    echo [SUCCESS] Removed Proxmox Discord RPC from Windows Startup.
) else (
    echo [INFO] Startup file was not found.
)
pause
