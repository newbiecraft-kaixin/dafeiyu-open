# 卸载：停止部署目录里的所有服务，然后删除部署目录。
#   默认保留用户数据（config、.env、data、各服务 data、AstrBot data、NapCat 登录配置），
#   -PurgeData 连数据一起删。源码仓库（含 .git）拒绝卸载，避免误删代码。
[CmdletBinding()]
param(
    [string]$Root = "",
    [switch]$PurgeData
)

$ErrorActionPreference = "SilentlyContinue"
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
try { $Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\') }
catch { Write-Host "目录不存在：$Root"; exit 1 }

if (Test-Path (Join-Path $Root ".git")) {
    Write-Host "这是源码仓库（含 .git），拒绝卸载；请手动删除目录。"
    exit 2
}

# 只匹配部署目录本身，避免把 FlatFish-backup 这类同前缀目录里的进程也杀掉
$rootPattern = '(^|[\s"])' + [Regex]::Escape($Root) + '(\\|[\s"]|$)'

Write-Host "等待面板退出 …"
Start-Sleep -Seconds 2

Write-Host "停止占用部署目录的进程 …"
Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match $rootPattern
} | ForEach-Object {
    Write-Host ("  停止 {0} (PID {1})" -f $_.Name, $_.ProcessId)
    # /T 连子进程一起收掉（NapCat 的 node 子进程、各服务的 Python 子进程）
    & taskkill.exe /F /T /PID $_.ProcessId 2>$null | Out-Null
}
Start-Sleep -Seconds 1

$keep = @()
if (-not $PurgeData) {
    # 用户数据：配置、模型密钥、运行时数据、QQ 登录态与各服务数据库
    foreach ($pattern in @(
        "config",
        ".env",
        "data",
        "napcat-win\config.json",
        "napcat-win\napcat\config",
        "services\*\data",
        "astrbot-win\AstrBot-*\data"
    )) {
        $keep += Get-ChildItem -Path (Join-Path $Root $pattern) -Force -ErrorAction SilentlyContinue
    }
}

$tmp = Join-Path $env:TEMP ("dafeiyu-keep-" + [Guid]::NewGuid().ToString("N"))
$saved = @()
if ($keep.Count -gt 0) {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $i = 0
    try {
        foreach ($item in $keep) {
            $rel = $item.FullName.Substring($Root.Length).TrimStart('\')
            $dest = Join-Path $tmp $i
            Copy-Item -LiteralPath $item.FullName -Destination $dest -Recurse -Force -ErrorAction Stop
            $saved += [pscustomobject]@{ Rel = $rel; Dest = $dest }
            $i++
        }
    } catch {
        # 备份失败就不删任何东西，避免"说保留其实丢了"
        Write-Host ("备份用户数据失败，已中止卸载（未删除文件）：{0}" -f $_.Exception.Message)
        Write-Host "已备份部分保留在：$tmp"
        exit 4
    }
}

Remove-Item -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue
$left = Test-Path $Root

$failed = @()
foreach ($s in $saved) {
    try {
        $target = Join-Path $Root $s.Rel
        New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) -ErrorAction Stop | Out-Null
        if (Test-Path -LiteralPath $s.Dest -PathType Container) {
            # 先建好目标目录，否则 Copy-Item 会把目录名当成文件名
            New-Item -ItemType Directory -Force -Path $target -ErrorAction Stop | Out-Null
            Copy-Item -Path (Join-Path $s.Dest '*') -Destination $target -Recurse -Force -ErrorAction Stop
        } else {
            Copy-Item -LiteralPath $s.Dest -Destination $target -Force -ErrorAction Stop
        }
    } catch {
        $failed += $s.Rel
    }
}

if ($failed.Count -gt 0) {
    Write-Host ("用户数据未能放回：{0}" -f ($failed -join '、'))
    Write-Host "备份仍在：$tmp（请手动拷回后再删除）"
    exit 5
}
if ($saved.Count -gt 0) {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($left) {
    Write-Host "删除未完成，可能有进程或文件仍被占用：$Root"
    if ($saved.Count -gt 0) { Write-Host "用户数据已放回原位置" }
    exit 3
}

if ($saved.Count -gt 0) {
    Write-Host "已卸载（保留 config、.env、data、各服务 data、AstrBot data、NapCat 登录配置）：$Root"
} else {
    Write-Host "已卸载：$Root"
}
exit 0
