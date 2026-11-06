Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\path\to\dafeiyu\bots"
sh.Run "cmd /c ""C:\Program Files\nodejs\node.exe"" pet-relay.js >> relay.log 2>&1", 0, False
