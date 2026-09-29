# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.building.datastruct import TOC

project_dir = Path(SPEC).resolve().parent

datas = [
    (str(project_dir / "package_resources" / "config.json"), "."),
    (str(project_dir / "package_resources" / "使用说明.txt"), "."),
]

a = Analysis(
    [str(project_dir / "智能解压V1.0.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# 避免把其他工具目录中的 ICU DLL 带入包内并覆盖 Windows 系统版本。
excluded_runtime_dlls = {"icuuc.dll", "icudt78.dll"}
a.binaries = TOC(
    entry for entry in a.binaries
    if Path(entry[0]).name.casefold() not in excluded_runtime_dlls
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="智能解压工作台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="智能解压工作台",
)
