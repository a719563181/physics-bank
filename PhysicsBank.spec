# -*- mode: python ; coding: utf-8 -*-
"""
PhysicsBank.spec — PyInstaller 打包配置

产物：dist/PhysicsBank/PhysicsBank.exe（onedir，启动快）
用 onedir 而非 onefile：本项目依赖 PySide6 + matplotlib，
onefile 每次启动都要把几百 MB 解压到临时目录，启动要几十秒，不可接受。

打包命令：
  venv-build\\Scripts\\pyinstaller.exe --clean --noconfirm PhysicsBank.spec
"""

import os
import shutil
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.dirname(SPEC))
APP = os.path.join(ROOT, 'app')

# ---------- 随包的资源 ----------
datas = []

# 应用代码本身（launcher 会把它加进 sys.path）
for fn in os.listdir(APP):
    if fn.endswith('.py'):
        datas.append((os.path.join(APP, fn), 'app'))

# 初始数据（首次运行拷到用户目录）
# ★ 绝对不要把开发目录的 bank.db 打进去！
#   否则：① 用户拿到的是别人的题库；② exe 旁出现 data\ 会触发便携模式误判。
#   随包只放「空模板」：一个全新的空库，知识树可选预置。
#
# ★★ 隐藏陷阱：这份模板会被解压到 <exe>\_internal\data\bank.db。
#   而未设 PHYSICS_BANK_HOME 时 db.py 会用 dirname(dirname(__file__)) 算 ROOT，
#   指向的就是 _internal\data —— 一旦这个文件存在，db 就会「静默连上随包模板」，
#   用户看到的是空库（以为题全丢了）。所以**必须**用 renpy 改名，
#   让 db.py 永远找不到 _internal\data\bank.db。
BOOT_DIR = os.path.join(ROOT, 'build', 'seed')
if os.path.isdir(BOOT_DIR):
    for fn in os.listdir(BOOT_DIR):
        p = os.path.join(BOOT_DIR, fn)
        if os.path.isfile(p):
            if fn.lower().endswith('.db'):
                # ★ PyInstaller 只支持 (src, dest_dir) 二元组，不能逐文件改名。
                #   所以先把模板拷成一个「带 .tpl 后缀的目录名 + 同名文件」，
                #   再整目录映射；这样包内是 _internal\data_tpl\bank.db，
                #   db.py 找 _internal\data\bank.db 永远找不到（目录名不同）。
                tpl_dir = os.path.join(ROOT, 'build', '_seed_tpl')
                os.makedirs(tpl_dir, exist_ok=True)
                shutil.copy2(p, os.path.join(tpl_dir, fn))
                datas.append((tpl_dir, 'data_tpl'))
            else:
                datas.append((p, 'data'))
			
themes_dir = os.path.join(ROOT, 'themes')
if os.path.isdir(themes_dir):
    for fn in os.listdir(themes_dir):
        p = os.path.join(themes_dir, fn)
        if os.path.isfile(p):
            datas.append((p, 'themes'))

# ---------- 隐式依赖 ----------
# matplotlib 后端 / 中文字体 / mathtext 字体表
hiddenimports = [
    'matplotlib.backends.backend_agg',
    'matplotlib.mathtext',
    'matplotlib.font_manager',
    'docx',
    'pptx',
    'openpyxl',
    'PIL',
    'fitz',
    'pymupdf',
    'sympy',
    'sqlite3',
    'win32com',
    'win32com.client',
    'pythoncom',
    'pywintypes',
    # ★ 2026-10-22：这两个在函数体内 import，静态分析可能漏掉
    'layout_enhance',
    'word_import',
]
for pkg in ('pptx', 'docx', 'openpyxl'):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# matplotlib 的数据文件（字体表、样式表）
try:
    datas += collect_data_files('matplotlib')
except Exception:
    pass

# ---------- 排除明确不用的重型包 ----------
excludes = [
    'torch', 'torchvision', 'torchaudio',
    'paddle', 'paddleocr', 'paddlex',
    'modelscope', 'aistudio_sdk', 'bce',
    'cv2', 'nvidia',
    # 注意：unittest / pyparsing 不要排除 ——
    # matplotlib -> pyparsing.testing -> unittest，排除会直接崩
    'tkinter',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DAnimation',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQml',
    'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
    'PySide6.QtSerialPort', 'PySide6.QtSql', 'PySide6.QtTest',
    'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
    'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtUiTools',
    'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
    'PySide6.QtSvgWidgets', 'PySide6.QtNetwork', 'PySide6.QtPrintSupport',
]

block_cipher = None

a = Analysis(
    [os.path.join(APP, 'launcher.py')],
    pathex=[APP, ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name='PhysicsBank',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口（GUI 程序）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'build', 'app.ico')
        if os.path.exists(os.path.join(ROOT, 'build', 'app.ico')) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PhysicsBank',
)
