# SPDX-License-Identifier: MIT
# Build a Windows x86_64 distribution of protoboard-designer.
#
# Output:
#   dist\protoboard-designer\                          (one-folder bundle)
#   dist\protoboard-designer-<VERSION>-windows-x86_64.zip
#
# Requirements:
#   - uv (https://docs.astral.sh/uv/)
#   - pyinstaller (declared in pyproject.toml [project.optional-dependencies] dev)
#
# Run from any directory:
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# Read version from pyproject.toml.
$VersionLine = Select-String -Path "pyproject.toml" -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionLine) {
    Write-Error "Could not parse version from pyproject.toml"
}
$Version = $VersionLine.Matches.Groups[1].Value

$Arch = "x86_64"
$BundleName = "protoboard-designer"
$ZipPath = "dist\$BundleName-$Version-windows-$Arch.zip"

Write-Host "==> Building $BundleName $Version for windows-$Arch"

Write-Host "==> Syncing dev dependencies (includes pyinstaller)"
& uv sync --extra dev
if ($LASTEXITCODE -ne 0) { Write-Error "uv sync failed" }

Write-Host "==> Running PyInstaller"
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist\$BundleName") { Remove-Item -Recurse -Force "dist\$BundleName" }
& uv run pyinstaller "packaging\protoboard.spec" --clean --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed" }

if (-not (Test-Path "dist\$BundleName")) {
    Write-Error "PyInstaller did not produce dist\$BundleName\"
}

Write-Host "==> Smoke test: launching --help"
$env:QT_QPA_PLATFORM = "offscreen"
& "dist\$BundleName\$BundleName.exe" --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Smoke test failed (binary did not print help cleanly)"
}

Write-Host "==> Packing zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath }
Compress-Archive -Path "dist\$BundleName" -DestinationPath $ZipPath

$Size = "{0:N1} MB" -f ((Get-Item $ZipPath).Length / 1MB)
Write-Host ""
Write-Host "Done."
Write-Host "  Bundle: dist\$BundleName\"
Write-Host "  Zip:    $ZipPath"
Write-Host "  Size:   $Size"
