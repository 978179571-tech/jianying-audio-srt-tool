$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    python -m PyInstaller --noconfirm --onefile --windowed --name '配音字幕整理-v1.1' --paths $PSScriptRoot app.py
    if ($LASTEXITCODE -ne 0) { throw '程序打包失败。' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '使用说明.md') -Destination (Join-Path $PSScriptRoot 'dist\使用说明.md')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'examples\script.txt') -Destination (Join-Path $PSScriptRoot 'dist\示例文案.txt')
} finally {
    Pop-Location
}
