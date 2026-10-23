# -*- coding: utf-8 -*-
"""
build_ocr_pack.py — 组装「OCR 引擎包」（独立分发用）

为什么需要它
============
`venv-ocr` 是一个 **venv**，它的 pyvenv.cfg 指向机器的 uv python
(base_prefix = %APPDATA%\\uv\\python\\...)，**不可移植**：
换台机器（或删掉那个 uv python）就跑不起来。

本脚本把 venv-ocr 重组成一个**自包含**目录 runtime-ocr/：
  runtime-ocr/
    python.exe            <- 从 uv base python 拷来（一套完整 CPython）
    python3*.dll, DLLs/   <- CPython 运行时
    Lib/                  <- 基础标准库 + site-packages（含 paddle/paddleocr/cuda）
    Scripts/python.exe    <- 兼容两种查找路径
    pyvenv.cfg 不要！      <- 去掉它，避免被当成 venv 去解析 base

产物用标准 CPython 的「python.exe 直接跑」方式启动，跟装在哪无关。

用法：
  venv-build\\Scripts\\python.exe build\\build_ocr_pack.py
  -> 产出 build\\ocr_pack\\runtime-ocr\\  （自包含，可直接解压到程序旁）
"""
import os
import sys
import shutil
import subprocess

PROJ = r'E:\openclaw\physics-bank'
BASE_URL = r'C:\Users\Administrator.DESKTOP-HG47USK\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none'
VENV = os.path.join(PROJ, 'venv-ocr')
OUT = os.path.join(PROJ, 'build', 'ocr_pack', 'runtime-ocr')


def log(*a):
    print(*a, flush=True)


def sizeof(path):
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main():
    if not os.path.isdir(BASE_URL):
        log('!! 找不到基础 python：', BASE_URL)
        return 1
    if not os.path.isdir(VENV):
        log('!! 找不到 venv-ocr：', VENV)
        return 1

    log('清空输出目录...')
    if os.path.isdir(OUT):
        shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)

    # 1) 拷贝基础 CPython 运行时（python.exe + DLLs + Lib 基础标准库）
    log('拷贝基础 CPython 运行时（约 60 MB）...')
    for name in os.listdir(BASE_URL):
        if name.lower() == 'lib':
            continue  # 标准库稍后单独并（要保留 site-packages）
        src = os.path.join(BASE_URL, name)
        dst = os.path.join(OUT, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=False,
                            ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(src, dst)

    # 2) 基础标准库 Lib（去掉 site-packages，我们用 venv 的）
    log('拷贝标准库 Lib/ ...')
    base_lib = os.path.join(BASE_URL, 'Lib')
    out_lib = os.path.join(OUT, 'Lib')
    os.makedirs(out_lib, exist_ok=True)
    for name in os.listdir(base_lib):
        if name == 'site-packages':
            continue
        src = os.path.join(base_lib, name)
        dst = os.path.join(out_lib, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=False,
                            ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(src, dst)

    # 3) venv 的 site-packages 覆盖进去（含 paddle / paddleocr / cuda）
    log('拷贝 venv-ocr 的 site-packages（约 4.5 GB，耐心等）...')
    vsp = os.path.join(VENV, 'Lib', 'site-packages')
    osp = os.path.join(out_lib, 'site-packages')
    os.makedirs(osp, exist_ok=True)
    for name in os.listdir(vsp):
        src = os.path.join(vsp, name)
        dst = os.path.join(osp, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=False,
                            ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(src, dst)

    # 4) 兼容路径：Scripts\python.exe（有的查找逻辑找这个）
    log('补 Scripts\\python.exe 兼容路径...')
    scripts = os.path.join(OUT, 'Scripts')
    os.makedirs(scripts, exist_ok=True)
    shutil.copy2(os.path.join(OUT, 'python.exe'),
                 os.path.join(scripts, 'python.exe'))

    # 5) 绝不放 pyvenv.cfg（放了会被当 venv -> 又去解析 base）
    cfg = os.path.join(OUT, 'pyvenv.cfg')
    if os.path.exists(cfg):
        os.remove(cfg)

    # 6) 自检：跑一遍看 paddle / fitz 能不能 import
    log('自检：import paddle / paddleocr / fitz ...')
    py = os.path.join(OUT, 'python.exe')
    code = ('import sys; print("exe", sys.executable); print("prefix", sys.prefix)')
    subprocess.run([py, '-X', 'utf8', '-c', code], env={**os.environ})
    code2 = ('import fitz, paddle; import paddleocr; '
             'print("paddle", paddle.__version__); '
             'print("cuda", paddle.device.is_compiled_with_cuda()); '
             'print("gpu", paddle.device.cuda.device_count())')
    r = subprocess.run([py, '-X', 'utf8', '-c', code2],
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    log('自检返回码:', r.returncode)

    mb = sizeof(OUT) / (1024 * 1024)
    log('完成！runtime-ocr 体积：%.1f MB' % mb)
    return 0 if r.returncode == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
