Set FSO = CreateObject("Scripting.FileSystemObject")
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = ScriptDir
WshShell.Run """" & ScriptDir & "\.venv\Scripts\pythonw.exe"" """ & ScriptDir & "\proxmox_rpc.py""", 0, False
