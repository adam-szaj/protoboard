# SPDX-License-Identifier: MIT
# PyInstaller spec — produces a one-folder distribution.
#
# Build:
#   uv run pyinstaller packaging/protoboard.spec --clean --noconfirm
#
# Output: dist/protoboard-designer/ — a self-contained folder with the
# executable, bundled Qt runtime, and the read-only assets.

import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).parent

datas = [
    (str(PROJECT_ROOT / "library"), "library"),
    (str(PROJECT_ROOT / "assets"), "assets"),
    (str(PROJECT_ROOT / "boards"), "boards"),
    (str(PROJECT_ROOT / "README.md"), "."),
    (str(PROJECT_ROOT / "CHANGELOG.md"), "."),
    (str(PROJECT_ROOT / "LICENSE"), "."),
    # Default toolbar layout shipped with the application — copied to
    # ~/.config/protoboard/toolbars.yaml on first start.  Bundled next
    # to paths.py so ``Path(__file__).parent`` resolves correctly in
    # both source checkout and the PyInstaller one-folder dist.
    (
        str(PROJECT_ROOT / "src" / "protoboard" / "default_toolbars.yaml"),
        "protoboard",
    ),
]

# Drop any data entries whose source path is missing — keeps the spec
# usable if optional assets (boards/, CHANGELOG.md) are absent.
datas = [(src, dst) for src, dst in datas if Path(src).exists()]

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "src" / "protoboard" / "app.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pytest",
        "hypothesis",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="protoboard-designer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="protoboard-designer",
)
