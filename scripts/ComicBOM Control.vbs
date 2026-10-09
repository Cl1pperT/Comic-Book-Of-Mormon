' ComicBOM Control: opens the start/stop window with no command prompt (double-click this, or use the Desktop shortcut).
Set shell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder & "\.."
shell.Run """" & folder & "\..\.venv\Scripts\pythonw.exe"" -m bom_comic.control", 0, False
