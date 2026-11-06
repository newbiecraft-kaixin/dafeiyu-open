# 把 Docker 版 AstrBot 的数据迁移到 AstrBot Launcher 的实例目录。
# 用法：powershell -File migrate-to-native.ps1 -InstanceDir "C:\path\to\.astrbot_launcher\instances\<实例名>\data"

param(
  [Parameter(Mandatory = $true)][string]$InstanceDir,
  [string]$SourceDir = 'C:\path\to\dafeiyu\stack\data'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $InstanceDir)) { throw "实例数据目录不存在: $InstanceDir" }
if (-not (Test-Path $SourceDir)) { throw "Docker 数据目录不存在: $SourceDir" }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $InstanceDir "_backup_before_migrate_$stamp"
New-Item -ItemType Directory -Force -Path $backup | Out-Null

# 需要迁移的文件与目录（相对 data/）
$items = @(
  'cmd_config.json',
  'data_v4.db',
  'plugins',
  'stickers',
  'plugin_data',
  'mcp_server.json'
)

foreach ($item in $items) {
  $src = Join-Path $SourceDir $item
  if (-not (Test-Path $src)) { Write-Host "跳过（不存在）: $item"; continue }

  $dst = Join-Path $InstanceDir $item
  if (Test-Path $dst) {
    Move-Item $dst (Join-Path $backup $item) -Force
    Write-Host "旧文件已备份: $item -> $backup"
  }

  if ((Get-Item $src).PSIsContainer) {
    Copy-Item $src $dst -Recurse -Force
  } else {
    Copy-Item $src $dst -Force
  }
  Write-Host "已迁移: $item"
}

# 同步修正：容器内的路径在 Windows 上不存在，改成绝对路径
$cfgPath = Join-Path $InstanceDir 'cmd_config.json'
if (Test-Path $cfgPath) {
  $raw = Get-Content $cfgPath -Raw -Encoding UTF8
  $raw = $raw.Replace('/AstrBot/data/stickers', ($InstanceDir -replace '\\', '/') + '/stickers')
  $raw = $raw.Replace('http://host.docker.internal:11434', 'http://127.0.0.1:11434')
  [IO.File]::WriteAllText($cfgPath, $raw, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host '已修正配置中的容器路径与 ollama 地址'
}

# 插件配置里的路径也要改
foreach ($plugin in @('astrbot_plugin_sticker_pack', 'astrbot_plugin_care_ping')) {
  $p = Join-Path $InstanceDir "plugins\$plugin\config.json"
  if (Test-Path $p) {
    $raw = Get-Content $p -Raw -Encoding UTF8
    $raw = $raw.Replace('/AstrBot/data/stickers', ($InstanceDir -replace '\\', '/') + '/stickers')
    [IO.File]::WriteAllText($p, $raw, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "已修正插件路径: $plugin"
  }
}

Write-Host "`n迁移完成。备份目录: $backup"
