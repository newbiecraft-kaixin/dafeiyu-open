# 安装本机同款桌宠：dsh-pet 0.4.1
# 说明：这台电脑上实际运行的桌宠是 dsh-pet（DeepSeek Harness 的桌宠插件），
#       不是 clawd-on-desk。本脚本把仓库内自带的 0.4.1 安装包装进 DSH 的 web profile。
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tgz = Join-Path $here "dsh-pet-0.4.1.tgz"
if (-not (Test-Path $tgz)) { throw "找不到安装包：$tgz" }

$profileDir = Join-Path $env:USERPROFILE ".dsh\profiles\web"
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
