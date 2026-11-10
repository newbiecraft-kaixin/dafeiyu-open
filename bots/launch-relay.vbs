' 桌宠 relay 启动脚本：工作目录必须是 pet-relay.js 所在目录（config.json/日志都按相对路径读）
' 注意：bots\ 下有一份旧版 pet-relay.js（maxPending=1），已改名为 *.legacy-20261108，别再指回去
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Program Files\FlatFish\relay"
sh.Run "cmd /c ""C:\Program Files\nodejs\node.exe"" pet-relay.js >> relay.log 2>&1", 0, False
