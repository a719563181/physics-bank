"""
import_dialog.py — OCR 批量导入界面
流程：选文件 → 跑 OCR（后台线程）→ 显示草稿 → 人工校对 → 入库
"""
import os
import sys
import json
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit,
    QListWidget, QListWidgetItem, QSplitter, QMessageBox, QFileDialog,
    QProgressBar, QGroupBox, QLineEdit, QComboBox, QCheckBox,
    QWidget, QAbstractItemView, QSpinBox
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap, QPixmapCache

TYPE_LABELS = {'choice_single': '单选题', 'choice_multi': '多选题',
               'fill': '填空题', 'judgment': '判断题',
               'calculation': '计算题', 'experiment': '实验题'}


def _thumb(path, height=110):
    """取缩略图，带缓存。

    直接 QPixmap(path) + scaledToHeight 在 4000x3000 上要 ~180ms，
    切题时重复解码会卡。这里按「路径+修改时间+高度」缓存，
    文件改了就自动失效。
    """
    try:
        st = os.stat(path)
        key = 'pbt:%s:%d:%d:%d' % (path, int(st.st_mtime), st.st_size, height)
    except OSError:
        return None
    pm = QPixmapCache.find(key)
    if pm is not None:
        return pm
    src = QPixmap(path)
    if src.isNull():
        return None
    pm = src.scaledToHeight(height, Qt.SmoothTransformation)
    QPixmapCache.insert(key, pm)
    return pm


def _imports_root():
    """导入相关目录的根：跟随 db.ROOT。
    开发时 = 项目根；打包后 = 用户可写目录（%LOCALAPPDATA%\\PhysicsBank
    或 PHYSICS_BANK_HOME），绝不往 Program Files 里写。"""
    return os.path.join(db.ROOT, 'imports')


def _default_imports_dir():
    """导入文件对话框的默认目录；不存在则返回空串（交给 Qt 用 CWD）。"""
    d = _imports_root()
    return d if os.path.isdir(d) else ''


def _ocr_work_dir(src):
    """OCR 工作目录：<db.ROOT>\\imports\\_ocr_work\\<文件名>\\。
    目录不存在则创建，保证全新安装也能跑。"""
    base = os.path.splitext(os.path.basename(src))[0]
    d = os.path.join(_imports_root(), '_ocr_work', base)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


class PreviewWorker(QThread):
    """只渲染预览页，不跑 OCR —— 要快。"""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, src, work_dir, parent=None):
        super().__init__(parent)
        self.src = src
        self.work_dir = work_dir

    def run(self):
        try:
            import subprocess
            py = OcrWorker._find_ocr_python()
            if not py:
                self.failed.emit(
                    '没有找到 OCR 引擎，无法生成预览页。\n'
                    '（PDF 预览需要 OCR 环境里的 PyMuPDF）\n\n'
                    '当前查找过的目录：' +
                    '；'.join(OcrWorker._ocr_search_roots()))
                return
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'ocr_import.py')
            out_dir = os.path.join(self.work_dir, 'preview')
            proc = subprocess.run(
                [py, script, '--render', self.src, out_dir],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=os.path.dirname(script),
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            out = proc.stdout.decode('utf-8', 'replace').strip()
            if not out:
                self.failed.emit('预览生成失败：' +
                                 proc.stderr.decode('utf-8', 'replace')[:1200])
                return
            data = json.loads(out.splitlines()[-1])
            pages = [(p['page'], p['image']) for p in data['pages']]
            self.done.emit(pages)
        except Exception as e:
            self.failed.emit(f'{e}\n\n{traceback.format_exc()}')


class OcrWorker(QThread):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, src, work_dir, parent=None, qnum_cfg=None,
                 boundaries=None, enhance=None):
        super().__init__(parent)
        self.src = src
        self.work_dir = work_dir
        self.qnum_cfg = qnum_cfg or {}
        self.boundaries = boundaries or None
        self.enhance = enhance or {}

    @staticmethod
    def _ocr_search_roots():
        """OCR 解释器该去哪些目录找。

        ★ 打包后 __file__ 在 <exe>\\_internal\\app 下，直接拿它推目录会算成
          <exe>\\_internal —— 那下面根本没有 venv-ocr（4GB+ 不进安装包）。
          所以这里跟随 db.ROOT（launcher 已把打包版指到用户可写目录），
          外加 exe 所在目录，两边都覆盖。
        """
        roots = []
        try:
            roots.append(os.path.abspath(db.ROOT))
        except Exception:
            pass
        # exe 所在目录（打包版）；源码运行就是项目根，与 db.ROOT 一致
        roots.append(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        # 逐级往上再找两层，容忍 exe 放在 <root>\dist\PhysicsBank 这种布局
        base = roots[-1]
        for _ in range(2):
            base = os.path.dirname(base)
            if base and base not in roots:
                roots.append(base)
        # 去重保序
        out = []
        for r in roots:
            if r and r not in out:
                out.append(r)
        return out

    @staticmethod
    def _find_ocr_python():
        """找带 OCR 的 python 解释器。

        依次看：环境变量 -> PHYSICS_BANK_HOME/项目根下的 venv-ocr ->
        上溯目录的 venv-ocr -> runtime-ocr / ocr。
        都找不到返回 None（安装包默认不带 OCR 引擎，见 README）。
        """
        if os.environ.get('PHYSICS_BANK_NO_OCR') == '1':
            return None
        cands = []
        env = os.environ.get('PHYSICS_BANK_OCR_PYTHON')
        if env:
            cands.append(env)
        for root in OcrWorker._ocr_search_roots():
            cands += [
                os.path.join(root, 'venv-ocr', 'Scripts', 'python.exe'),
                os.path.join(root, 'runtime-ocr', 'python.exe'),
                os.path.join(root, 'runtime-ocr', 'Scripts', 'python.exe'),
                os.path.join(root, 'ocr', 'python.exe'),
                os.path.join(root, 'python-ocr', 'python.exe'),
            ]
        for c in cands:
            c = os.path.abspath(c)
            if os.path.isfile(c):
                return c
        return None

    def run(self):
        try:
            # OCR 必须在独立的 paddle 环境里跑（paddle 与 torch DLL 冲突），
            # 所以这里用子进程调用，而不是直接 import。
            # 打包版（精简版）不带 OCR，会提前告知用户。
            import subprocess
            py = self._find_ocr_python()
            if not py:
                self.failed.emit(
                    '没有找到 OCR 引擎（独立的 paddle 环境）。\n\n'
                    '安装包默认不带 OCR（含 paddle + CUDA，约 4.5GB）。\n'
                    '要启用扫码件/照片识别，任选一种：\n'
                    '  1) 把「OCR 引擎包」解压到程序目录旁边，\n'
                    '     使其成为 <程序目录>\\runtime-ocr\\python.exe\n'
                    '     （或 <程序目录>\\venv-ocr\\Scripts\\python.exe）\n'
                    '  2) 设置环境变量 PHYSICS_BANK_OCR_PYTHON 指向\n'
                    '     带 paddleocr 的 python.exe\n'
                    '  3) 用「Excel/CSV 批量导入」或手工录入。\n\n'
                    '当前查找过的目录：' +
                    '；'.join(OcrWorker._ocr_search_roots()))
                return
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'ocr_import.py')
            out_json = os.path.join(self.work_dir, 'draft.json')
            self.progress.emit('启动 OCR 引擎（首次加载模型约需 10-20 秒）...')

            # ★ 识别依据（哪些写法算题目开头）传给 OCR 子进程
            want = [k for k in ('plain', 'li', 'ti')
                    if self.qnum_cfg.get(k)]
            argv = [py, script, self.src, self.work_dir, out_json,
                    ','.join(want)]
            # ★ 人工分界：写成临时 JSON 交子进程
            if self.boundaries:
                bpath = os.path.join(self.work_dir, 'boundaries.json')
                with open(bpath, 'w', encoding='utf-8') as f:
                    json.dump({str(k): v for k, v in self.boundaries.items()}, f)
                argv.append(bpath)
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=os.path.dirname(script),
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8',
                     # ★ 版面增强开关（2026-10-22）：公式/表格/多栏
                     'PHYSICS_BANK_ENH_MATH':
                         '0' if self.enhance.get('no_math') else '1',
                     'PHYSICS_BANK_ENH_TABLE':
                         '0' if self.enhance.get('no_table') else '1',
                     'PHYSICS_BANK_ENH':
                         '0' if self.enhance.get('no_enhance') else '1'})
            for raw in iter(proc.stdout.readline, b''):
                try:
                    line = raw.decode('utf-8', 'replace').rstrip()
                except Exception:
                    continue
                if line:
                    self.progress.emit(line)
            proc.wait()

            if not os.path.exists(out_json):
                self.failed.emit('OCR 未产出结果文件，请查看日志')
                return
            with open(out_json, 'r', encoding='utf-8') as f:
                self.done.emit(json.load(f))
        except Exception as e:
            self.failed.emit(f'{e}\n\n{traceback.format_exc()}')


class WordWorker(QThread):
    """后台解析 .docx（python-docx 在主环境，不需子进程）"""
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, src, work_dir, parent=None, qnum_cfg=None):
        super().__init__(parent)
        self.src = src
        self.work_dir = work_dir
        self.qnum_cfg = qnum_cfg or {}

    def run(self):
        try:
            self.progress.emit('正在解析 Word 文档（公式 / 图片）...')
            import word_import
            d = word_import.run_word(
                self.src, self.work_dir, None,
                qnum_cfg=self.qnum_cfg, verbose=False)
            if not d.get('questions'):
                self.failed.emit(
                    '没从 Word 里解析出题目。可能原因：\n'
                    '  • 题号不是「1. / 2、/ 3．」等常见写法\n'
                    '  • 全文放在文本框 / 表格外框里（当前只读正文段与表格单元格）')
                return
            self.done.emit(d)
        except Exception as e:
            import traceback
            self.failed.emit(f'{e}\n\n{traceback.format_exc()}')


class ImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('批量导入（Word / PDF / 扫描件 / 照片）')
        self.resize(1220, 780)
        self.draft = None
        self.worker = None

        root = QVBoxLayout(self)

        # ---- 顶部：选文件 ----
        top = QGroupBox('导入源')
        tv = QVBoxLayout(top)
        tl = QHBoxLayout()
        self.e_src = QLineEdit()
        self.e_src.setPlaceholderText('选择 Word(.docx) / PDF / 图片文件...')
        b_pick = QPushButton('选择文件')
        b_pick.clicked.connect(self.pick_file)
        b_run = QPushButton('开始识别')
        b_run.clicked.connect(self.start_ocr)
        self.b_run = b_run
        b_manual = QPushButton('人工分界')
        b_manual.setToolTip('先预览各页，人工点出每题开始的位置，再按分界识别（推荐）')
        b_manual.clicked.connect(self.start_manual)
        self.b_manual = b_manual
        tl.addWidget(self.e_src)
        tl.addWidget(b_pick)
        tl.addWidget(b_manual)
        tl.addWidget(b_run)
        tv.addLayout(tl)

        # ★ 「题目开始点」识别依据（用户 2026-10-07 需求，可按文档自定义）
        ql = QHBoxLayout()
        ql.addWidget(QLabel('识别依据:'))
        self.cb_plain = QCheckBox('数字起头（1. / 2、/ 3．）')
        self.cb_plain.setChecked(True)
        self.cb_li = QCheckBox('「例」+数字（例题3.）')
        self.cb_li.setChecked(True)
        self.cb_ti = QCheckBox('「题」+数字（题5、）')
        self.cb_ti.setChecked(True)
        ql.addWidget(self.cb_plain)
        ql.addWidget(self.cb_li)
        ql.addWidget(self.cb_ti)
        ql.addStretch(1)
        lab_tip = QLabel('（点号后紧跟数字的小数如 3.62cm 永不当题号）')
        lab_tip.setStyleSheet('color:#888; font-size:11px;')
        ql.addWidget(lab_tip)
        tv.addLayout(ql)

        # ★ 版面增强开关（2026-10-22）：公式 / 表格 / 多栏
        el = QHBoxLayout()
        el.addWidget(QLabel('增强:'))
        self.cb_math = QCheckBox('公式识别（转 LaTeX）')
        self.cb_math.setChecked(True)
        self.cb_math.setToolTip('把含数学的行交给公式模型识别成 LaTeX；'
                                '偏慢（每页多 1-3 秒）')
        self.cb_table = QCheckBox('表格识别')
        self.cb_table.setChecked(True)
        self.cb_table.setToolTip('实验数据表转成结构化文本，不拆成散字')
        self.cb_cols = QCheckBox('双栏自动排序')
        self.cb_cols.setChecked(True)
        self.cb_cols.setToolTip('双栏试卷按栏读，避免两栏串行')
        for w in (self.cb_math, self.cb_table, self.cb_cols):
            el.addWidget(w)
        el.addStretch(1)
        tv.addLayout(el)
        root.addWidget(top)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setVisible(False)
        root.addWidget(self.bar)

        self.lab_log = QLabel('等待开始...')
        self.lab_log.setStyleSheet('color:#555; font-family:Consolas; font-size:11px;')
        root.addWidget(self.lab_log)

        # ---- 主体 ----
        split = QSplitter(Qt.Horizontal)

        # 左：题目列表
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('识别出的题目（勾选要入库的）:'))
        self.lst = QListWidget()
        self.lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lst.currentRowChanged.connect(self.on_pick)
        self.lst.itemChanged.connect(self.refresh_sel_label)
        ll.addWidget(self.lst)

        bl = QHBoxLayout()
        b_all = QPushButton('全选')
        b_all.clicked.connect(lambda: self.set_all_checked(True))
        b_none = QPushButton('全不选')
        b_none.clicked.connect(lambda: self.set_all_checked(False))
        b_low = QPushButton('只选高置信度')
        b_low.clicked.connect(self.select_high_conf)
        bl.addWidget(b_all)
        bl.addWidget(b_none)
        bl.addWidget(b_low)
        ll.addLayout(bl)
        split.addWidget(lw)

        # 右：校对编辑
        rw = QWidget()
        rl = QVBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)

        self.e_num = QLineEdit()
        self.e_num.setMaximumWidth(80)
        self.cb_type = QComboBox()
        for k, v in TYPE_LABELS.items():
            self.cb_type.addItem(v, k)
        self.sp_diff = QSpinBox()
        self.sp_diff.setRange(1, 5)
        self.sp_diff.setValue(3)
        self.sp_diff.setMaximumWidth(60)
        self.sp_diff.setToolTip('1 很容易 ~ 5 很难')
        row = QHBoxLayout()
        row.addWidget(QLabel('原题号:'))
        row.addWidget(self.e_num)
        row.addWidget(QLabel('题型:'))
        row.addWidget(self.cb_type)
        row.addWidget(QLabel('难度:'))
        row.addWidget(self.sp_diff)
        row.addStretch()
        rl.addLayout(row)

        rl.addWidget(QLabel('题干（可改写；公式用 $...$ 包住）:'))
        self.e_stem = QTextEdit()
        self.e_stem.setMaximumHeight(120)
        rl.addWidget(self.e_stem)

        rl.addWidget(QLabel('选项（每行一个，A. 内容）:'))
        self.e_opts = QTextEdit()
        self.e_opts.setMaximumHeight(110)
        rl.addWidget(self.e_opts)

        rl.addWidget(QLabel('答案（选择题填字母，如 B 或 AC）:'))
        self.e_ans = QLineEdit()
        self.e_ans.setPlaceholderText('例如：B、AC、12 Ω')
        rl.addWidget(self.e_ans)

        rl.addWidget(QLabel('解析（可留空；导入后也能在题库里补）:'))
        self.e_ansz = QTextEdit()
        self.e_ansz.setMaximumHeight(90)
        self.e_ansz.setPlaceholderText('解题思路 / 步骤')
        rl.addWidget(self.e_ansz)

        # 配图预览（Word 里抽出来的图，挂错位置时肉眼最好判断）
        self.lab_figcap = QLabel('配图:')
        rl.addWidget(self.lab_figcap)
        self.fig_box = QWidget()
        self.fig_lay = QHBoxLayout(self.fig_box)
        self.fig_lay.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.fig_box)

        b_apply = QPushButton('保存本题修改（Ctrl+Enter）')
        b_apply.setShortcut('Ctrl+Return')
        b_apply.clicked.connect(self.apply_current)
        rl.addWidget(b_apply)

        # 改过没保存就切题，静默丢失最气人。这里记脏。
        self._dirty = False
        for w in (self.e_stem, self.e_ansz):
            w.textChanged.connect(self._mark_dirty)
        for w in (self.e_ans, self.e_num):
            w.textChanged.connect(self._mark_dirty)
        self.sp_diff.valueChanged.connect(self._mark_dirty)
        self.cb_type.currentIndexChanged.connect(self._mark_dirty)

        rl.addWidget(QLabel('原始文本（只读，供对照）:'))
        self.e_raw = QTextEdit()
        self.e_raw.setReadOnly(True)
        self.e_raw.setStyleSheet('background:#f6f6f6; color:#666;')
        self.e_raw.setMaximumHeight(90)
        rl.addWidget(self.e_raw)

        split.addWidget(rw)
        split.setSizes([400, 800])
        root.addWidget(split, 1)

        # ---- 底部 ----
        bot = QHBoxLayout()
        self.cb_kp = QComboBox()
        self.cb_kp.addItem('（不指定知识点）', 0)
        for kp in db.list_kp():
            self.cb_kp.addItem(kp['name'], kp['id'])
        bot.addWidget(QLabel('统一知识点:'))
        bot.addWidget(self.cb_kp)
        bot.addStretch()
        self.lab_sel = QLabel('已勾选 0 题')
        bot.addWidget(self.lab_sel)
        b_import = QPushButton('导入勾选的题目')
        b_import.clicked.connect(self.do_import)
        b_import.setStyleSheet('font-weight:bold; padding:4px 12px;')
        bot.addWidget(b_import)
        b_close = QPushButton('关闭')
        b_close.clicked.connect(self.reject)
        bot.addWidget(b_close)
        root.addLayout(bot)

        self.lst.itemChanged.connect(self.refresh_sel_label)

    # ---------------- 文件 / OCR ----------------

    def pick_file(self):
        p, _ = QFileDialog.getOpenFileName(
            self, '选择导入文件', _default_imports_dir(),
            '可导入文件 (*.pdf *.docx *.png *.jpg *.jpeg *.bmp *.tif *.tiff);;'
            'Word 文档 (*.docx);;PDF (*.pdf);;'
            '图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;所有文件 (*)')
        if p:
            self.e_src.setText(p)
            # Word 不需要「识别依据 / 人工分界」，直接切掉这些按钮
            self._sync_mode_by_ext(p)

    def _sync_mode_by_ext(self, path=None):
        """根据文件类型切换界面：Word 走直读，不需要 OCR 那些选项。"""
        path = path or self.e_src.text().strip()
        is_word = path.lower().endswith('.docx')
        for w in (self.b_manual, getattr(self, 'b_run', None)):
            if w is not None:
                w.setVisible(not is_word or w is self.b_run)
        for w in (getattr(self, 'cb_plain', None), getattr(self, 'cb_li', None),
                  getattr(self, 'cb_ti', None)):
            if w is not None:
                w.setVisible(not is_word)
        if is_word and hasattr(self, 'lab_log'):
            self.lab_log.setText('Word 文档：直接解析段落/公式/图片，无需 OCR')

    # ---- 人工分界流程（用户 2026-10-07 需求）----
    def start_manual(self):
        """选文件 → 渲染预览页 → 人工点分界 → 按分界跑 OCR。"""
        src = self.e_src.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, '提示', '请先选择一个存在的文件')
            return
        work = _ocr_work_dir(src)
        os.makedirs(work, exist_ok=True)
        self.work_dir = work
        self.b_manual.setEnabled(False)
        self.bar.setVisible(True)
        self.lab_log.setText('正在生成预览页...')
        self._preview_worker = PreviewWorker(src, work, self)
        self._preview_worker.done.connect(self.on_preview_done)
        self._preview_worker.failed.connect(self.on_preview_failed)
        self._preview_worker.start()

    def on_preview_done(self, pages):
        self.bar.setVisible(False)
        self.b_manual.setEnabled(True)
        if not pages:
            QMessageBox.warning(self, '提示', '没能生成预览页')
            return
        from boundary_dialog import BoundaryDialog
        dlg = BoundaryDialog(pages, self)
        if dlg.exec():
            self._boundaries = dlg.result_map
            self.start_ocr(boundaries=self._boundaries)

    def on_preview_failed(self, msg):
        self.bar.setVisible(False)
        self.b_manual.setEnabled(True)
        QMessageBox.critical(self, '生成预览失败', msg[:1500])

    def start_ocr(self, boundaries=None):
        src = self.e_src.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, '提示', '请先选择一个存在的文件')
            return

        # ★ Word 文档：不走 OCR 子进程，直接本地解析 OOXML
        #   （公式取 OMML→LaTeX，图片按 rId 抽二进制）
        low = src.lower()
        if low.endswith('.docx'):
            return self.start_word(src)
        if low.endswith('.doc'):
            QMessageBox.warning(
                self, '不支持 .doc',
                '老版 .doc 是二进制格式，读不了。\n\n'
                '请用 Word / WPS 打开后「另存为」→ 选 .docx，再导入。')
            return

        base = os.path.splitext(os.path.basename(src))[0]
        work = _ocr_work_dir(src)
        self.work_dir = work
        os.makedirs(work, exist_ok=True)

        self.b_run.setEnabled(False)
        self.bar.setVisible(True)
        self.lst.clear()
        qnum_cfg = {'plain': self.cb_plain.isChecked(),
                    'li': self.cb_li.isChecked(),
                    'ti': self.cb_ti.isChecked()}
        # 人工分界时不需要「识别依据」（不看题号，按线切）
        if not boundaries and not any(qnum_cfg.values()):
            QMessageBox.warning(self, '提示',
                                '至少要勾选一种「识别依据」，否则找不出题目。')
            self.b_run.setEnabled(True)
            self.bar.setVisible(False)
            return
        self.worker = OcrWorker(src, work, self, qnum_cfg=qnum_cfg,
                                boundaries=boundaries,
                                enhance={
                                    'no_math': not self.cb_math.isChecked(),
                                    'no_table': not self.cb_table.isChecked(),
                                    'no_enhance': not (
                                        self.cb_math.isChecked()
                                        or self.cb_table.isChecked()
                                        or self.cb_cols.isChecked()),
                                })
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def start_word(self, src):
        """Word 导入：本地解析 OOXML，不用 paddle 子进程。"""
        work = _ocr_work_dir(src)
        self.work_dir = work
        os.makedirs(work, exist_ok=True)
        qnum_cfg = {'plain': self.cb_plain.isChecked(),
                    'li': self.cb_li.isChecked(),
                    'ti': self.cb_ti.isChecked()}
        if not any(qnum_cfg.values()):
            qnum_cfg = {'plain': True}
        self.b_run.setEnabled(False)
        self.bar.setVisible(True)
        self.lst.clear()
        self.worker = WordWorker(src, work, self, qnum_cfg=qnum_cfg)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_progress(self, line):
        self.lab_log.setText(line[:160])

    def on_failed(self, msg):
        self.bar.setVisible(False)
        self.b_run.setEnabled(True)
        self.lab_log.setText('识别失败')
        QMessageBox.critical(self, '导入失败', msg[:2000])

    def on_done(self, draft):
        self.bar.setVisible(False)
        self.b_run.setEnabled(True)
        self.draft = draft
        n = len(draft['questions'])
        kind = draft.get('kind')
        if kind == 'word':
            nfig = sum(len(q.get('figures') or []) for q in draft['questions'])
            self.lab_log.setText(
                f"Word 解析完成：{n} 道题，{nfig} 张图，"
                f"耗时 {draft['total_seconds']}s")
        else:
            self.lab_log.setText(
                f"识别完成：{len(draft['pages'])} 页，{n} 道题，"
                f"耗时 {draft['total_seconds']}s")
        self.populate()
        if n == 0:
            QMessageBox.information(
                self, '提示',
                '没有识别出题目。可能原因：图片是双栏排版被误判、'
                '题号格式特殊，或图片质量过低。\n'
                '原始识别文本可在草稿 JSON 里查看。')

    # ---------------- 填充 ----------------

    def _row_text(self, q, i):
        from ocr_import import guess_type
        t = guess_type(q)
        low = len(q.get('low_conf') or [])
        mark = f' ⚠{low}' if low else ''
        ans = f' ✓{q["answer"]}' if (q.get('answer') or '').strip() else ''
        return (f"[{i+1}] {TYPE_LABELS.get(t, t)}  "
                f"P{q.get('page','?')}  "
                f"{(q['stem'] or '(空)')[:38]}{mark}{ans}")

    def populate(self):
        self.lst.blockSignals(True)
        self.lst.clear()
        for i, q in enumerate(self.draft['questions']):
            from ocr_import import guess_type
            t = guess_type(q)
            low = len(q.get('low_conf') or [])
            txt = self._row_text(q, i)
            it = QListWidgetItem(txt)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked if low > 3 else Qt.Checked)
            it.setData(Qt.UserRole, i)
            if low:
                it.setForeground(Qt.darkRed)
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        if self.lst.count():
            self.lst.setCurrentRow(0)
        self.refresh_sel_label()

    def _show_figures(self, q):
        """清空并重建配图缩略图区。图挂错题时，肉眼一看就知道。"""
        while self.fig_lay.count():
            w = self.fig_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        figs = [f for f in (q.get('figures') or [])
                if f.get('path') and os.path.exists(f['path'])]
        if not figs:
            self.lab_figcap.setText('配图: 无')
            return
        self.lab_figcap.setText(f'配图: {len(figs)} 张（点开看大图）')
        for f in figs:
            w = QLabel()
            pm = _thumb(f['path'], 110)
            if pm is None or pm.isNull():
                w.setText('(读不出)')
            else:
                w.setPixmap(pm)
                w.setToolTip('%s\n%s' % (
                    os.path.basename(f['path']),
                    '未能自动归题，暂挂第一题' if f.get('unassigned') else ''))
                w.setCursor(Qt.PointingHandCursor)
                w.mousePressEvent = (
                    lambda ev, p=f['path']: self._open_figure(p))
            self.fig_lay.addWidget(w)
        self.fig_lay.addStretch(1)

    def _open_figure(self, path):
        dlg = QDialog(self)
        dlg.setWindowTitle(os.path.basename(path))
        v = QVBoxLayout(dlg)
        lab = QLabel()
        pm = QPixmap(path)
        # 大图按屏幕 80% 缩放，别让 4K 截图撑爆窗口
        scr = (self.screen().availableGeometry()
               if self.screen() else None)
        if scr is not None and not pm.isNull():
            w = int(scr.width() * 0.8)
            h = int(scr.height() * 0.8)
            if pm.width() > w or pm.height() > h:
                pm = pm.scaled(w, h, Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
        lab.setPixmap(pm)
        v.addWidget(lab)
        dlg.exec()

    def set_all_checked(self, on):
        self.lst.blockSignals(True)
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.lst.blockSignals(False)
        self.refresh_sel_label()

    def select_high_conf(self):
        self.lst.blockSignals(True)
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            q = self.draft['questions'][it.data(Qt.UserRole)]
            it.setCheckState(Qt.Unchecked if q['low_conf'] else Qt.Checked)
        self.lst.blockSignals(False)
        self.refresh_sel_label()

    def _summary(self):
        """勾选题目的自检摘要。"""
        qs = self.draft['questions'] if self.draft else []
        picked = [self.lst.item(i).data(Qt.UserRole)
                  for i in range(self.lst.count())
                  if self.lst.item(i).checkState() == Qt.Checked]
        sel = [qs[i] for i in picked]
        no_ans = sum(1 for q in sel if not (q.get('answer') or '').strip())
        low = sum(1 for q in sel if q.get('low_conf'))
        orphan = sum(1 for q in sel
                     for f in (q.get('figures') or []) if f.get('unassigned'))
        return len(sel), no_ans, low, orphan

    def refresh_sel_label(self, *_):
        n = sum(1 for i in range(self.lst.count())
                if self.lst.item(i).checkState() == Qt.Checked)
        if not n:
            self.lab_sel.setText('已勾选 0 题')
            return
        total, no_ans, low, orphan = self._summary()
        parts = [f'已勾选 {n} 题']
        if no_ans:
            parts.append(f'{no_ans} 题无答案')
        if low:
            parts.append(f'{low} 题有低置信')
        if orphan:
            parts.append(f'{orphan} 图未归题')
        self.lab_sel.setText('（' + '，'.join(parts) + '）')

    def _refresh_sel(self):
        self.refresh_sel_label()

    def _mark_dirty(self, *a):
        # 程序回填也会触发 textChanged，那不是用户改动
        if getattr(self, '_filling', False):
            return
        self._dirty = True

    def on_pick(self, row):
        if row < 0 or not self.draft:
            return
        # 上一题改过没保存？问一句再走
        if self._dirty and getattr(self, '_cur_idx', None) is not None:
            r = QMessageBox.question(
                self, '尚未保存',
                '刚才的修改还没保存，切换后就会丢失。\n\n要先保存吗？',
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                # 把列表选回原处，别让界面和实际状态错位
                self.lst.blockSignals(True)
                self.lst.setCurrentRow(self._last_row)
                self.lst.blockSignals(False)
                return
            if r == QMessageBox.Save:
                self._save_current(silent=True)
        self._dirty = False
        it = self.lst.item(row)
        idx = it.data(Qt.UserRole)
        q = self.draft['questions'][idx]
        self._cur_idx = idx
        self._last_row = row
        self._filling = True
        from ocr_import import guess_type
        self.e_num.setText(q['number'] or '')
        self.cb_type.setCurrentIndex(
            max(0, self.cb_type.findData(guess_type(q))))
        self.e_stem.setPlainText(q['stem'] or '')
        self.e_opts.setPlainText(
            '\n'.join(f"{o['label']}. {o['content']}" for o in q['options']))
        # ★ 答案 / 解析回填：旧版这里写死 setText('')，split_questions
        #   辛苦分离出来的答案全被丢掉，用户得逐题重敲。
        self.e_ans.setText(q.get('answer') or '')
        self.e_ansz.setPlainText(q.get('analysis') or '')
        self.sp_diff.setValue(int(q.get('difficulty') or 3))
        self.e_raw.setPlainText('\n'.join(q.get('raw') or []))
        self._show_figures(q)
        self._filling = False
        self._dirty = False

    def apply_current(self):
        self._save_current(silent=False)

    def _save_current(self, silent=False):
        row = self.lst.currentRow()
        if row < 0 or not self.draft:
            return
        idx = self.lst.item(row).data(Qt.UserRole)
        q = self.draft['questions'][idx]
        q['stem'] = self.e_stem.toPlainText().strip()
        opts = []
        for line in self.e_opts.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if len(line) > 1 and line[0] in 'ABCD' and line[1] in '.、．)）':
                opts.append({'label': line[0], 'content': line[2:].strip()})
            else:
                opts.append({'label': chr(65 + len(opts)), 'content': line})
        q['options'] = opts
        q['answer'] = self.e_ans.text().strip()
        q['analysis'] = self.e_ansz.toPlainText().strip()
        q['difficulty'] = self.sp_diff.value()
        q['_edited'] = True
        it = self.lst.item(row)
        it.setText(self._row_text(q, row) + ' ✓')
        self._dirty = False
        # 保存后自动跳到下一题，省一次点击
        if not silent:
            nxt = row + 1
            if nxt < self.lst.count():
                self.lst.setCurrentRow(nxt)
            else:
                self.lab_sel.setText('已是最后一题')

    # ---------------- 入库 ----------------

    def do_import(self):
        if not self.draft:
            QMessageBox.warning(self, '提示', '还没有识别结果')
            return
        picked = [self.lst.item(i).data(Qt.UserRole)
                  for i in range(self.lst.count())
                  if self.lst.item(i).checkState() == Qt.Checked]
        if not picked:
            QMessageBox.warning(self, '提示', '没有勾选任何题目')
            return
        total, no_ans, low, orphan = self._summary()
        warn = []
        if no_ans:
            warn.append(f'· {no_ans} 题没有答案，导入后要到题库里补')
        if low:
            warn.append(f'· {low} 题含低置信内容，需逐题校对')
        if orphan:
            warn.append(f'· {orphan} 张图没能自动归题，都挂在第 1 题上')
        if not warn:
            warn.append('· 未发现明显问题')
        tip = ('Word 解析结果建议先逐题校对公式与图片挂位。'
               if self.draft.get('kind') == 'word' else
               'OCR 结果可能含错字、错公式，建议先逐题校对。')
        if QMessageBox.question(
                self, '确认导入',
                f'把勾选的 {len(picked)} 道题写入题库？\n\n'
                + '\n'.join(warn) + '\n\n' + tip
                ) != QMessageBox.Yes:
            return

        kp_id = self.cb_kp.currentData() or 0
        srcname = self.draft['source_name']
        SRC_TAG = {'word': 'Word 导入'}.get(self.draft.get('kind'), 'OCR 导入')
        n_ok = 0
        n_fig = 0
        n_ans = 0
        for idx in picked:
            q = self.draft['questions'][idx]
            stem = (q['stem'] or '').strip()
            if not stem:
                continue
            from ocr_import import guess_type
            # 选项并入题干（新数据模型：选项就是题干里的若干行）
            if q.get('options'):
                opt_lines = [f"{o['label']}. {o['content']}".rstrip()
                             for o in q['options'] if (o.get('content') or '').strip()]
                if opt_lines:
                    stem = stem + '\n' + '\n'.join(opt_lines)
            # 答案/解析：优先用户刚在界面上改过的，其次自动分离的
            ans = (q.get('answer') or '').strip()
            ana = (q.get('analysis') or '').strip()
            # 选项里带的 is_correct 也算答案（OCR 有时把正确项标粗/勾）
            if not ans and q.get('options'):
                cor = [o['label'] for o in q['options'] if o.get('is_correct')]
                if cor:
                    ans = ''.join(sorted(cor))
            try:
                qid = db.add_question(
                    stem=stem,
                    answer=ans,
                    solution=ana,
                    question_type=guess_type(q),
                    difficulty=int(q.get('difficulty') or 3),
                    source=f'{srcname} ({SRC_TAG})',
                    source_page=q.get('page'),
                    grade='',
                    note=(f"导入，原题号 {q['number']}。"
                          + (f"低置信度 {len(q.get('low_conf') or [])} 处，需人工校对。"
                             if q.get('low_conf') else '')
                          + (f"含配图 {len(q.get('figures') or [])} 张。"
                             if q.get('figures') else '')),
                    options=None,
                    kp_ids=[kp_id] if kp_id else None)
                n_ok += 1
                if ans:
                    n_ans += 1
                # ★ 配图入库（2026-10-12）：OCR 阶段切出的插图作为附件挂到题上
                fign = 0
                for fig in (q.get('figures') or []):
                    p = fig.get('path')
                    if p and os.path.exists(p):
                        try:
                            db.add_attachment(qid, p, role='stem_figure',
                                              field='stem', anchor_offset=-1)
                            fign += 1
                        except Exception as fe:
                            print('配图入库失败:', fe)
                if fign:
                    n_fig += fign
            except Exception as e:
                QMessageBox.warning(self, '导入出错', f'第{idx+1}题: {e}')

        msg = f'成功导入 {n_ok} 道题。'
        if n_ans:
            msg += f'\n其中 {n_ans} 题带答案。'
        if n_ans < n_ok:
            msg += f'\n还有 {n_ok - n_ans} 题没答案，可到主界面补。'
        if n_fig:
            msg += f'\n配图 {n_fig} 张已挂到对应题。'
        msg += f'\n\n来源标记为「{SRC_TAG}」，可在题库里筛出来统一校对。'
        QMessageBox.information(self, '导入完成', msg)
        if n_ok:
            self.accept()
