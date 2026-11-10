' DSH 启动器：路径全部相对本文件（FlatFish\dsh），迁移后不再依赖旧目录
Option Explicit
Dim fso, sh, here, cli, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
cli = fso.BuildPath(here, "node_modules\@deepseek-ai\dsh\lib\bin.js")
cmd = fso.BuildPath(here, "node_modules\.bin\dsh.cmd")

sh.Environment("PROCESS")("DSH_DAFEIYU_PYTHON") = "%USERPROFILE%\Documents\deepseek-harness\default-workspace\.plugin-src\pyside_env\Scripts\python.exe"
sh.Environment("PROCESS")("DSH_CLI_JS") = cli
sh.Environment("PROCESS")("DSH_PROVIDER") = "deepseek-account"
sh.Environment("PROCESS")("DSH_MODEL") = "deepseek-flash"
sh.Run "C:\Windows\System32\cmd.exe /c """ & cmd & """ --profile web --no-open", 0, False
