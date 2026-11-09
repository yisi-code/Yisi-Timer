# -*- mode: python ; coding: utf-8 -*-
import os

# 发行目录在源码目录的上一层（与源码目录同级、名字去掉“-源码”）
OUT = os.path.abspath(os.path.join(SPECPATH, '..', 'yisi-定时工具'))

a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(OUT, 'yisi-定时工具.exe'), 'payload'),
        # 默认配置单独放在 defaults/，不要把开发者本机正在用的配置打进安装包
        (os.path.join(SPECPATH, 'defaults', 'reminder_options.json'), 'payload'),
        (os.path.join(SPECPATH, 'defaults', 'reminders.json'), 'payload'),
        (os.path.join(SPECPATH, 'defaults', 'categories.json'), 'payload'),
    ],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='安装 yisi-定时工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
