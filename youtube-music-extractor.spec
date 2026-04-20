# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for YouTube Music Extractor.

Build:
    pyinstaller youtube-music-extractor.spec --clean --noconfirm

Inputs:
    vendor/ffmpeg.exe   (not committed; see docs/BUILD.md)
"""
import os

block_cipher = None

here = os.path.abspath(os.path.dirname(SPEC))
ffmpeg_src = os.path.join(here, "vendor", "ffmpeg.exe")
if not os.path.isfile(ffmpeg_src):
    raise SystemExit(
        "vendor/ffmpeg.exe not found. Place an ffmpeg.exe build there "
        "before running PyInstaller. See docs/BUILD.md."
    )

a = Analysis(
    ["main.py"],
    pathex=[here],
    binaries=[(ffmpeg_src, ".")],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="YouTubeMusicExtractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
