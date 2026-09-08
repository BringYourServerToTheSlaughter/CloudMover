
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH)
version_scope = {}
exec((project_root / "src/cloudmover/version.py").read_text(), version_scope)
VERSION = version_scope["VERSION"]

a = Analysis(
    [str(project_root / "cloudmover_app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "assets" / "CloudMover.png"), "assets"),
           (str(project_root / "assets" / "Pingstate.png"), "assets")],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "src.cloudmover.start",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CloudMover",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
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
    name="CloudMover",
)

app = BUNDLE(
    coll,
    name="CloudMover.app",
    icon=str(project_root / "assets" / "CloudMover.icns"),
    bundle_identifier="com.cloudmover.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "CloudMover",
        "CFBundleDisplayName": "CloudMover",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    },
)
