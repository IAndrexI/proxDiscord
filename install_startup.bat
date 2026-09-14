@echo off
setlocal
echo Installing Proxmox Discord RPC to Windows Startup...

set "TARGET_DIR=%~dp0"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Proxmox-Discord-RPC.vbs"

(
echo Set WshShell = CreateObject^("WScript.Shell"^)
echo WshShell.Run "cmd /c """"%TARGET_DIR%run.bat""""", 0, False
) > "%STARTUP_VBS%"

if exist "%STARTUP_VBS%" (
    echo [SUCCESS] Proxmox Discord RPC added to Windows Startup!
    echo It will now start automatically whenever your PC turns on.
) else (
    echo [ERROR] Could not create startup file.
)
pause
