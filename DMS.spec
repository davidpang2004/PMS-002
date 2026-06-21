# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('dms.html', '.')]
binaries = []
hiddenimports = ['pillow_heif', 'fitz', 'qrcode', 'pyngrok', 'pyngrok.ngrok', 'pyngrok.conf', 'tkinter', 'tkinter.ttk', 'tkinter.scrolledtext', 'tkinter.font', 'tkinter.messagebox', 'flask', 'dms_server', 'databook', 'pdf_extraction', '_dms_trial']
datas += copy_metadata('Pillow')
hiddenimports += collect_submodules('tkinter')
hiddenimports += collect_submodules('tkinter.ttk')
hiddenimports += collect_submodules('tkinter.scrolledtext')
hiddenimports += collect_submodules('tkinter.font')
hiddenimports += collect_submodules('tkinter.messagebox')
hiddenimports += collect_submodules('flask')
hiddenimports += collect_submodules('werkzeug')
hiddenimports += collect_submodules('jinja2')
hiddenimports += collect_submodules('click')
hiddenimports += collect_submodules('itsdangerous')
hiddenimports += collect_submodules('markupsafe')
hiddenimports += collect_submodules('waitress')
hiddenimports += collect_submodules('pypdf')
hiddenimports += collect_submodules('reportlab')
hiddenimports += collect_submodules('PIL')
hiddenimports += collect_submodules('pillow_heif')
hiddenimports += collect_submodules('fitz')
hiddenimports += collect_submodules('qrcode')
hiddenimports += collect_submodules('pyngrok')
tmp_ret = collect_all('pillow_heif')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['/Users/david/PMS/dms_launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'scipy', 'matplotlib', 'idlelib', 'turtle', 'turtledemo', 'lib2to3'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DMS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='DMS',
)
app = BUNDLE(
    coll,
    name='DMS.app',
    icon=None,
    bundle_identifier='com.david.qcdms',
)
