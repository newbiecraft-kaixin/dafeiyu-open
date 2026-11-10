' Ollama 启动脚本（脱离终端，重启/开机自启都可用；与 launch-relay.vbs 同一套模式）
' 8GB 显存扛不住多模型同时驻留：一次只留一个模型，KV 缓存压缩，5 分钟没用就卸载
Set sh = CreateObject("WScript.Shell")
sh.Environment("PROCESS")("OLLAMA_MAX_LOADED_MODELS") = "1"
sh.Environment("PROCESS")("OLLAMA_KEEP_ALIVE") = "5m"
sh.Environment("PROCESS")("OLLAMA_FLASH_ATTENTION") = "1"
sh.Environment("PROCESS")("OLLAMA_KV_CACHE_TYPE") = "q8_0"
sh.Environment("PROCESS")("OLLAMA_MODELS") = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.ollama\models"
sh.Run "cmd /c """ & sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Ollama\ollama.exe"" serve", 0, False
