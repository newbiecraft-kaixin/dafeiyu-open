# 把 astrbot_plugins 下 input/output/function 三个子包里的插件
# 打散拷进 AstrBot 的 data/plugins/（AstrBot 只认平铺一级目录）
# 用法: powershell -ExecutionPolicy Bypass -File .\installer\install-plugins.ps1 -AstrBotData "D:\AstrBot\data"
param(
    [Parameter(Mandatory = $true)]
    [string]$AstrBotData
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$src = Join-Path $repo "astrbot_plugins"
$dst = Join-Path $AstrBotData "plugins"

if (-not (Test-Path $src)) { throw "找不到 $src，请在仓库根目录运行" }
New-Item -ItemType Directory -Force -Path $dst | Out-Null

$copied = 0
Get-ChildItem $src -Recurse -Depth 1 -Directory -Filter "astrbot_plugin_*" | Sort-Object Name | ForEach-Object {
    $target = Join-Path $dst $_.Name
    Copy-Item -Recurse -Force -Path $_.FullName -Destination $target
    # 清掉运行残留
    Get-ChildItem $target -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force
    $copied++
}
Write-Host "已安装 $copied 个插件到 $dst" -ForegroundColor Green
Write-Host "下一步：打开 AstrBot WebUI -> 插件管理 -> 重新加载。配置见 docs/PLUGINS.md"
