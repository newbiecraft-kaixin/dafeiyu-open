# 安装本机同款桌宠：dsh-pet 0.4.1
# 说明：这台电脑上实际运行的桌宠是 dsh-pet（DeepSeek Harness 的桌宠插件），
#       不是 clawd-on-desk。本脚本把仓库内自带的 0.4.1 安装包装进 DSH 的 web profile。
#
# 只想修「海草 + 背景泡泡」这一层（不重装桌宠）：
#   .\install.ps1 -Backdrop check   查看当前配置值
#   .\install.ps1 -Backdrop on      强制打开
#   .\install.ps1 -Backdrop off     关闭
param(
    [ValidateSet("check", "on", "off")]
    [string]$Backdrop = ""
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgDir = Join-Path $env:USERPROFILE ".dsh\dsh-pet"
$cfg = Join-Path $cfgDir "main-config.jsonc"
$profileDir = Join-Path $env:USERPROFILE ".dsh\profiles\web"

function Get-KeyValue([string]$text, [string]$key) {
    $m = [regex]::Match($text, '(?m)^\s*"' + [regex]::Escape($key) + '"\s*:\s*(-?\d+|true|false|"[^"]*")')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

function Set-KeyValue([string]$text, [string]$key, [string]$literal) {
    $pattern = '(?m)^(\s*"' + [regex]::Escape($key) + '"\s*:\s*)(-?\d+|true|false|"[^"]*")'
    if ([regex]::IsMatch($text, $pattern)) {
        return [regex]::Replace($text, $pattern, ('${1}' + $literal), 1)
    }
    $i = $text.IndexOf("{")
    if ($i -lt 0) { throw "配置文件格式异常：$cfg" }
    return $text.Insert($i + 1, "`r`n  `"$key`": $literal,")
}

# ---- 只维护背景层（海草 + 背景泡泡），不碰安装 ----
if ($Backdrop) {
    if (-not (Test-Path $cfg)) {
        Write-Host "没有找到 $cfg" -ForegroundColor Yellow
        Write-Host "说明这台机器还没跑过桌宠。先完整跑一遍 install.ps1，或在 DSH 里启用一次 dsh-pet 让它生成配置。" -ForegroundColor Yellow
        exit 1
    }

    $ver = "未知"
    $pkg = Join-Path $profileDir "node_modules\dsh-pet\package.json"
    if (Test-Path $pkg) {
        try {
            # 用正则取版本号：package.json 里的中文描述会被 PS5 的默认编码读坏，ConvertFrom-Json 会直接报错
            $m = [regex]::Match([System.IO.File]::ReadAllText($pkg), '"version"\s*:\s*"([^"]+)"')
            if ($m.Success) { $ver = $m.Groups[1].Value }
        } catch { }
    }

    $text = [System.IO.File]::ReadAllText($cfg)
    $enabled = Get-KeyValue $text "bubbleBackdropEnabled"
    $count = Get-KeyValue $text "bubbleBackdropCount"
    $motion = Get-KeyValue $text "bubbleBackdropMotion"
    $palette = Get-KeyValue $text "bubbleBackdropPalette"

    Write-Host "配置文件：$cfg"
    Write-Host "dsh-pet 版本：$ver"
    Write-Host "bubbleBackdropEnabled = $(if ($null -eq $enabled) { '缺失（缺省视为开）' } else { $enabled })"
    Write-Host "bubbleBackdropCount   = $(if ($null -eq $count) { '缺失（缺省 9）' } else { $count })"
    Write-Host "bubbleBackdropMotion  = $(if ($null -eq $motion) { '缺失（缺省 normal）' } else { $motion })"
    Write-Host "bubbleBackdropPalette = $(if ($null -eq $palette) { '缺失（缺省 candy）' } else { $palette })"

    if ($ver -ne "未知" -and ([version]($ver -replace '[^\d\.].*$', '') -lt [version]"0.4.1")) {
        Write-Host "当前 dsh-pet 版本低于 0.4.1，这个版本里没有海草/背景泡泡渲染层，改配置也不会出现。" -ForegroundColor Yellow
        Write-Host "修复：完整跑一遍 install.ps1 升到 0.4.1。" -ForegroundColor Yellow
    }

    if ($Backdrop -eq "check") { exit 0 }

    $want = if ($Backdrop -eq "on") { "true" } else { "false" }
    $new = Set-KeyValue $text "bubbleBackdropEnabled" $want
    if ($Backdrop -eq "on") {
        if ($count -eq "0") { $new = Set-KeyValue $new "bubbleBackdropCount" "9" }
        if ($motion -and $motion -notmatch '^"(calm|normal|lively)"$') { $new = Set-KeyValue $new "bubbleBackdropMotion" '"normal"' }
        if ($palette -and $palette -notmatch '^"(candy|ocean|sunset|rainbow|mono)"$') { $new = Set-KeyValue $new "bubbleBackdropPalette" '"candy"' }
    }

    if ($new -ne $text) {
        [System.IO.File]::WriteAllText($cfg, $new, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "已写入：bubbleBackdropEnabled = $want" -ForegroundColor Green
        Write-Host "桌宠每 30 秒拉一次配置，等半分钟；不生效就右键桌宠重启一次。" -ForegroundColor Green
    }
    else {
        Write-Host "配置已是目标值，未改动。" -ForegroundColor Green
    }
    exit 0
}

$tgz = Join-Path $here "dsh-pet-0.4.1.tgz"
if (-not (Test-Path $tgz)) { throw "找不到安装包：$tgz" }

if (-not (Test-Path $profileDir)) {
    throw "找不到 DSH web profile（$profileDir）。请先安装并运行一次 DeepSeek Harness（DSH）。"
}

Write-Host "[1/3] 安装 dsh-pet@0.4.1 到 $profileDir"
Push-Location $profileDir
try {
    npm install $tgz --save-exact --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }

    Write-Host "[2/3] 下载 Electron 运行时"
    npm --prefix (Join-Path $profileDir "node_modules\dsh-pet") run ensure:electron
    if ($LASTEXITCODE -ne 0) { throw "ensure:electron 失败" }

    Write-Host "[3/3] 把 dsh-pet 加入 profile 的 bundles"
    $pkgPath = Join-Path $profileDir "package.json"
    $pkg = Get-Content $pkgPath -Raw | ConvertFrom-Json
    if (-not $pkg.dsh) { $pkg | Add-Member -MemberType NoteProperty -Name dsh -Value ([pscustomobject]@{}) }
    if (-not $pkg.dsh.profile) { $pkg.dsh | Add-Member -MemberType NoteProperty -Name profile -Value ([pscustomobject]@{}) }
    $bundles = @()
    if ($pkg.dsh.profile.bundles) { $bundles = @($pkg.dsh.profile.bundles) }
    if ($bundles -notcontains "dsh-pet") { $bundles += "dsh-pet" }
    $pkg.dsh.profile | Add-Member -MemberType NoteProperty -Name bundles -Value $bundles -Force
    $pkg | ConvertTo-Json -Depth 20 | Set-Content $pkgPath -Encoding UTF8
}
finally {
    Pop-Location
}

Write-Host "完成。重启 DSH 后在插件列表里启用 dsh-pet，桌宠会出现在桌面上。" -ForegroundColor Green
Write-Host "个人配置目录：$env:USERPROFILE\.dsh\dsh-pet"

# 个人配置：没有就铺一份；已有则只补齐新版必填字段，不动用户原有设置
$cfgDir = Join-Path $env:USERPROFILE ".dsh\dsh-pet"
$cfg = Join-Path $cfgDir "main-config.jsonc"
if (-not (Test-Path $cfg)) {
    New-Item -ItemType Directory $cfgDir -Force | Out-Null
    Copy-Item (Join-Path $here "config.example.jsonc") $cfg
    Write-Host "已铺入默认个人配置：$cfg（碎碎念人设/海草泡泡等，可自行修改）" -ForegroundColor Green
}
else {
    # dsh-pet 把这几个开关当必填项，老版本配置缺了会被判「配置错误」，气泡开关也存不回去
    $required = [ordered]@{
        "whisperImageEnabled"    = "true"
        "chatImageEnabled"       = "true"
        "chatAnimationEnabled"   = "true"
        "bubbleBackdropEnabled"  = "true"
        "bubbleRiseSfxEnabled"   = "true"
        "bubbleBurstSfxEnabled"  = "true"
        "notificationsEnabled"   = "true"
        "clickSoundEnabled"      = "true"
        "confineToScreen"        = "false"
        "trayEnabled"            = "true"
    }
    $text = [System.IO.File]::ReadAllText($cfg)
    $lines = @()
    $names = @()
    foreach ($k in $required.Keys) {
        if ($text -notmatch ('"' + [regex]::Escape($k) + '"\s*:')) {
            $lines += ('  "' + $k + '": ' + $required[$k] + ',')
            $names += $k
        }
    }
    if ($lines.Count -gt 0) {
        $i = $text.IndexOf("{")
        if ($i -ge 0) {
            $text = $text.Insert($i + 1, "`r`n" + ($lines -join "`r`n"))
            [System.IO.File]::WriteAllText($cfg, $text, (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "已为旧配置补齐缺失字段：$($names -join ', ')" -ForegroundColor Yellow
        }
    }
}
