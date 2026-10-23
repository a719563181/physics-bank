"""
launcher.py — 打包入口
======================
打包后的 exe 走这里启动。与原 app/main.py 的区别：

1. **数据目录可写**：打包后程序装在 Program Files（只读），
   数据库/附件/公式缓存/导出/主题必须放到用户可写目录。
   规则：优先读环境变量 PHYSICS_BANK_HOME；否则用
   %LOCALAPPDATA%\\PhysicsBank。首次运行时把随包发布的
   初始 data/ 和 themes/ 拷过去。
2. **OCR 提示**：精简版不含 OCR；按钮保留但会明确提示。
"""

import os
import sys
import shutil
import traceback


def _resource_dir():
    """随包发布的只读资源目录（PyInstaller 解包后的 _MEIPASS）。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _home_dir(verbose=False):
    """用户可写数据目录。

    优先级：
      1. 环境变量 PHYSICS_BANK_HOME（可写绝对路径）
      2. 程序目录旁的 data\\（便携模式：把程序放 U 盘/文件夹就能带走数据）
      3. %LOCALAPPDATA%\\PhysicsBank（标准安装）

    便携模式的判断是严格的：**只有程序目录旁已经存在一个真正的
    data\\bank.db（是文件，不是空目录）才用便携**。
    这样避免两类坑：
      · 空 data\\ 目录被误判成便携，题写到奇怪的地方
      · 建了 data\\ 但里面没 bank.db，仍走 LOCALAPPDATA（不静默切换）
    """
    env = os.environ.get('PHYSICS_BANK_HOME')
    if env:
        d = os.path.abspath(env)
        os.makedirs(d, exist_ok=True)
        if verbose:
            print('[launcher] 数据目录 = %s  (来自 PHYSICS_BANK_HOME)' % d)
        return d

    # 便携模式：程序目录旁的 data\\ 里有真 bank.db -> 用它
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        portable_db = os.path.join(exe_dir, 'data', 'bank.db')
        if os.path.isfile(portable_db):
            if verbose:
                print('[launcher] 数据目录 = %s  (便携模式)' % exe_dir)
            return exe_dir
    except Exception:
        pass

    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    d = os.path.join(base, 'PhysicsBank')
    os.makedirs(d, exist_ok=True)
    if verbose:
        print('[launcher] 数据目录 = %s  (标准安装)' % d)
    return d


def current_home():
    """当前实际使用的数据目录（给 [工具 -> 数据位置] 显示）。"""
    return _home_dir()


def current_db_path():
    """当前实际使用的数据库文件路径。"""
    return os.path.join(_home_dir(), 'data', 'bank.db')


def _seed(home, res):
    r"""首次运行：把随包的初始 data/themes 铺到用户目录（已存在的不覆盖）。

    ★ 关键安全规则：**绝不往「程序目录旁」写字**。
      只在「随包资源目录(res) != 程序目录(exe_dir)」时才铺。
      因为一旦 exe 旁出现 data\bank.db，下次启动就会切成便携模式，
      用户会以为「题不见了」。随包的资源在 _internal 下的 data（onedir），
      绝不能和 exe 旁的 data\ 混为一谈。
    """
    exe_dir = None
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    except Exception:
        pass
    res_n = os.path.normcase(os.path.abspath(res))
    exe_n = os.path.normcase(os.path.abspath(exe_dir)) if exe_dir else None

    # 如果随包资源目录就是程序目录本身，绝不能把 data/ 铺到那里
    if exe_n is not None and res_n == exe_n:
        return

    # 如果 home 就是程序目录（便携模式），也不要铺（用户自己的数据优先）
    home_n = os.path.normcase(os.path.abspath(home))
    if exe_n is not None and home_n == exe_n:
        return

    for sub in ('data', 'themes'):
        src = os.path.join(res, sub)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(home, sub)
        os.makedirs(dst, exist_ok=True)
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            tgt = dst if rel == '.' else os.path.join(dst, rel)
            os.makedirs(tgt, exist_ok=True)
            for f in files:
                sp = os.path.join(root, f)
                # 随包模板可能被改名为 .dbtpl（避免 _internal\data\bank.db
                # 被 db.py 直接认到）。铺到用户目录时要还原成原名。
                if f.endswith('.db.tpl'):
                    dp = os.path.join(tgt, f[:-4])
                else:
                    dp = os.path.join(tgt, f)
                if not os.path.exists(dp):
                    try:
                        shutil.copy2(sp, dp)
                    except Exception:
                        pass


def _detect_ocr_engine(home):
    """判断旁边有没有可用的 OCR 引擎（独立解释器）。

    与 import_dialog.OcrWorker._ocr_search_roots 保持同一套根目录：
      · 数据/程序根（home）
      · exe 所在目录及其上溯两级
    只要任一目录下存在 venv-ocr / runtime-ocr / ocr 解释器就算有。

    注意：这里【不 import import_dialog】（会拉起 Qt）。纯文件系统检查。
    """
    roots = []
    try:
        roots.append(os.path.abspath(home))
    except Exception:
        pass
    try:
        base = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(base)
        for _ in range(2):
            base = os.path.dirname(base)
            if base:
                roots.append(base)
    except Exception:
        pass
    env = os.environ.get('PHYSICS_BANK_OCR_PYTHON')
    if env and os.path.isfile(env):
        return True
    cands = []
    for r in roots:
        for sub in (('venv-ocr', 'Scripts', 'python.exe'),
                    ('runtime-ocr', 'python.exe'),
                    ('runtime-ocr', 'Scripts', 'python.exe'),
                    ('ocr', 'python.exe'),
                    ('python-ocr', 'python.exe')):
            cands.append(os.path.join(r, *sub))
    for c in cands:
        if os.path.isfile(c):
            return True
    return False


def main():
    res = _resource_dir()
    home = _home_dir()

    # 把代码目录和数据目录都塞进 sys.path，让 import db 之类照常工作
    app_dir = os.path.join(res, 'app')
    if os.path.isdir(app_dir):
        sys.path.insert(0, app_dir)
    sys.path.insert(0, res)

    # ★ 必须在任何 import db 之前设好：db.py 在 import 期就会算 ROOT
    #   并 makedirs。如果不提前设，打包版会把 data\\ 建到 exe 旁边，
    #   下次启动就被判成「便携模式」→ 用户以为题丢了。
    os.environ['PHYSICS_BANK_HOME'] = home

    _seed(home, res)

    # ---- 关键：改写 db 的路径常量，指向用户可写目录 ----
    import db as _db
    data_dir = os.path.join(home, 'data')
    _db.ROOT = home
    _db.DATA_DIR = data_dir
    _db.ATTACH_DIR = os.path.join(data_dir, 'attachments')
    _db.DB_PATH = os.path.join(data_dir, 'bank.db')
    os.makedirs(_db.DATA_DIR, exist_ok=True)
    os.makedirs(_db.ATTACH_DIR, exist_ok=True)
    _db.init_db()

    # 公式缓存放用户目录
    import formula as _formula
    cache = os.path.join(data_dir, 'formula_cache')
    os.makedirs(cache, exist_ok=True)
    if hasattr(_formula, 'CACHE_DIR'):
        _formula.CACHE_DIR = cache

    # 主题目录
    import ppt_theme as _pt
    for attr in ('THEME_DIR', 'THEMES_DIR'):
        if hasattr(_pt, attr):
            d = os.path.join(home, 'themes')
            os.makedirs(d, exist_ok=True)
            setattr(_pt, attr, d)

    # OCR：安装包默认【不含】引擎，但若用户在旁边放了「OCR 引擎包」
    # （runtime-ocr \ venv-ocr 目录），就应当能用 —— 不能一棍子打死。
    # 这里只在【真的找不到引擎】时才设 NO_OCR=1（用于给出准确提示），
    # 找到了就让 OcrWorker 正常去找。
    try:
        _ocr_found = _detect_ocr_engine(home)
    except Exception:
        _ocr_found = False
    if not _ocr_found:
        os.environ.setdefault('PHYSICS_BANK_NO_OCR', '1')

    import main as _main
    # 让主程序里的导出目录也落到用户目录
    if hasattr(_main, 'EXPORT_DIR'):
        try:
            _main.EXPORT_DIR = os.path.join(home, 'exports')
            os.makedirs(_main.EXPORT_DIR, exist_ok=True)
        except Exception:
            pass
    _main.main()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # 打包后没有控制台，把崩溃写到日志文件，方便排查
        try:
            log = os.path.join(_home_dir(), 'crash.log')
            with open(log, 'a', encoding='utf-8') as f:
                f.write('\n===== %s =====\n' % __import__('datetime').datetime.now())
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise
