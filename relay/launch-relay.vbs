' 桌宠 relay 启动脚本：工作目录必须是 pet-relay.js 所在目录（config.json/日志都按相对路径读）
' 路径一律取脚本自身所在目录与系统环境变量，避免写死安装位置
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd /c """ & sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\nodejs\node.exe"" pet-relay.js >> relay.log 2>&1", 0, False
