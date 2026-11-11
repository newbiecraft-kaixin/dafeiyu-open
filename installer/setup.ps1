# 大肥鱼 一键安装（命令行菜单）
#
# 用法（也可以直接双击仓库根目录的 install.bat）：
#   install.bat                 交互式菜单
#   install.bat -Check          只检测环境与安装状态
#   install.bat -Select all     安装全部
#   install.bat -Select 2,4     安装指定项（见菜单编号）
#   install.bat -Select 1,2 -Yes 不询问确认
#   install.bat -DryRun         只演示流程，不做任何修改

[CmdletBinding()]
param(
    [string]$Select = "",
    [switch]$Check,
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$Core
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Get-RepoRoot {
    # 脚本可能在 installer\（新布局，仓库根就是上一级），也可能在 tools\windows\（老布局，上两级）；
    # 先按「哪一级有 services\hub\service.py」认，认不到再退回老布局的算法。
    $dir = $PSScriptRoot
    for ($i = 0; $i -lt 4 -and $dir; $i++) {
        if (Test-Path (Join-Path $dir "services\hub\service.py")) { return $dir }
        $dir = Split-Path -Parent $dir
    }
    return (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
}

$Repo = Get-RepoRoot
$ThirdParty = Join-Path $Repo "third_party"
# 运行环境先扫本地：实际部署在 bwe_env，老位置 astrbot-win\venv 也兼容
$VenvDir = @((Join-Path $Repo "bwe_env"), (Join-Path $Repo "astrbot-win\venv")) |
    Where-Object { Test-Path (Join-Path $_ "Scripts\python.exe") } |
    Select-Object -First 1
$VenvPy = if ($VenvDir) { Join-Path $VenvDir "Scripts\python.exe" } else { $null }
$LogDir = Join-Path $Repo "logs"
$LogFile = Join-Path $LogDir "install.log"

function Write-Title($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }
function Write-Info($text) { Write-Host "   $text" }
function Write-Ok($text) { Write-Host "   [OK] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "   [!] $text" -ForegroundColor Yellow }
function Write-Err2($text) { Write-Host "   [x] $text" -ForegroundColor Red }

function Write-Log($text) {
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
        $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch { }
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Get-PythonCandidates {
    $list = @()
    if (Test-Command "py") {
        foreach ($v in @("3.13", "3.12", "3.11", "3.10")) {
            $list += [pscustomobject]@{ Label = "py -$v"; Exe = "py"; Pre = @("-$v") }
        }
    }
    foreach ($n in @("python", "python3")) {
        if (Test-Command $n) { $list += [pscustomobject]@{ Label = $n; Exe = $n; Pre = @() } }
    }
    return $list
}

function Get-PythonVersion($cand) {
    try {
        $out = & $cand.Exe @($cand.Pre) -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [string]$out }
    } catch { }
    return ""
}

function Get-BestPython([int]$minMinor = 11) {
    foreach ($cand in (Get-PythonCandidates)) {
        $ver = Get-PythonVersion $cand
        if (-not $ver) { continue }
        $parts = $ver.Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge $minMinor) {
            return [pscustomobject]@{ Label = $cand.Label; Exe = $cand.Exe; Pre = $cand.Pre; Version = $ver }
        }
    }
    return $null
}

function Invoke-Py($py, [string[]]$pyArgs) {
    $all = @()
    if ($py.Pre) { $all += $py.Pre }
    $all += $pyArgs
    & $py.Exe @all
    return $LASTEXITCODE
}

function Test-AstrBotDir($path) {
    if (-not $path) { return $false }
    # 只认 AstrBot / AstrBot-4.29.3 这种本体目录；astrbot_plugin_* 是插件，不能当宿主
    $leaf = Split-Path -Leaf $path
    if ($leaf -notmatch '^(?i)astrbot(-[\d.]+)?$') { return $false }
    return ((Test-Path (Join-Path $path "main.py")) -or (Test-Path (Join-Path $path "astrbot\__init__.py")))
}

function Get-AstrBotDir {
    # 认三种装法：源码/绿色目录（有 main.py 或 astrbot 包）、以及下面 Get-AstrBotCli 的全局工具
    $fixed = Join-Path $ThirdParty "AstrBot"
    if (Test-AstrBotDir $fixed) { return $fixed }
    $hits = @()
    $parents = @(
        $ThirdParty,
        (Join-Path $Repo "astrbot-win"),
        (Join-Path $env:USERPROFILE "dafeiyu\third_party"),
        (Join-Path $env:USERPROFILE "dafeiyu\astrbot-win")
    )
    foreach ($base in $parents) {
        if (-not (Test-Path $base)) { continue }
        $hits += @(Get-ChildItem -Path $base -Directory -Filter "AstrBot*" -ErrorAction SilentlyContinue |
            Where-Object { Test-AstrBotDir $_.FullName })
    }
    if (-not $hits) {
        foreach ($base in @(
            (Join-Path $env:USERPROFILE "dafeiyu"),
            (Join-Path $env:USERPROFILE "workspace"),
            (Join-Path $env:USERPROFILE "Desktop")
        )) {
            if (-not (Test-Path $base)) { continue }
            $hits += @(Get-ChildItem -Path $base -Directory -Filter "AstrBot*" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                Where-Object { Test-AstrBotDir $_.FullName })
        }
    }
    if ($hits) {
        $best = $hits | Sort-Object -Property @{ Expression = {
            $n = [regex]::Matches($_.Name, '\d+') | ForEach-Object { $_.Value.PadLeft(4, '0') }
            if ($n) { $n -join '.' } else { '0' }
        } } -Descending | Select-Object -First 1
        return $best.FullName
    }
    return ""
}

function Get-NapCatDir {
    foreach ($p in @((Join-Path $ThirdParty "NapCat"), (Join-Path $Repo "napcat-win"), (Join-Path $Repo "installer\napcat-win"), (Join-Path $env:USERPROFILE "dafeiyu\napcat-win"))) {
        if (Test-Path (Join-Path $p "napcat.bat")) { return $p }
        if (Test-Path (Join-Path $p "bootmain\napcat.bat")) { return (Join-Path $p "bootmain") }
    }
    if (Test-Path $ThirdParty) {
        $hit = Get-ChildItem -Path $ThirdParty -Recurse -Filter "napcat.bat" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.DirectoryName }
    }
    return ""
}

function Test-CosyVoice {
    # 先扫本地：模型目录必须有；运行环境可能是 cosyvoice_env venv，也可能直接用系统 Python 跑 tools\cosyvoice_tts_server.py
    $models = @(
        (Join-Path $Repo "audio\cosyvoice_models\CosyVoice2-0.5B"),
        (Join-Path $Repo "cosyvoice_models\CosyVoice2-0.5B"),
        (Join-Path $Repo "tools\cosyvoice_models\CosyVoice2-0.5B")
    )
    $modelOk = $false
    foreach ($model in $models) {
        if (Test-Path $model) { $modelOk = $true; break }
    }
    if (-not $modelOk) { return $false }
    $venvs = @(
        (Join-Path $Repo "audio\cosyvoice_env\Scripts\python.exe"),
        (Join-Path $Repo "cosyvoice_env\Scripts\python.exe"),
        (Join-Path $Repo "tools\cosyvoice_env\Scripts\python.exe")
    )
    foreach ($venv in $venvs) {
        if (Test-Path $venv) { return $true }
    }
    # 系统Python运行模式：CosyVoice 源码目录或 TTS 服务脚本在位即视为就绪
    return ((Test-Path (Join-Path $Repo "audio\CosyVoice")) -or
            (Test-Path (Join-Path $Repo "CosyVoice")) -or
            (Test-Path (Join-Path $Repo "tools\cosyvoice_tts_server.py")))
}

function Get-NodePath {
    # 绿色版 node 常常没进 PATH：仓库 tools\node-* → PATH → NapCat 自带，三路兜底
    foreach ($base in @((Join-Path $Repo "tools"), (Join-Path $Repo "third_party"))) {
        if (-not (Test-Path $base)) { continue }
        $dirs = @(Get-ChildItem -Path $base -Directory -Filter "node*" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending)
        foreach ($dir in $dirs) {
            foreach ($rel in @("node.exe", "bin\node.exe", "node\node.exe")) {
                $p = Join-Path $dir.FullName $rel
                if (Test-Path $p) { return $p }
            }
        }
    }
    $cmd = Get-Command "node" -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    $napcat = Get-NapCatDir
    if ($napcat) {
        foreach ($rel in @("node.exe", "node\node.exe", "..\node.exe", "..\..\node.exe")) {
            $p = Join-Path $napcat $rel
            if (Test-Path $p) { return (Resolve-Path $p).Path }
        }
    }
    return ""
}

function Get-AstrBotCli {
    # uv / pip 全局工具装的 AstrBot 没有源码 main.py，只有命令行入口
    $cmd = Get-Command "astrbot" -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    foreach ($p in @(
        (Join-Path $env:USERPROFILE ".local\bin\astrbot.exe"),
        (Join-Path $env:APPDATA "Python\Scripts\astrbot.exe"),
        (Join-Path $env:LOCALAPPDATA "uv\tools\astrbot\Scripts\astrbot.exe")
    )) {
        if (Test-Path $p) { return $p }
    }
    if (Test-Command "uv") {
        try {
            $out = & uv tool list 2>$null
            if ($out -match "(?m)^\s*astrbot\s") { return "uv:astrbot" }
        } catch { }
    }
    return ""
}

function Get-AstrBotDataDir {
    # 插件真正装在数据目录；源码目录、绿色目录、uv 全局工具三种装法的数据目录位置都可能不同
    $candidates = @()
    if ($env:ASTRBOT_ROOT) {
        $candidates += $env:ASTRBOT_ROOT
        $candidates += (Join-Path $env:ASTRBOT_ROOT "data")
    }
    $ab = Get-AstrBotDir
    if ($ab) { $candidates += (Join-Path $ab "data") }
    $candidates += @(
        (Join-Path $env:USERPROFILE ".astrbot"),
        (Join-Path $env:USERPROFILE ".astrbot\data"),
        (Join-Path $env:USERPROFILE "astrbot\data"),
        (Join-Path $env:USERPROFILE "workspace\astrbot\data")
    )
    # 装在别的盘（如 D:\工作区\astrbot\data）：只扫一层子目录 + <子目录>\astrbot\data，避免全盘遍历
    foreach ($base in @((Join-Path $env:USERPROFILE "Desktop"), (Join-Path $env:USERPROFILE "Documents"), "D:\", "E:\")) {
        if (-not (Test-Path $base)) { continue }
        $children = @(Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue | Select-Object -First 40)
        foreach ($child in $children) {
            $candidates += (Join-Path $child.FullName "data")
            $candidates += (Join-Path $child.FullName "astrbot\data")
        }
    }
    # 有 data\plugins 的才算数；多个时取插件最多的那个（最可能是真在用）
    $best = ""
    $bestCount = -1
    foreach ($dir in $candidates) {
        if (-not $dir -or -not (Test-Path (Join-Path $dir "plugins"))) { continue }
        $count = @(Get-ChildItem (Join-Path $dir "plugins") -Directory -Filter "astrbot_plugin_*" -ErrorAction SilentlyContinue).Count
        if ($count -gt $bestCount) { $best = $dir; $bestCount = $count }
    }
    return $best
}

function Test-WeChatAuto {
    # wechatauto 是作者私有包，PyPI 上没有，只能从 GitHub 装；缺了不算故障
    foreach ($py in @($VenvPy, (Get-Command python -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }))) {
        if (-not $py -or -not (Test-Path $py)) { continue }
        try {
            & $py -c "import wechatauto" 2>$null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch { }
    }
    return $false
}

function Test-Pet {
    # 装好的桌宠不一定有 npm（用户可能只装了成品），直接看 node_modules\dsh-pet
    foreach ($p in @(
        (Join-Path $env:USERPROFILE ".dsh\profiles\web\node_modules\dsh-pet"),
        (Join-Path $env:USERPROFILE ".dsh\dsh-pet"),
        (Join-Path $Repo "dsh\node_modules\dsh-pet")
    )) {
        if (Test-Path $p) { return $true }
    }
    return $false
}

function Test-WeChatDeps {
    # 必需：uiautomation（操作微信窗口）。wechatauto 是可选增强，见 Test-WeChatAuto
    $candidates = @()
    if ($VenvPy -and (Test-Path $VenvPy)) { $candidates += $VenvPy }
    $sysPy = Get-Command python -ErrorAction SilentlyContinue
    if ($sysPy) { $candidates += $sysPy.Source }
    foreach ($py in $candidates) {
        try {
            & $py -c "import uiautomation" 2>$null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch { }
    }
    return $false
}

function Get-Status {
    $py = Get-BestPython 11
    $pyVer = ""
    if ($py) { $pyVer = $py.Version }
    $nodePath = Get-NodePath
    $nodeVer = ""
    if ($nodePath) {
        try { $nodeVer = ((& $nodePath -v) -replace "^v", "") } catch { }
        if (-not $nodeVer) { $nodeVer = "已找到" }
    }
    $ab = Get-AstrBotDir
    $cli = ""
    if (-not $ab) { $cli = Get-AstrBotCli }
    $dataDir = Get-AstrBotDataDir
    $plugins = 0
    if ($dataDir) {
        $plugins = @(Get-ChildItem (Join-Path $dataDir "plugins") -Directory -Filter "astrbot_plugin_*" -ErrorAction SilentlyContinue).Count
    }
    if ($plugins -eq 0 -and (Test-Path (Join-Path $Repo "astrbot_plugins"))) {
        $plugins = @(Get-ChildItem (Join-Path $Repo "astrbot_plugins") -Directory -Filter "astrbot_plugin_*" -ErrorAction SilentlyContinue).Count
    }
    return [pscustomobject]@{
        Python = $pyVer
        Node = $nodeVer
        NodePath = $nodePath
        AstrBot = $ab
        AstrBotCli = $cli
        AstrBotData = $dataDir
        Plugins = $plugins
        NapCat = (Get-NapCatDir)
        WeChat = (Test-WeChatDeps)
        WeChatAuto = (Test-WeChatAuto)
        CosyVoice = (Test-CosyVoice)
        Pet = (Test-Pet)
        Venv = ($VenvPy -and (Test-Path $VenvPy))
    }
}

function Show-Status {
    $s = Get-Status
    Write-Title "环境检测"
    if ($s.Python) { Write-Ok "Python $($s.Python)" } else { Write-Warn2 "Python 未安装（需要 3.12+，AstrBot 要求）" }
    if ($s.Node) {
        $from = if ($s.NodePath -and -not (Get-Command "node" -ErrorAction SilentlyContinue)) { "（绿色版，$($s.NodePath)）" } else { "" }
        Write-Ok "Node.js $($s.Node)$from"
    } else { Write-Warn2 "Node.js 未安装（桌宠需要 20+）" }
    if ($s.Venv) { Write-Ok "服务虚拟环境 $(Split-Path -Leaf $VenvDir)" } else { Write-Info "服务虚拟环境未创建（装核心时会建）" }
    if ($s.AstrBot) { Write-Ok "AstrBot：$($s.AstrBot)" }
    elseif ($s.AstrBotCli) { Write-Ok "AstrBot：$($s.AstrBotCli)（uv/pip 全局工具装）" }
    else { Write-Info "AstrBot 未安装" }
    if ($s.AstrBotData) { Write-Info "AstrBot 数据目录：$($s.AstrBotData)" }
    if ($s.Plugins -gt 0) { Write-Ok "大肥鱼插件：$($s.Plugins) 个" } else { Write-Info "插件未安装" }
    if ($s.NapCat) { Write-Ok "NapCat：$($s.NapCat)" } else { Write-Info "NapCat 未安装" }
    if ($s.WeChat) {
        $opt = if ($s.WeChatAuto) { "（含 wechatauto）" } else { "（wechatauto 未装，可选）" }
        Write-Ok "微信桥依赖已就绪$opt"
    } else { Write-Info "微信桥依赖未装（需要 uiautomation）" }
    if ($s.CosyVoice) { Write-Ok "CosyVoice2 已就绪" } else { Write-Info "CosyVoice2 未安装（约 3GB，需要 NVIDIA 显卡）" }
    if ($s.Pet) { Write-Ok "桌宠运行环境已就绪" } else { Write-Info "桌宠未安装（需要先装 DeepSeek Harness）" }
}

function ConvertTo-StepList($raw) {
    $text = $raw.Trim().ToLower()
    if ($text -eq "a" -or $text -eq "all") { return 1..9 }
    $items = @()
    foreach ($piece in ($text -split "[,\s]+")) {
        if (-not $piece) { continue }
        $n = 0
        if ([int]::TryParse($piece, [ref]$n) -and $n -ge 1 -and $n -le 9) { $items += $n }
        else { throw "无法识别的编号：$piece" }
    }
    return ($items | Select-Object -Unique)
}

function Invoke-Step($name, [scriptblock]$action) {
    Write-Title $name
    Write-Log "开始：$name"
    if ($DryRun) {
        Write-Info "[dry-run] 跳过实际操作"
        return $true
    }
    try {
        & $action
        Write-Log "完成：$name"
        return $true
    } catch {
        Write-Err2 "$name 失败：$($_.Exception.Message)"
        Write-Log "失败：$name -> $($_.Exception.Message)"
        return $false
    }
}

function Get-LatestAsset($repoSlug, $pattern) {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repoSlug/releases/latest" `
        -Headers @{ "User-Agent" = "dafeiyu-installer" }
    $asset = $rel.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
    if (-not $asset) { throw "$repoSlug 的最新 Release 里没有匹配 $pattern 的资产" }
    return [pscustomobject]@{ Tag = $rel.tag_name; Name = $asset.name; Url = $asset.browser_download_url }
}

function Save-File($url, $dest) {
    $dir = Split-Path -Parent $dest
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Write-Info "下载 $url"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    Write-Ok ("已保存 " + (Split-Path -Leaf $dest) + "（" + [math]::Round((Get-Item $dest).Length / 1MB, 1) + " MB）")
}

function Expand-ZipTo($zip, $dest) {
    if (Test-Path $dest) { Remove-Item -Path $dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dest -Force
}

function Ensure-Venv([int]$minMinor = 11) {
    if (Test-Path $VenvPy) {
        $ver = [string](& $VenvPy -c "import sys;print('%d.%d' % sys.version_info[:2])")
        $parts = $ver.Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge $minMinor) {
            $script:PipArgs = @()
            return $true
        }
        throw "astrbot-win\venv 里的 Python 是 $ver，低于要求的 3.$minMinor；请安装新版 Python 后删除该目录再重跑"
    }
    # Python 属于前置依赖：遵循 Python 默认存放机制，依赖装进解释器自己的 site-packages（用户级），
    # 不再在工程目录里创建 venv。
    $py = Get-BestPython $minMinor
    if (-not $py) { throw "没有可用的 Python $minMinor+，请先安装：https://www.python.org/downloads/windows/" }
    Write-Info "使用 Python 默认环境 $($py.Label)：$($py.Exe)"
    $script:VenvPy = $py.Exe
    $script:PipArgs = @("--user")
    $installed = (& $VenvPy -m pip list --format=freeze 2>$null | Measure-Object).Count
    Write-Ok "已扫描到现有依赖 $installed 个，已装的会直接复用"
    return $true
}

# ---------------- 安装步骤 ----------------

function Step-Env {
    $minMinor = 11
    if ($steps -contains 3) { $minMinor = 12 }
    $need = Get-BestPython $minMinor
    if (-not $need -and $minMinor -ge 12) {
        Write-Warn2 "没有 3.12+ 的 Python（AstrBot 硬性要求）"
        if (Test-Command "winget") {
            Write-Info "用 winget 安装 Python 3.12 …"
            & winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
            $need = Get-BestPython 12
        }
    }
    if (-not $need) { throw "仍未找到 Python $minMinor+，请手动安装后重试：https://www.python.org/downloads/windows/" }
    Write-Ok "Python $($need.Version)（$($need.Label)）"

    if (-not (Get-NodePath)) {
        Write-Warn2 "未找到 Node.js（桌宠需要 20+；仓库 tools\node-* 未发现，PATH 也没有）"
        if (Test-Command "winget") {
            Write-Info "用 winget 安装 Node.js LTS …"
            & winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-package-agreements --accept-source-agreements
        }
    }
    $nodeExe = Get-NodePath
    if ($nodeExe) { Write-Ok ("Node.js " + ((& $nodeExe -v) -replace "^v", "") + "（$nodeExe）") }
    else { Write-Warn2 "Node.js 仍不可用，桌宠稍后会装不了（不影响核心）" }

    Ensure-Venv | Out-Null
    & $VenvPy -c "import sys;print(sys.version.split()[0])" | ForEach-Object { Write-Ok "虚拟环境 Python $_" }
}

function Step-Core {
    $missing = @()
    foreach ($svc in @("hub", "model", "store", "brain")) {
        if (-not (Test-Path (Join-Path $Repo "services\$svc\service.py"))) { $missing += $svc }
    }
    if ($missing.Count -gt 0) { throw "仓库不完整，缺少服务：$($missing -join ', ')" }

    $envFile = Join-Path $Repo ".env"
    if (-not (Test-Path $envFile)) {
        $key = ""
        if (-not $Yes) {
            Write-Info "需要云端模型密钥（DeepSeek）。直接回车可跳过，之后手写 .env 也行。"
            $key = Read-Host "   DEEPSEEK_API_KEY"
        }
        if ($key) {
            $content = "DEEPSEEK_API_KEY=$key"
            [IO.File]::WriteAllText($envFile, $content + "`n", (New-Object Text.UTF8Encoding($false)))
            Write-Ok "已写入 $envFile（已加入 .gitignore）"
        } else {
            Write-Warn2 "未写入密钥；启动后模型会不可用，可稍后在仓库根目录建 .env"
        }
    } else {
        Write-Ok ".env 已存在，保持不动"
    }
    Write-Ok "核心四服务校验通过（hub 23408 / model 23406 / store 23407 / brain 23402）"
}

function Step-AstrBot {
    Ensure-Venv 12 | Out-Null
    $ab = Get-AstrBotDir
    if ($ab) { Write-Ok "AstrBot 已存在：$ab"; return }

    # 固定到 v4.29.3，与仓库随附的 astrbot-win/AstrBot-4.29.3/data 对齐；下载失败再回退 master
    $tag = "v4.29.3"
    $zip = Join-Path $env:TEMP "dafeiyu-astrbot-$tag.zip"
    try {
        Save-File "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/tags/$tag" $zip
    } catch {
        Write-Warn2 "下载 AstrBot $tag 失败，回退 master：$($_.Exception.Message)"
        Save-File "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/heads/master" $zip
    }
    $tmp = Join-Path $ThirdParty "_astrbot_tmp"
    Expand-ZipTo $zip $tmp
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $inner) { throw "AstrBot 压缩包结构异常" }
    $dest = Join-Path $ThirdParty "AstrBot"
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $inner.FullName $dest
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "AstrBot 源码已就位：$dest"

    $bundled = Join-Path $Repo "astrbot-win\AstrBot-4.29.3\data"
    if (Test-Path $bundled) {
        $data = Join-Path $dest "data"
        New-Item -ItemType Directory -Force -Path $data | Out-Null
        Copy-Item -Path (Join-Path $bundled "*") -Destination $data -Recurse -Force
        Write-Ok "已带入仓库自带的 data（表情包/配置模板）"
    }

    Write-Info "安装 AstrBot 依赖（包较多，请耐心等待）…"
    Push-Location $dest
    try {
        & $VenvPy -m pip install @PipArgs -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "pip install 失败（退出码 $LASTEXITCODE）" }
    } finally { Pop-Location }

    # 大肥鱼插件在 AstrBot 依赖之外还要用到的包；失败只降级，不阻塞安装
    Write-Info "安装插件附加依赖（edge-tts / imageio-ffmpeg / faster-whisper / numpy / requests / jieba）…"
    & $VenvPy -m pip install @PipArgs edge-tts imageio-ffmpeg faster-whisper numpy requests jieba --quiet
    if ($LASTEXITCODE -ne 0) { Write-Warn2 "附加依赖安装失败：语音会退到系统 TTS、字幕与表情识别可能不可用，可稍后重试" }
    Write-Ok "AstrBot 安装完成，启动：installer\start-astrbot-win.bat"
}

function Step-Plugins {
    param([switch]$CoreOnly)
    $ab = Get-AstrBotDir
    if (-not $ab) { throw "先安装 AstrBot（第 3 项）" }
    if (-not $ab.StartsWith($Repo, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Warn2 "AstrBot 来自仓库外：$ab"
        Write-Info "插件会装进该目录的 data\plugins；不想改动它的话，先执行第 3 项在仓库内装一份"
    }
    $src = Join-Path $Repo "astrbot_plugins"
    if (-not (Test-Path $src)) { throw "找不到 astrbot_plugins 目录" }
    $dst = Join-Path $ab "data\plugins"
    New-Item -ItemType Directory -Force -Path $dst | Out-Null

    $count = 0
    $coreNames = @(
        "astrbot_plugin_human_sim",
        "astrbot_plugin_humanizer",
        "astrbot_plugin_group_window",
        "astrbot_plugin_speaker_tag",
        "astrbot_plugin_provider_router",
        "astrbot_plugin_basic_tools",
        "astrbot_plugin_sticker_pack",
        "astrbot_plugin_face_convert",
        "astrbot_plugin_dafeiyu_pet"
    )
    # 仓库分包为 input/output/function 三个子包；部署时打散平铺进 data\plugins
    # （AstrBot 只认 data\plugins\<插件名> 一级目录，不能带包层级）
    $dirs = Get-ChildItem $src -Recurse -Depth 1 -Directory -Filter "astrbot_plugin_*" |
        Sort-Object Name
    if ($CoreOnly) {
        $dirs = $dirs | Where-Object { $coreNames -contains $_.Name }
        Write-Info "核心部署：只安装 $($dirs.Count) 个基础插件"
    }
    $dirs | ForEach-Object {
        $target = Join-Path $dst $_.Name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Copy-Item $_.FullName $target -Recurse
        Get-ChildItem $target -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        $count++
    }
    Write-Ok "已安装 $count 个插件到 $dst"
    Write-Info "打开 AstrBot WebUI -> 插件管理 -> 重新加载；只启用需要的插件"

    # 记忆插件不在白名单里：缺了就明确提示，避免“没有记忆但不报错”
    $memDir = Join-Path $dst "astrbot_plugin_livingmemory"
    if (-not (Test-Path $memDir)) {
        Write-Warn2 "未安装记忆插件 astrbot_plugin_livingmemory：机器人没有长期记忆，但不会报错"
        Write-Info "去 AstrBot WebUI -> 插件市场 安装 livingmemory，装完后重新执行本步骤"
    } else {
        $patch = Join-Path $Repo "tools\patches\astrbot_plugin_livingmemory\memory_reflection.py"
        $target = Join-Path $memDir "core\event_handler_modules\memory_reflection.py"
        if ((Test-Path $patch) -and (Test-Path $target)) {
            Copy-Item $target ($target + ".bak") -Force
            Copy-Item $patch $target -Force
            Write-Ok "已应用记忆“全量裁对半”补丁（原文件备份为 memory_reflection.py.bak）"
        } elseif (-not (Test-Path $patch)) {
            Write-Warn2 "缺少补丁文件 tools\patches\astrbot_plugin_livingmemory\memory_reflection.py，记忆裁剪仍是原版"
        }
    }
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Warn2 "未检测到 Ollama：记忆的向量检索依赖本地 Ollama（11434）"
        Write-Info "安装 https://ollama.com/download 后执行：ollama pull qwen3.5:9b 与 ollama pull nomic-embed-text"
    }
}

function Step-NapCat {
    $nc = Get-NapCatDir
    if ($nc) { Write-Ok "NapCat 已存在：$nc"; return }
    $asset = Get-LatestAsset "NapNeko/NapCatQQ" "NapCat.Shell.Windows.OneKey.zip"
    $zip = Join-Path $env:TEMP ("dafeiyu-" + $asset.Name)
    Save-File $asset.Url $zip
    $dest = Join-Path $ThirdParty "NapCat"
    Expand-ZipTo $zip $dest
    Write-Ok "NapCat $($asset.Tag) 已解压到 $dest"
    $installer = Join-Path $dest "NapCatInstaller.exe"
    if (Test-Path $installer) {
        Write-Warn2 "还需运行一次 $installer，选择你电脑上已装的 QQ 版本"
        if (-not $Yes) {
            $run = Read-Host "   现在打开安装器？(y/N)"
            if ($run -eq "y" -or $run -eq "Y") { Start-Process -FilePath $installer -WorkingDirectory $dest }
        }
    }
    Write-Info "安装完成后用 installer\start-napcat-win.bat 启动"
}

function Step-WeChat {
    Ensure-Venv | Out-Null
    Write-Info "安装 uiautomation（必需）…"
    & $VenvPy -m pip install @PipArgs uiautomation --quiet
    if ($LASTEXITCODE -ne 0) { throw "uiautomation 安装失败" }
    Write-Info "安装 wechatauto（可选增强，上游 wechatauto-replica，PyPI 上没有，只能走 codeload）…"
    & $VenvPy -m pip install @PipArgs "https://codeload.github.com/fanyuantaier/wechatauto-replica/zip/refs/heads/main"
    $autoOk = ($LASTEXITCODE -eq 0)
    & $VenvPy -c "import uiautomation; print('ok')" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "uiautomation 装完但导入失败" }
    if ($autoOk) {
        & $VenvPy -c "import wechatauto" 2>$null
        if ($LASTEXITCODE -ne 0) { $autoOk = $false }
    }
    if ($autoOk) {
        Write-Ok "微信桥依赖就绪（含 wechatauto），启动：services\io-wechat\start-wechat.bat"
    } else {
        Write-Warn2 "uiautomation 已就绪；wechatauto 没装上（可选，不影响基础收发），需要时手动："
        Write-Info "pip install https://codeload.github.com/fanyuantaier/wechatauto-replica/zip/refs/heads/main"
        Write-Info "启动：services\io-wechat\start-wechat.bat"
    }
    Write-Info "需要桌面微信已登录且窗口可见"
}

function Step-CosyVoice {
    if (Test-CosyVoice) { Write-Ok "CosyVoice2 已就绪"; return }
    $gpu = $null
    if (Test-Command "nvidia-smi") {
        try { $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1) } catch { }
    }
    if (-not $gpu) {
        Write-Warn2 "没有检测到 NVIDIA 显卡；CosyVoice2 基本跑不动，安装会白等 3GB 下载"
        if (-not $Yes) {
            $go = Read-Host "   仍要继续？(y/N)"
            if ($go -ne "y" -and $go -ne "Y") { Write-Info "跳过"; return }
        }
    } else {
        Write-Ok "显卡：$gpu"
    }
    $py = Get-BestPython 10
    if (-not $py) { throw "没有可用的 Python（需要 3.10~3.12）" }
    Write-Info "调用 tools\setup_cosyvoice.py（torch + 模型约 3GB，耗时较长）…"
    Push-Location (Join-Path $Repo "tools")
    try {
        $rc = Invoke-Py $py @("setup_cosyvoice.py", "--no-start")
        if ($rc -ne 0) { throw "setup_cosyvoice.py 失败（退出码 $rc）" }
    } finally { Pop-Location }
    Write-Ok "CosyVoice2 安装完成，启动：installer\start-cosyvoice-tts.bat"
}

function Step-Pet {
    $dsh = Join-Path $env:USERPROFILE ".dsh\profiles\web"
    if (-not (Test-Path $dsh)) {
        throw "缺少 DeepSeek Harness 配置目录（$dsh）：请先安装 DSH 并至少启动一次"
    }
    if (-not (Get-NodePath)) {
        throw "找不到 Node.js：可以放到仓库 tools\node-*（绿色版），或装 Node 20+ 后重试"
    }
    $script = Join-Path $Repo "pet\dsh-pet\install.ps1"
    if (-not (Test-Path $script)) { throw "找不到 $script" }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script
    if ($LASTEXITCODE -ne 0) { throw "桌宠安装失败（退出码 $LASTEXITCODE）" }
    Write-Ok "桌宠已装入 DSH，重启 DSH 并在插件列表启用 dsh-pet"
}

function Step-Start {
    $bat = Join-Path $Repo "installer\start-core.bat"
    if (-not (Test-Path $bat)) { throw "找不到 $bat" }
    Start-Process -FilePath $bat -WorkingDirectory $Repo -WindowStyle Minimized
    Start-Sleep -Seconds 5
    Start-Process "http://127.0.0.1:23408"
    Write-Ok "已启动核心并打开控制台 http://127.0.0.1:23408"
}

# ---------------- 菜单 ----------------

$MenuText = @(
    "1) 运行环境（Python 3.12+ / Node 20+ / 虚拟环境）",
    "2) 核心四服务（hub / model / store / brain）",
    "3) AstrBot（插件宿主，自动下载源码与依赖）",
    "4) 大肥鱼插件包（24 个插件）",
    "5) NapCat（QQ 接入，自动下载）",
    "6) 微信桥 io-wechat（依赖 + 说明）",
    "7) 本地语音 CosyVoice2（约 3GB）",
    "8) 桌宠 dsh-pet（需要已装 DeepSeek Harness）",
    "9) 启动核心并打开控制台"
)

function Show-Menu {
    Show-Status
    Write-Title "选择要安装的项目"
    foreach ($line in $MenuText) { Write-Host "   $line" }
    Write-Host ""
    Write-Host "   输入编号，逗号分隔（如 1,2,4）；a=全部；直接回车=推荐(1,2)；q=退出" -ForegroundColor DarkGray
}

if ($Check) {
    Show-Status
    Write-Host ""
    Write-Host "检测完成（-Check 不做任何修改）" -ForegroundColor Cyan
    exit 0
}

$steps = @()
if ($Select) {
    try { $steps = @(ConvertTo-StepList $Select) }
    catch { Write-Err2 $_.Exception.Message; exit 1 }
} else {
    Write-Host ""
    Write-Host "  大肥鱼 一键安装" -ForegroundColor Cyan
    Write-Host "  仓库：$Repo" -ForegroundColor DarkGray
    Show-Menu
    $raw = Read-Host "  选择"
    if ($raw -eq "q" -or $raw -eq "Q") { exit 0 }
    if (-not $raw.Trim()) { $raw = "1,2" }
    try { $steps = @(ConvertTo-StepList $raw) }
    catch { Write-Err2 $_.Exception.Message; exit 1 }
}

if ($Core) {
    # 核心部署：环境 + 核心四服务 + AstrBot + 基础插件 + NapCat + 桌宠；
    # 不含微信桥(6)、CosyVoice(7)，也不启动 RAG/语音类服务
    $steps = @(1, 2, 3, 4, 5, 8)
}

if ($steps -contains 7 -and -not $Yes) {
    Write-Warn2 "CosyVoice2 会下载 torch 与模型（约 3GB），耗时较长"
}
if (-not $Yes -and -not $DryRun) {
    $ok = Read-Host "   开始执行？(Y/n)"
    if ($ok -and $ok -ne "y" -and $ok -ne "Y") { Write-Info "已取消"; exit 0 }
}

$names = @{
    1 = "运行环境"
    2 = "核心四服务"
    3 = "AstrBot"
    4 = "大肥鱼插件包"
    5 = "NapCat"
    6 = "微信桥"
    7 = "CosyVoice2"
    8 = "桌宠 dsh-pet"
    9 = "启动核心"
}
$failed = @()

foreach ($n in $steps) {
    $ok = $false
    switch ($n) {
        1 { $ok = Invoke-Step $names[$n] { Step-Env } }
        2 { $ok = Invoke-Step $names[$n] { Step-Core } }
        3 { $ok = Invoke-Step $names[$n] { Step-AstrBot } }
        4 { $ok = Invoke-Step $names[$n] { Step-Plugins -CoreOnly:$Core } }
        5 { $ok = Invoke-Step $names[$n] { Step-NapCat } }
        6 { $ok = Invoke-Step $names[$n] { Step-WeChat } }
        7 { $ok = Invoke-Step $names[$n] { Step-CosyVoice } }
        8 { $ok = Invoke-Step $names[$n] { Step-Pet } }
        9 { $ok = Invoke-Step $names[$n] { Step-Start } }
    }
    if (-not $ok) { $failed += $names[$n] }
    if ($DryRun) { Write-Info "[dry-run] 后续步骤同理，不实际执行" ; break }
}

Write-Title "结果"
if ($failed.Count -eq 0) {
    Write-Ok "全部完成"
    Write-Host ""
    Write-Host "   下一步：" -ForegroundColor Cyan
    Write-Host "   - 核心控制台：http://127.0.0.1:23408"
    Write-Host "   - 启动 AstrBot：installer\start-astrbot-win.bat（WebUI 默认 http://127.0.0.1:6185）"
    Write-Host "   - 启动 NapCat：installer\start-napcat-win.bat"
    Write-Host "   - 启动桌宠：重启 DSH 并启用 dsh-pet"
    Write-Host "   - 详细步骤：docs\GETTING-STARTED.md"
} else {
    Write-Err2 ("未完成：" + ($failed -join "、"))
    Write-Info "日志：$LogFile"
    exit 1
}
