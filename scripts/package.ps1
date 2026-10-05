<#
  便携版打包脚本
  用法:  powershell -ExecutionPolicy Bypass -File scripts\package.ps1
  产物:  dist\<项目文件夹名>-便携版.zip
  说明:  1) 排除 data / dist / .venv / __pycache__ / 开发脚本 等一切开发与运行产物;
         2) runtime/ 便携运行环境完整打进包里, 对方解压后双击 start.bat 即可使用;
         3) 打包完成后自动做「安全校验」：缺必需文件、或混入个人数据即失败。
  注意:  本文件含中文，必须以 UTF-8 (带 BOM) 编码保存（PowerShell 5.1 的要求）。
#>
$ErrorActionPreference = "Stop"

$root       = Split-Path -Parent $PSScriptRoot       # 项目根目录
$folderName = Split-Path -Leaf $root                 # 项目文件夹名 (zip 顶层目录)
$parentDir  = Split-Path -Parent $root
$distDir    = Join-Path $root "dist"
$zipPath    = Join-Path $distDir "$folderName-便携版.zip"

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }

$excludeArgs = @(
    "--exclude=$folderName/.venv",
    "--exclude=$folderName/.git",
    "--exclude=$folderName/build_tmp",
    "--exclude=$folderName/data",
    "--exclude=$folderName/dist",
    "--exclude=$folderName/scripts/_*",
    "--exclude=$folderName/scripts/collect_sample.py",
    "--exclude=$folderName/scripts/diag_boss.py",
    "--exclude=$folderName/scripts/screenshot.py",
    "--exclude=$folderName/scripts/verify_block*",
    "--exclude=$folderName/项目计划书.md",
    "--exclude=$folderName/UI设计规范.md",
    "--exclude=$folderName/runtime/Scripts",
    "--exclude=$folderName/runtime/Lib/site-packages/playwright/driver/package/.local-browsers",
    "--exclude=$folderName/runtime/Lib/site-packages/playwright/driver/package/.local-browsers/*",
    "--exclude=*/__pycache__*",
    "--exclude=*.pyc"
)

# ---- 内容级扫描（打包前）：任何待打包文件都不得内嵌本机路径（含 exe 二进制）----
$py      = Join-Path $root "runtime\python.exe"
$scanner = Join-Path $root "scripts\package_scan.py"
if ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $scanner)) {
    $scanOut = & $py -X utf8 $scanner --root $root --needle $root --needle $env:USERPROFILE
    $scanOut | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "打包失败：内容扫描发现本机路径残留（见上）。"
        exit 1
    }
} else {
    Write-Host "[警告] 缺少 runtime 或扫描脚本，已跳过内容扫描。"
}

Push-Location $parentDir
try {
    tar -a -c -f $zipPath @excludeArgs $folderName
    if ($LASTEXITCODE -ne 0) { throw "tar 打包失败, 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

# ---- 安全校验：缺必需文件 / 混入个人数据 → 直接判定失败 ----
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $names = @($zip.Entries | ForEach-Object { $_.FullName })
} finally {
    $zip.Dispose()
}

$required = @(
    "$folderName/start.bat",
    "$folderName/runtime/python.exe",
    "$folderName/backend/main.py",
    "$folderName/frontend/index.html",
    "$folderName/frontend/style.css",
    "$folderName/frontend/app.js",
    "$folderName/frontend/themes/dark.css",
    "$folderName/scripts/open_ui.py",
    "$folderName/使用说明.md"
)
$missing = @($required | Where-Object { $names -notcontains $_ })
if ($missing.Count -gt 0) {
    Write-Host "打包失败：缺少必需条目 —"
    $missing | ForEach-Object { Write-Host "  x $_" }
    exit 1
}

$violations = @()
foreach ($n in $names) {
    if ($n.StartsWith("$folderName/data/")) { $violations += $n; continue }
    if ($n.StartsWith("$folderName/runtime/")) { continue }   # runtime 内第三方包自带 data/ 目录属正常
    if ($n -match '(^|/)__pycache__(/|$)') { $violations += $n; continue }
    if ($n -match '\.pyc$') { $violations += $n; continue }
    if ($n -match 'scripts/_') { $violations += $n; continue }
    if ($n -match 'runtime/Scripts/') { $violations += $n; continue }
    if ($n -match '\.local-browsers') { $violations += $n; continue }
    if ($n -match '(user_config|server_console|browser_data|task_logs|_shots|_backup|\.db$)') { $violations += $n; continue }
}
if ($violations.Count -gt 0) {
    Write-Host "打包失败：检出敏感/开发产物条目（前 20 条）—"
    $violations | Select-Object -First 20 | ForEach-Object { Write-Host "  x $_" }
    exit 1
}

$sizeMB = [math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "打包完成并通过安全校验: $zipPath"
Write-Host "  $sizeMB MB | $($names.Count) 个条目 | 未检出任何个人数据"
Write-Host ""
Write-Host "分发方式: 解压到任意目录, 双击 start.bat 即可使用。"
Write-Host "GitHub 发布: 本 zip 传到 Releases 作为附件（不要提交进仓库），仓库只放源码。"

