# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for YouTube Music Extractor.

Build:
    pyinstaller youtube-music-extractor.spec --clean --noconfirm

Inputs:
    vendor/ffmpeg.exe   (not committed; see docs/BUILD.md)
"""
import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None

here = os.path.abspath(os.path.dirname(SPEC))
ffmpeg_src = os.path.join(here, "vendor", "ffmpeg.exe")
if not os.path.isfile(ffmpeg_src):
    raise SystemExit(
        "vendor/ffmpeg.exe not found. Place an ffmpeg.exe build there "
        "before running PyInstaller. See docs/BUILD.md."
    )

# CustomTkinter 는 테마 JSON 과 폰트 파일을 런타임에 로드한다.
# 일반 import 추적으로는 안 잡히므로 collect_all 로 명시적으로 번들.
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")

a = Analysis(
    ["main.py"],
    pathex=[here],
    binaries=[(ffmpeg_src, ".")] + ctk_binaries,
    datas=ctk_datas,
    hiddenimports=ctk_hiddenimports,
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
