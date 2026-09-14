@echo off
cd /d "%~dp0"
echo Starting Proxmox Discord Rich Presence...
call .venv\Scripts\activate.bat
python -u proxmox_rpc.py
pause
