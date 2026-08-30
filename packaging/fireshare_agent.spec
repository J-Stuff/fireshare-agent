# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller packaging/fireshare_agent.spec  (run from the repo root)
# Produces an onedir build under dist/FireshareAgent/ - faster startup than a onefile build,
# at the cost of shipping a folder instead of a single exe.
import os

block_cipher = None
repo_root = os.path.join(SPECPATH, "..")

a = Analysis(
    [os.path.join(repo_root, "main.py")],
    pathex=[repo_root],
    binaries=[],
    datas=[(os.path.join(repo_root, "img"), "img")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FireshareAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(repo_root, "img", "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="FireshareAgent",
)
