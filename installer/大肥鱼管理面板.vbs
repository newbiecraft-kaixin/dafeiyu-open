' DaFeiYu service manager - silent launcher (no console window).
' Picks the first Python that can import tkinter, then starts manager.py.
' Keep this file ASCII-only: VBScript reads scripts as ANSI.
Option Explicit

Dim fso, sh, here, root, script, pick, i, rc, cands(7)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(fso.GetParentFolderName(here))
script = fso.BuildPath(here, "manager.py")

' Python 是前置依赖：优先系统默认 Python（依赖在它自己的 site-packages），
' 旧部署的 venv 只作为兜底。
cands(0) = fso.BuildPath(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%"), "Microsoft\WindowsApps\pythonw.exe")
cands(1) = "pythonw.exe"
cands(2) = "pyw.exe"
cands(3) = fso.BuildPath(root, "astrbot-win\venv\Scripts\pythonw.exe")
cands(4) = fso.BuildPath(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%"), "Microsoft\WindowsApps\python.exe")
cands(5) = "python.exe"
cands(6) = fso.BuildPath(root, "astrbot-win\venv\Scripts\python.exe")

pick = ""
For i = 0 To UBound(cands)
    If cands(i) <> "" Then
        If fso.FileExists(cands(i)) Or InStr(cands(i), "\") = 0 Then
            rc = -1
            On Error Resume Next
            rc = sh.Run("""" & cands(i) & """ -c ""import tkinter""", 0, True)
            If Err.Number <> 0 Then
                rc = -1
                Err.Clear
            End If
            On Error GoTo 0
            If rc = 0 Then
                pick = cands(i)
                Exit For
            End If
        End If
    End If
Next

If pick = "" Then
    MsgBox "Python with tkinter was not found." & vbCrLf & vbCrLf & _
           "Install Python 3.12+ from python.org (tkinter is included by default)," & vbCrLf & _
           "then double-click this file again.", 16, "DaFeiYu Manager"
    WScript.Quit 1
End If

sh.CurrentDirectory = root
' 1 = show the app window; pythonw has no console, so nothing black flashes.
sh.Run """" & pick & """ """ & script & """", 1, False
