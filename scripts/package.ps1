# 打包部署包：生成一个干净的 zip，拷到云服务器解压即可
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1 -WithConfig   # 连同 config.yaml 一起打包（含你的 Key，慎用）
#
# 默认不打包：.venv / data / logs / config.yaml / __pycache__ / dist
param(
    [string]$Output = "",
    [switch]$WithConfig
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
if (-not $Output) { $Output = Join-Path $root "dist\ai接入-$stamp.zip" }
$Output = [IO.Path]::GetFullPath($Output)
$dist = Split-Path -Parent $Output
New-Item -ItemType Directory -Force -Path $dist | Out-Null

$stage = [IO.Path]::GetFullPath((Join-Path $env:TEMP ("aijieru-package-" + [guid]::NewGuid().ToString("N"))))
$tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
if (-not $stage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "临时路径越界" }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$include = @(
    "src", "web", "scripts", "tests", "docs",
    "README.md", "LICENSE", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md",
    "THIRD_PARTY_NOTICES.md", ".gitattributes", ".github", "requirements.txt", "requirements-dev.txt",
    "config.example.yaml", "config.minimal.yaml", "constraints-tested.txt", ".env.example", ".gitignore",
    "requirements-wechat.txt", "requirements-media.txt", "requirements-legacy.txt",
    "start_bot.bat", "start_web.bat", "start_manage.bat"
)
foreach ($item in $include) {
    $src = Join-Path $root $item
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $stage $item
    New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
    Copy-Item $src $dst -Recurse -Force
}
if ($WithConfig) {
    Copy-Item (Join-Path $root "config.yaml") (Join-Path $stage "config.yaml") -Force
    Write-Host "⚠️  已包含 config.yaml（里面有你的 API Key），不要外传这个压缩包！" -ForegroundColor Yellow
}

# 个人排查归档和旧聊天截图不进入公开源码包；只操作上面已验证的临时目录。
$archive = Join-Path $stage "docs\archive"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Recurse -Force }
Get-ChildItem -LiteralPath (Join-Path $stage "docs\screenshots") -Filter "web-console-*.png" -ErrorAction SilentlyContinue |
    Remove-Item -Force

# 清掉不该进包的东西
Get-ChildItem $stage -Recurse -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $stage -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } | Remove-Item -Force -ErrorAction SilentlyContinue

if (Test-Path -LiteralPath $Output) { throw "输出文件已存在，请使用新的 -Output 路径" }
# 明确使用 ZIP 标准的 / 分隔符，兼容跨平台解压，并保留 .github 等文件。
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipOut = [IO.Compression.ZipFile]::Open($Output, [IO.Compression.ZipArchiveMode]::Create)
try {
    $stagePrefix = $stage.TrimEnd('\') + '\'
    Get-ChildItem -LiteralPath $stage -Recurse -File | ForEach-Object {
        $entryName = $_.FullName.Substring($stagePrefix.Length).Replace('\', '/')
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zipOut, $_.FullName, $entryName, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally { $zipOut.Dispose() }
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue

$size = (Get-Item $Output).Length / 1MB
Write-Host ""
Write-Host ("✅ 打包完成：{0}（{1:N2} MB）" -f $Output, $size) -ForegroundColor Green
Write-Host "   · 拷到 Windows 云服务器后解压，按 docs\DEPLOY.md 操作"
if (-not $WithConfig) {
    Write-Host "   · 包内没有 config.yaml：部署后运行 setup.ps1 自动创建最小配置，再填 Key" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "包内结构："
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($Output)
try {
    $zip.Entries | Group-Object { ($_.FullName -split "[/\\]")[0] } | Sort-Object Name |
        ForEach-Object { "  {0,-26} {1,4} 个文件" -f $_.Name, $_.Count }
} finally {
    $zip.Dispose()
}
