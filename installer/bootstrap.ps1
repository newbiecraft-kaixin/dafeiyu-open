# 大肥鱼 一键下载部署（无需先克隆仓库）
#
# 直接在 PowerShell 里执行：
#   irm https://raw.githubusercontent.com/newbiecraft-kaixin/dafeiyu-open/main/installer/bootstrap.ps1 | iex
#
# 可选参数（下载到本地后再运行）：
#   -Dest "D:\FlatFish"   安装目录，默认 C:\Program Files\FlatFish
#   -Full                 连 AstrBot / NapCat / CosyVoice / 桌宠一起装（默认只装核心）
#   -NoLaunch             装完不自动打开管理面板
#
# 已存在旧目录时只更新代码，保留 .env、venv、data、日志与第三方组件。

[CmdletBinding()]
param(
    [string]$Dest = (Join-Path $env:ProgramFiles "FlatFish"),
    [string]$Repo = "newbiecraft-kaixin/dafeiyu-open",
    [string]$Branch = "main",
    [switch]$Full,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# Program Files 需要管理员权限：能自提权就重启，否则给出明确命令
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and $Dest -like "$env:ProgramFiles*") {
    if ($PSCommandPath -and (Test-Path $PSCommandPath)) {
        Write-Host "需要管理员权限，正在请求提权 …" -ForegroundColor Yellow
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Dest", "`"$Dest`"")
        if ($Full) { $argList += "-Full" }
        if ($NoLaunch) { $argList += "-NoLaunch" }
        Start-Process powershell -Verb RunAs -ArgumentList $argList
        exit
    }
    throw "写入 $Dest 需要管理员权限。请用管理员 PowerShell 重新运行本脚本，或在管理面板里点「迁移到 FlatFish」。"
}

function Write-Step($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "   [OK] $text" -ForegroundColor Green }
function Write-Note($text) { Write-Host "   $text" }

Write-Host ""
Write-Host "  大肥鱼 一键部署" -ForegroundColor Cyan
Write-Host "  目标目录：$Dest" -ForegroundColor DarkGray

# 1. 下载仓库压缩包
Write-Step "下载仓库"
$zip = Join-Path $env:TEMP "dafeiyu-open-$Branch.zip"
Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile $zip -UseBasicParsing
Write-Ok ("已下载 " + [math]::Round((Get-Item $zip).Length / 1MB, 1) + " MB")

$tmp = Join-Path $env:TEMP ("dafeiyu-bootstrap-" + [guid]::NewGuid().ToString("N"))
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
if (-not $src) { throw "压缩包结构异常" }

# 2. 落地到目标目录（升级时保留用户数据）
Write-Step "写入目标目录"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$keep = @(
    "astrbot-win", "third_party", "dsh", "napcat-win", "cosyvoice_env",
    "tools\cosyvoice_env", "tools\cosyvoice_models", "logs", "data",
    ".git", ".env"
)
$isUpgrade = Test-Path (Join-Path $Dest "install.bat")
foreach ($item in Get-ChildItem $src -Force) {
    $name = $item.Name
    if ($isUpgrade -and ($keep -contains $name)) { continue }
    $target = Join-Path $Dest $name
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Copy-Item $item.FullName $target -Recurse -Force
}
if ($isUpgrade) {
    Write-Ok "已更新代码（保留 .env / venv / data / 第三方组件）"
} else {
    Write-Ok "已写入完整仓库"
}

# 3. 安装核心（可选全部）
Write-Step "安装服务"
$setup = Join-Path $Dest "installer\setup.ps1"
if (-not (Test-Path $setup)) { throw "找不到 $setup" }
$select = if ($Full) { "all" } else { "1,2" }
& powershell -NoProfile -ExecutionPolicy Bypass -File $setup -Select $select -Yes
if ($LASTEXITCODE -ne 0) { throw "安装步骤失败（退出码 $LASTEXITCODE）" }
Write-Ok "安装完成"

# 4. 打开管理面板（无控制台）
$launcher = Join-Path $Dest "installer\大肥鱼管理面板.vbs"
if (-not $NoLaunch -and (Test-Path $launcher)) {
    Start-Process -FilePath "wscript.exe" -ArgumentList "`"$launcher`""
    Write-Ok "管理面板已打开"
}

Write-Host ""
Write-Host "  完成。以后双击：$launcher" -ForegroundColor Cyan
Write-Host "  中枢控制台：http://127.0.0.1:23408" -ForegroundColor DarkGray

# 清理临时文件
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
