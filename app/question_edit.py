"""
question_edit.py — 题目录入 / 编辑对话框

设计要点（2026-09-24 调整）：
  * 选项不再单独录入 —— 选择题的 A/B/C/D 直接写进「题干」文本框。
    系统用 db.split_stem_options() 自动认出选项行，导出时按选项排版。
  * 题干支持插图：工具栏「插入图片」或直接 Ctrl+V 粘贴截图/图片，
    图片作为附件入库存相对路径，导出 Word/PDF/PPT 时随题干渲染。
  * 「答案」框填 A/B/C/D（可 'AC' / 'A、C'），系统据此给选项行加标记。
  * 属性（题型/难度/来源/年级/备注/知识点）已并入「题目内容」页，
    不再单独占标签页；原「页码」字段按需求移除（不入库）。
  * 知识点支持在本对话框内直接新建；「管理...」按钮打开完整管理窗口。
  * 布局（2026-09-24 再调）：取消标签页，改为左右分列 ——
    左侧 3/4 放题干 + 答案 + 解析，右侧 1/4 放属性 + 知识点 + 配图。
"""
import os
import shutil
import tempfile
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QTextEdit, QComboBox, QSpinBox, QPushButton, QLabel, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QGroupBox,
    QScrollArea, QWidget, QSizePolicy, QSplitter, QTextBrowser,
    QAbstractItemView, QDialogButtonBox, QApplication
)
from PySide6.QtCore import Qt, QMimeData, QUrl, QTimer, QSize, QEvent, QPoint
from PySide6.QtGui import (QPixmap, QImage, QGuiApplication, QKeySequence,
                           QShortcut, QTextDocument, QTextCharFormat, QColor,
                           QFont, QTextCursor, QIcon, QPainter, QWheelEvent)

import re

import db
import formula as _formula
from formula_edit import FormulaInput

IMG_FILTER = ('图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*)')


def _fit_size(w, h, margin=60):
    """把期望尺寸夹到当前屏幕可用区域内，避免对话榆超出屏幕。"""
    scr = QGuiApplication.primaryScreen()
    if scr is None:
        return w, h
    g = scr.availableGeometry()
    return (min(w, max(560, g.width() - margin)),
            min(h, max(460, g.height() - margin)))


class StemRenderPreview(QWidget):
    """把文本框里的 $...$ 公式渲染成真图，拼回文字里显示。

    ★ 为什么不做成「就地替换」：
      实测 Qt 里图片必须在文档中占一个 U+FFFC 真字符位置。
      用 QTextLayout.setFormats + ImageObject 试图只在绘制层替换，
      结果是文本被抹掉、图没画出来（实测深色像素 726 -> 0）。
      所以正确做法是：编辑框保持纯文本，另开一块只读区做真渲染。
      这样光标、换行、撤销全不受影响，而公式就在眼前。
    """

    def __init__(self, source, placeholder='（输入含 $公式$ 的内容后，这里显示排版效果）'):
        super().__init__()
        self.source = source
        self._placeholder = placeholder
        self._paths = {}                 # 缓存： (公式, 颜色) -> png 路径

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        lbl = QLabel('排版预览')
        lbl.setStyleSheet('color:#666; font-size:11px;')
        head.addWidget(lbl)
        head.addStretch()
        self.lbl_info = QLabel('')
        self.lbl_info.setStyleSheet('color:#999; font-size:11px;')
        head.addWidget(self.lbl_info)
        lay.addLayout(head)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setStyleSheet(
            'QTextBrowser{background:#fcfcfd; border:1px solid #ddd;'
            ' border-radius:3px; font-size:12px;}')
        self.view.setMinimumHeight(52)
        self.view.setMaximumHeight(150)
        lay.addWidget(self.view)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(450)
        self._timer.timeout.connect(self.refresh)
        self.source.textChanged.connect(self._timer.start)
        self.refresh()

    def _img_for(self, tex):
        key = (tex, '#111111')
        if key in self._paths:
            return self._paths[key]
        p = _formula.render_formula(tex, fontsize=16, dpi=160)
        self._paths[key] = p
        return p

    def refresh(self):
        text = self.source.toPlainText().replace('\ufffc', '')
        if not text.strip():
            self.view.setHtml(
                '<div style="color:#aaa">%s</div>' % self._placeholder)
            self.lbl_info.setText('')
            return

        import re as _re
        import html as _html
        pat = _re.compile(r'\$([^$\n]+)\$')
        parts = []
        pos = 0
        n_ok = 0
        n_fail = 0
        for m in pat.finditer(text):
            head = text[pos:m.start()]
            if head:
                parts.append(self._fmt_plain(head))
            tex = m.group(1).strip()
            png = self._img_for(tex) if tex else None
            if png:
                n_ok += 1
                parts.append('<img src="%s" style="vertical-align:middle">'
                             % png.replace('\\', '/'))
            else:
                n_fail += 1
                parts.append('<span style="color:#c0392b;'
                             'background:#fff4f4">%s</span>'
                             % _html.escape('$%s$' % tex))
            pos = m.end()
        tail = text[pos:]
        if tail:
            parts.append(self._fmt_plain(tail))

        self.view.setHtml('<div style="line-height:1.9">%s</div>' % ''.join(parts))
        if n_fail:
            self.lbl_info.setText('%d 个公式渲染失败' % n_fail)
            self.lbl_info.setStyleSheet('color:#c0392b; font-size:11px;')
        elif n_ok:
            self.lbl_info.setText('%d 个公式' % n_ok)
            self.lbl_info.setStyleSheet('color:#2b7a3d; font-size:11px;')
        else:
            self.lbl_info.setText('')

    @staticmethod
    def _fmt_plain(s):
        import html as _html
        return ('<span style="white-space:pre-wrap">%s</span>'
                % _html.escape(s))


def _one_line(s, n=40):
    """题干压成单行，用于列表展示。"""
    return ' '.join((s or '').split())[:n]


class ImageTextEdit(QTextEdit):
    """题干输入框：支持 Ctrl+V 粘贴图片 + Ctrl+V 粘贴文件路径。

    粘贴的位图会先落到临时文件，通过 image_added 信号抛出真实路径，
    由对话框统一登记进 _pending_attachments（保存时才入库）。

    为了导出时「图文位置对得上」，插图时会在文本里留下一个**不可见锚点字符**
    （U+FFFC OBJECT REPLACEMENT CHARACTER）。它对用户完全隐形，但能记账：
    第几个字符前有一张图。保存时换算成 anchor_offset 存库；
    OCR / 粘贴进来的历史 U+FFFC 会在保存前剥掉，不会误判成锚点。
    """

    ANCHOR = '\ufffc'          # 图片锚点（不可见）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tmpdir = tempfile.mkdtemp(prefix='qimg_')
        self._seq = 0
        self.image_added = None          # 由外部赋值为 callable(path)
        self.setAcceptDrops(True)

    # ---- 粘贴 ----
    def canInsertFromMimeData(self, src):
        if src.hasImage() or src.hasUrls():
            return True
        return super().canInsertFromMimeData(src)

    def insertFromMimeData(self, src):
        # 1) 真·位图（截图工具、从浏览器复制图片）
        if src.hasImage():
            img = QImage(src.imageData())
            if not img.isNull():
                path = self._save_image(img)
                if path:
                    self._emit_image(path)
                    return
        # 2) 文件 URL（从资源管理器拖/拷文件）
        if src.hasUrls():
            handled = False
            for url in src.urls():
                p = url.toLocalFile()
                if p and os.path.isfile(p):
                    if os.path.splitext(p)[1].lower() in (
                            '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'):
                        self._emit_image(p)
                        handled = True
                    else:
                        self.insertPlainText(p)
                        handled = True
            if handled:
                return
        super().insertFromMimeData(src)

    # ---- 拖拽 ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasImage() or e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e):
        self.insertFromMimeData(e.mimeData())
        e.acceptProposedAction()

    def _save_image(self, img):
        self._seq += 1
        p = os.path.join(self._tmpdir, f'paste_{self._seq}.png')
        try:
            if img.save(p, 'PNG'):
                return p
        except Exception:
            pass
        return None

    def _emit_image(self, path):
        if callable(self.image_added):
            self.image_added(path)
        else:
            self._insert_thumb(path)

    def _insert_thumb(self, path, max_w=280):
        """把一个缩略图**预览行**插到当前位置（不改动正文文字）。

        只用于纯视觉反馈：图上不参与正文，不影响任何字符下标。
        真正的“图在哪”靠 `_add_image_from_path` 写的隐形锚点。
        """
        try:
            img = QImage(path)
            if img.isNull():
                return
            if img.width() > max_w:
                img = img.scaledToWidth(max_w, Qt.SmoothTransformation)
            cur = self.textCursor()
            cur.insertBlock()
            self.document().addResource(
                QTextDocument.ImageResource, QUrl.fromLocalFile(path), img)
            cur.insertImage(img, 'fig')
            cur.insertBlock()
            self.setTextCursor(cur)
        except Exception:
            pass

    # ---- 锚点换算 ----
    @staticmethod
    def strip_anchors(text):
        """剥掉全部锚点字符（以及历史图留下的 U+FFFC）。

        OCR / 网页粘贴常带 U+FFFC，不能当成锚点，否则位置全错。
        """
        return (text or '').replace(ImageTextEdit.ANCHOR, '')

    def anchor_offsets(self):
        """列出本框内每个锚点在**纯文本**中的下标（升序）。

        只统计锚点字符自身，而不是 QTextDocument 里可能出现的其它
        U+FFFC（历史二进制插图），后者会被剥掉且不计入。
        """
        raw = self.toPlainText()
        return [i for i, ch in enumerate(raw) if ch == self.ANCHOR]

    # ---- 公式所见即所得 ----
    def enable_formula_wysiwyg(self, delay_ms=600):
        """开启 $...$ 的彩色高亮预览（只读美化，不改文本内容）。

        真正的“渲染成图”会打乱编辑体验（光标/换行），所以采用：
        把 $...$ 片段染成靛蓝色并换上等宽字体，一眼能分清哪些是公式；
        完整效果靠下方「公式速编」的实时预览框看。
        """
        self._need_fmt = False
        self._fmt_timer = QTimer(self)
        self._fmt_timer.setSingleShot(True)
        self._fmt_timer.setInterval(delay_ms)
        self._fmt_timer.timeout.connect(self._apply_formula_format)
        self.textChanged.connect(self._queue_format)

    def _queue_format(self):
        self._fmt_timer.start()

    def _apply_formula_format(self):
        """重扫全文，把 $...$ 内的片段上色。用 ExtraSelection，不污染文本。"""
        import re as _re
        text = self.toPlainText()
        sels = []
        pat = _re.compile(r'\$[^$\n]+\$')
        for m in pat.finditer(text):
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setForeground(QColor('#2b4fa8'))
            fmt.setFontWeight(QFont.Bold)
            fmt.setBackground(QColor('#eef3ff'))
            sel.format = fmt
            cur = QTextCursor(self.document())
            cur.setPosition(m.start())
            cur.setPosition(m.end(), QTextCursor.KeepAnchor)
            sel.cursor = cur
            sels.append(sel)
        self.setExtraSelections(sels)


class DuplicateDialog(QDialog):
    """查重提示框：列出库里相同/相似的题，让用户二次确认。

    左右对照 —— 左边是本次要存的，右边是库里命中的，
    带相似度条，方便一眼判断是不是同一道。
    """

    def __init__(self, hits, exact=False, parent=None):
        super().__init__(parent)
        self.hits = hits
        self.setWindowTitle('发现重复题目' if exact else '发现相似题目')
        self.resize(980, 620)

        root = QVBoxLayout(self)

        tip = QLabel()
        if exact:
            tip.setText(
                '<b style="color:#c0392b;font-size:13px">⚠ 题库里已经有一道<b>完全一样</b>的题</b>'
                '<br>请确认是不是重复录入。如果确实是不同的题（比如换了数字/情境），'
                '点「仍然保存」继续。')
        else:
            tip.setText(
                '<b style="color:#b9770e;font-size:13px">⚠ 题库里有相似的题</b>'
                '<br>下面列出来了，你对比一下。如果只是普通的相关题，'
                '点「仍然保存」继续即可。')
        tip.setWordWrap(True)
        root.addWidget(tip)

        body = QHBoxLayout()
        root.addLayout(body, 1)

        # 左：本题
        lb = QGroupBox('本次要保存的题')
        lv = QVBoxLayout(lb)
        self.new_view = QTextBrowser()
        self.new_view.setStyleSheet('font-size:13px;background:#fff;')
        lv.addWidget(self.new_view)
        body.addWidget(lb, 1)

        # 右：命中列表 + 对照
        rb = QGroupBox('题库中命中')
        rv = QVBoxLayout(rb)
        self.list = QListWidget()
        self.list.setMaximumWidth(300)
        for h in hits:
            tag = '完全相同' if h['kind'] == 'exact' else '相似 %.0f%%' % (h['score'] * 100)
            it = QListWidgetItem('【%s】#%d  %s' % (
                tag, h['id'], _one_line(h['stem'])[:18]))
            it.setData(Qt.UserRole, h)
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self._show_existing)
        rv.addWidget(self.list)

        self.old_view = QTextBrowser()
        self.old_view.setStyleSheet('font-size:13px;background:#fff;')
        rv.addWidget(self.old_view, 1)
        body.addWidget(rb, 1)

        body.setStretch(0, 1)
        body.setStretch(1, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        self.b_modify = QPushButton('返回修改')
        self.b_modify.setDefault(True)
        self.b_modify.clicked.connect(self.reject)
        btns.addWidget(self.b_modify)
        self.b_keep = QPushButton('仍然保存')
        self.b_keep.clicked.connect(self.accept)
        btns.addWidget(self.b_keep)
        root.addLayout(btns)

        stem = (parent.t_stem.toPlainText().replace('\ufffc', '')
                if parent and hasattr(parent, 't_stem') else '')
        self.new_view.setPlainText(stem)

    def _show_existing(self, row):
        if row < 0 or row >= len(self.hits):
            self.old_view.clear()
            return
        h = self.hits[row]
        txt = (h['stem'] or '')
        if h['answer']:
            txt += '\n\n【答案】' + h['answer']
        if h['source']:
            txt += '\n【来源】' + h['source']
        self.old_view.setPlainText(txt)


class ImagePreviewDialog(QDialog):
    """大图预览：滚轮缩放 + 拖拽平移 + 适应窗口。

    必须 WA_DeleteOnClose —— 反复双击看图会稳定泄漏内存。
    """

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle('图片预览 — %s' % os.path.basename(path))
        self.resize(900, 680)
        self._path = path
        self._scale = 1.0
        self._origin = QPixmap(path)

        lay = QVBoxLayout(self)
        self.canvas = QLabel()
        self.canvas.setAlignment(Qt.AlignCenter)
        self.canvas.setStyleSheet('background:#2b2b2b;')
        self.canvas.setMinimumSize(400, 300)
        lay.addWidget(self.canvas, 1)

        bar = QHBoxLayout()
        self.lab_info = QLabel()
        bar.addWidget(self.lab_info)
        bar.addStretch()
        b_fit = QPushButton('适应窗口')
        b_fit.clicked.connect(self.fit)
        b_100 = QPushButton('100%')
        b_100.clicked.connect(lambda: self.set_scale(1.0))
        b_in = QPushButton('放大')
        b_in.clicked.connect(lambda: self.set_scale(self._scale * 1.25))
        b_out = QPushButton('缩小')
        b_out.clicked.connect(lambda: self.set_scale(self._scale / 1.25))
        for b in (b_fit, b_100, b_out, b_in):
            bar.addWidget(b)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        bb.accepted.connect(self.close)
        bar.addWidget(bb)
        lay.addLayout(bar)

        self.setToolTip('滚轮缩放')
        self.fit()

    def fit(self):
        if self._origin.isNull():
            return
        w = max(1, self.canvas.width() - 8)
        h = max(1, self.canvas.height() - 8)
        s = min(w / self._origin.width(), h / self._origin.height())
        self.set_scale(min(s, 1.0))

    def set_scale(self, s):
        s = max(0.05, min(12.0, s))
        self._scale = s
        pm = self._origin.scaled(
            max(1, int(self._origin.width() * s)),
            max(1, int(self._origin.height() * s)),
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.canvas.setPixmap(pm)
        self.lab_info.setText('%d × %d 像素   缩放 %d%%' % (
            self._origin.width(), self._origin.height(), int(s * 100)))

    def wheelEvent(self, e: QWheelEvent):
        d = e.angleDelta().y()
        if d:
            self.set_scale(self._scale * (1.15 if d > 0 else 1 / 1.15))
        e.accept()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(30, self.fit)


class QuestionEditDialog(QDialog):
    def __init__(self, parent=None, qid=None):
        super().__init__(parent)
        self.qid = qid
        self.setWindowTitle('编辑题目' if qid else '新建题目')
        # 先按屏幕可用区域定尺寸：左栏内容实测约 1459px 高，
        # 在 900p/768p 屏上会被切掉且无法够到（左栏已加滚动区）。
        self.resize(*_fit_size(1240, 900))

        self._pending_attachments = []   # 新选的图片路径（保存时入库）
        self._existing_attachments = []
        self._orphan_rels = set()        # 磁盘上捡回来的孤儿图（当成新图重入库）
        self._drag_data = None           # 缩略图拖拽起手的项
        self._img_field = {}             # 草稿图路径 -> 'stem'/'solution'
        self._stem_anchor_marks = []
        self._sol_anchor_marks = []

        root = QVBoxLayout(self)

        # 左右分列：左 3/4 题干 + 答案解析，右 1/4 属性
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        # ================= 左：题干 + 答案 + 解析 =================
        # 左栏内容天生很高（题干+预览+速编+答案解析+预览 ≈ 1459px），
        # 小屏上放不下。包一层滚动区，任何模块都够得到。
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 6, 0)

        ll.addWidget(QLabel(
            '题干 —— 选择题的选项直接接在后面写，每行一个，如「A. 选项一」；\n'
            '系统会自动识别选项并在导出时排版。（也支持 A、 / (A) / A： 等写法）'))

        self.t_stem = ImageTextEdit()
        self.t_stem.setMinimumHeight(220)
        self.t_stem.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.t_stem.image_added = self._add_image_from_path
        self.t_stem.installEventFilter(self)
        self.t_stem.enable_formula_wysiwyg()
        self.t_stem.setPlaceholderText(
            '题干。文字直接写；公式用 $...$ 包裹，如：物体受力 $F = ma$\n'
            '选项接在后面：\n'
            'A. 加速度恒定\n'
            'B. 速度恒定\n'
            '\n'
            '💡 可直接 Ctrl+V 粘贴截图 / 图片，或点下方「插入图片」')
        ll.addWidget(self.t_stem, 5)

        # 题干公式排版预览（常驻，实时）
        self.prev_stem = StemRenderPreview(
            self.t_stem, '（在题干里写 $公式$ 后，这里显示排版效果）')
        ll.addWidget(self.prev_stem)

        # 题干工具按钮
        tb = QHBoxLayout()
        b_img = QPushButton('插入图片...')
        b_img.setToolTip('把图片作为配图加到本题（导出时随题干渲染）')
        b_img.clicked.connect(self._pick_stem_image)
        tb.addWidget(b_img)
        tb.addWidget(QLabel('  粘贴截图：在文本框里按 Ctrl+V   ·   公式速编见下方'))
        tb.addStretch()
        ll.addLayout(tb)

        # 实时选项识别预览
        opt_box = QGroupBox('已识别的选项（自动，只读）')
        opt_box.setMaximumHeight(120)
        ol = QVBoxLayout(opt_box)
        self.opt_preview = QLabel('（还没有识别到选项行）')
        self.opt_preview.setWordWrap(True)
        self.opt_preview.setStyleSheet('color:#555;')
        self.opt_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.opt_preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        ol.addWidget(self.opt_preview)
        ll.addWidget(opt_box)
        self.t_stem.textChanged.connect(self._refresh_opt_preview)

        # 题干/解析 公式速编
        stem_formula_box = QGroupBox('公式速编（编辑单个公式并插入到光标处）')
        sf = QVBoxLayout(stem_formula_box)
        self.stem_formula = FormulaInput()
        sf.addWidget(self.stem_formula)
        sfb = QHBoxLayout()
        btn_ins1 = QPushButton('插入到题干光标处')
        btn_ins1.clicked.connect(self._insert_stem_formula)
        btn_ins2 = QPushButton('插入到解析光标处')
        btn_ins2.clicked.connect(self._insert_solution_formula)
        sfb.addWidget(btn_ins1)
        sfb.addWidget(btn_ins2)
        sfb.addStretch()
        sf.addLayout(sfb)
        ll.addWidget(stem_formula_box)

        # ---- 答案 ----
        ans_box = QGroupBox('答案')
        al = QVBoxLayout(ans_box)
        self.t_answer = QTextEdit()
        self.t_answer.setMaximumHeight(64)
        self.t_answer.setPlaceholderText(
            '选择题填选项字母，如 A 或 AC / A、C；判断题填 √ 或 ×；'
            '其他题型填答案文本或 $LaTeX$')
        al.addWidget(self.t_answer)
        ll.addWidget(ans_box)
        self.t_answer.textChanged.connect(self._refresh_opt_preview)

        # ---- 解析 ----
        sol_box = QGroupBox('解析')
        sl = QVBoxLayout(sol_box)
        self.t_solution = ImageTextEdit()
        self.t_solution.setMinimumHeight(140)
        self.t_solution.setPlaceholderText(
            '解析（可含 LaTeX，也可直接 Ctrl+V 粘贴图片 / 拖入图片文件）')
        self.t_solution.image_added = self._add_image_from_path
        self.t_solution.installEventFilter(self)
        self.t_solution.enable_formula_wysiwyg()
        sl.addWidget(self.t_solution)

        # 解析公式排版预览
        self.prev_sol = StemRenderPreview(
            self.t_solution, '（解析里写 $公式$ 后，这里显示排版效果）')
        sl.addWidget(self.prev_sol)
        ll.addWidget(sol_box, 4)

        # 收尾：撑住底部，滚动时最末模块不会贴边
        ll.addStretch()

        left_scroll = QScrollArea()
        left_scroll.setWidget(left)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 滚动区自己别要求那么高，否则照样撑爆窗口
        left_scroll.setMinimumWidth(380)
        split.addWidget(left_scroll)

        # ================= 右：属性 =================
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        right.setMinimumWidth(240)

        attr_box = QGroupBox('题目属性')
        af = QFormLayout(attr_box)
        af.setVerticalSpacing(8)

        self.c_type = QComboBox()
        self.c_type.addItems(['choice_single', 'choice_multi', 'fill',
                              'judgment', 'calculation', 'experiment'])
        self.type_labels = {
            'choice_single': '单选题', 'choice_multi': '多选题',
            'fill': '填空题', 'judgment': '判断题',
            'calculation': '计算题', 'experiment': '实验题'}
        for i in range(self.c_type.count()):
            k = self.c_type.itemText(i)
            self.c_type.setItemText(i, self.type_labels.get(k, k))
        af.addRow('题型:', self.c_type)

        self.s_diff = QSpinBox()
        self.s_diff.setRange(1, 5)
        self.s_diff.setValue(3)
        self.s_diff.setToolTip('1=最容易，5=最难')
        af.addRow('难度:', self.s_diff)

        self.c_grade = QComboBox()
        self.c_grade.addItems(['', '高一', '高二', '高三'])
        af.addRow('年级:', self.c_grade)

        self.e_source = QLineEdit()
        self.e_source.setPlaceholderText('如：2023级校际联考(二)')
        af.addRow('来源:', self.e_source)

        self.e_note = QLineEdit()
        self.e_note.setPlaceholderText('可空')
        af.addRow('备注:', self.e_note)

        rl.addWidget(attr_box)

        # ---- 知识点（可多选 + 现场增改） ----
        kp_box = QGroupBox('知识点（可多选）')
        kl = QVBoxLayout(kp_box)

        self.kp_filter = QLineEdit()
        self.kp_filter.setPlaceholderText('搜索知识点…')
        self.kp_filter.textChanged.connect(self._filter_kp)
        kl.addWidget(self.kp_filter)

        self.kp_list = QListWidget()
        self.kp_list.setSelectionMode(QListWidget.MultiSelection)
        self.kp_list.setMinimumHeight(120)
        kl.addWidget(self.kp_list, 1)

        kpb = QHBoxLayout()
        kpb.setSpacing(4)
        b_kp_new = QPushButton('新建')
        b_kp_new.setToolTip('新建知识点并勾选到本题')
        b_kp_new.clicked.connect(self._new_kp)
        b_kp_ren = QPushButton('改名')
        b_kp_ren.setToolTip('重命名当前高亮的知识点')
        b_kp_ren.clicked.connect(self._rename_kp)
        b_kp_del = QPushButton('删除')
        b_kp_del.setToolTip('删除当前高亮的知识点（会解除与所有题目的关联）')
        b_kp_del.clicked.connect(self._delete_kp)
        b_kp_mgr = QPushButton('管理…')
        b_kp_mgr.setToolTip('打开完整的知识点管理窗口')
        b_kp_mgr.clicked.connect(self._manage_kp)
        for b in (b_kp_new, b_kp_ren, b_kp_del, b_kp_mgr):
            b.setMinimumWidth(0)
            kpb.addWidget(b)
        kl.addLayout(kpb)
        rl.addWidget(kp_box, 1)

        # ---- 配图（缩略图墙 + 拖拽排序 + 大图预览）----
        fig_box = QGroupBox('配图')
        fl = QVBoxLayout(fig_box)

        fab = QHBoxLayout()
        b_add = QPushButton('添加图片…')
        b_add.setToolTip('选图后插到光标处（题干或解析），导出时按这个位置排')
        b_add.clicked.connect(self._add_images)
        b_del = QPushButton('移除')
        b_del.clicked.connect(self._remove_image)
        fab.addWidget(b_add)
        fab.addWidget(b_del)
        fab.addStretch()
        fl.addLayout(fab)

        hint = QLabel('拖动可调顺序；双击看大图')
        hint.setStyleSheet('color:#888; font-size:11px;')
        fl.addWidget(hint)

        # 缩略图墙：QListWidget 的 IconMode，拖拽内部重排
        self.img_list = QListWidget()
        self.img_list.setViewMode(QListWidget.IconMode)
        self.img_list.setIconSize(QSize(72, 72))
        self.img_list.setGridSize(QSize(84, 96))
        self.img_list.setResizeMode(QListWidget.Adjust)
        self.img_list.setMovement(QListWidget.Static)
        self.img_list.setDragDropMode(QListWidget.InternalMove)
        self.img_list.setDefaultDropAction(Qt.MoveAction)
        self.img_list.setWordWrap(True)
        self.img_list.setMinimumHeight(210)
        self.img_list.setStyleSheet(
            'QListWidget::item{border:1px solid #ddd; border-radius:4px;'
            ' background:#fdfdfd;}'
            'QListWidget::item:selected{border:2px solid #2b6cb0;'
            ' background:#eaf2fb;}')
        self.img_list.currentRowChanged.connect(self._preview_image)
        self.img_list.itemDoubleClicked.connect(self._show_big_preview)
        # 拖完必须把新顺序同步到文本框里的占位符，否则导出跟看到的对不上
        self.img_list.model().rowsMoved.connect(self._on_rows_moved)
        # 用 Qt 自带的 InternalMove，但在起拖前记下拖的是谁
        self.img_list.installEventFilter(self)
        fl.addWidget(self.img_list, 1)

        self.img_preview = QLabel('选中图片预览')
        self.img_preview.setAlignment(Qt.AlignCenter)
        self.img_preview.setMinimumHeight(130)
        self.img_preview.setStyleSheet('background:#fafafa; border:1px solid #ddd;')
        fl.addWidget(self.img_preview, 2)

        rl.addWidget(fig_box, 2)
        rl.addStretch()

        # 右栏最小高 835px（题目属性 182 + 知识点 213 + 配图 428）——
        # 比小屏还高，同样包滚动区，否则配图区被切且无滚动条。
        right_scroll = QScrollArea()
        right_scroll.setWidget(right)
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QScrollArea.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        split.addWidget(right_scroll)
        # 右栏尺寸提示会把它撑宽（知识点/配图列表天生要宽），
        # 钉死右栏宽度区间，保证左栏拿到 3/4。
        right_scroll.setMinimumWidth(240)
        right_scroll.setMaximumWidth(340)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([900, 300])

        # ================= 底部按钮 =================
        brow2 = QHBoxLayout()
        brow2.addStretch()
        b_save = QPushButton('保存')
        b_save.setDefault(True)
        b_save.clicked.connect(self._save)
        b_cancel = QPushButton('取消')
        b_cancel.clicked.connect(self.reject)
        brow2.addWidget(b_save)
        brow2.addWidget(b_cancel)
        root.addLayout(brow2)

        self._reload_kp_list()

        if qid:
            self._load(qid)
        self._refresh_opt_preview()

    # ---------------- 知识点 ----------------

    def _reload_kp_list(self, keep_ids=None):
        """重填知识点列表。keep_ids=None 时保留当前勾选。"""
        if keep_ids is None:
            keep_ids = {it.data(Qt.UserRole) for it in self.kp_list.selectedItems()}
        # 记住当前高亮项，尽量保持
        cur = self.kp_list.currentItem()
        cur_id = cur.data(Qt.UserRole) if cur else None

        self.kp_list.clear()
        for kp in db.list_kp():
            it = QListWidgetItem(kp['name'])
            it.setData(Qt.UserRole, kp['id'])
            self.kp_list.addItem(it)
            if kp['id'] in keep_ids:
                it.setSelected(True)
        # 恢复高亮
        if cur_id is not None:
            for i in range(self.kp_list.count()):
                if self.kp_list.item(i).data(Qt.UserRole) == cur_id:
                    self.kp_list.setCurrentRow(i)
                    break
        self._filter_kp(self.kp_filter.text())

    def _filter_kp(self, text):
        text = (text or '').strip().lower()
        for i in range(self.kp_list.count()):
            it = self.kp_list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def _new_kp(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, '新建知识点', '名称:')
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        kp_id = db.add_kp(name)
        if kp_id is None:
            QMessageBox.warning(self, '重名', f'已存在知识点「{name}」')
            return
        keep = {it.data(Qt.UserRole) for it in self.kp_list.selectedItems()}
        keep.add(kp_id)          # 新建的自动勾上
        self._reload_kp_list(keep)

    def _rename_kp(self):
        from PySide6.QtWidgets import QInputDialog
        cur = self.kp_list.currentItem()
        if not cur:
            QMessageBox.information(self, '提示', '请先高亮一个知识点')
            return
        kp_id = cur.data(Qt.UserRole)
        new, ok = QInputDialog.getText(
            self, '重命名知识点', '新名称:', QLineEdit.Normal, cur.text())
        if not ok:
            return
        new = new.strip()
        if not new or new == cur.text():
            return
        good, msg = db.rename_kp(kp_id, new)
        if not good:
            QMessageBox.warning(self, '无法重命名', msg)
            return
        self._reload_kp_list()

    def _delete_kp(self):
        cur = self.kp_list.currentItem()
        if not cur:
            QMessageBox.information(self, '提示', '请先高亮一个知识点')
            return
        kp_id = cur.data(Qt.UserRole)
        n = db.kp_usage(kp_id)
        txt = (f'知识点「{cur.text()}」正被 {n} 道题引用。\n\n'
               f'删除后这些题将失去该知识点（题目本身不受影响）。\n\n确定删除？'
               if n else f'确定删除知识点「{cur.text()}」？')
        box = QMessageBox(self)
        box.setWindowTitle('删除知识点')
        box.setText(txt)
        box.setIcon(QMessageBox.Warning)
        b_yes = box.addButton('删除', QMessageBox.AcceptRole)
        box.addButton('取消', QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not b_yes:
            return
        keep = {it.data(Qt.UserRole) for it in self.kp_list.selectedItems()}
        keep.discard(kp_id)
        db.delete_kp(kp_id)
        self._reload_kp_list(keep)

    def _manage_kp(self):
        from kp_manage_dialog import KpManageDialog
        dlg = KpManageDialog(self)
        # ★ 必须让关闭即销毁，否则反复开关会持续吃内存（详见 main._autoclose）
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.exec()
        self._reload_kp_list()

    def _selected_kp_ids(self):
        return [it.data(Qt.UserRole) for it in self.kp_list.selectedItems()]

    # ---------------- 选项识别 ----------------

    def _refresh_opt_preview(self):
        stem = self.t_stem.toPlainText()
        _, opts = db.split_stem_options(stem)
        if not opts:
            self.opt_preview.setText('（还没有识别到选项行）')
            return
        ans = db.parse_answer_letters(self.t_answer.toPlainText(),
                                      [o[0] for o in opts])
        parts = []
        for lab, content in opts:
            mark = '  ✅' if lab in ans else ''
            c = content if len(content) <= 40 else content[:40] + '…'
            parts.append(f'{lab}. {c}{mark}')
        self.opt_preview.setText('\n'.join(parts))

    # ---------------- 图片 ----------------

    # 草稿（未入库）图片在正文里的占位符写法：[[IMG:draft0]]
    _RE_DRAFT = None

    @staticmethod
    def _draft_token(i):
        return '[[IMG:draft%d]]' % i

    @staticmethod
    def _re_draft():
        if QuestionEditDialog._RE_DRAFT is None:
            QuestionEditDialog._RE_DRAFT = re.compile(r'\[\[IMG:draft(\d+)\]\]')
        return QuestionEditDialog._RE_DRAFT

    def _iter_figs(self):
        """当前配图列表，按**面板顺序**列出。

        每项：{'kind':'new'|'existing', 'path':路径, 'row':行号}
        """
        out = []
        for i in range(self.img_list.count()):
            it = self.img_list.item(i)
            d = it.data(Qt.UserRole)
            if not d:
                continue
            kind, val = d
            path = val if kind == 'new' else db.abs_attach_path(val)
            out.append({'kind': kind, 'path': path, 'row': i, 'val': val})
        return out

    @staticmethod
    def _make_thumb_icon(path, size=72):
        """生成缩略图图标（等比铺满，不拉变形）。"""
        from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
        try:
            pm = QPixmap(path)
            if pm.isNull():
                return QIcon()
            pm = pm.scaled(size, size, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
            # 贴上棋盘底，透明图也能看清轮廓
            canvas = QPixmap(size, size)
            canvas.fill(QColor('#ffffff'))
            p = QPainter(canvas)
            p.drawPixmap((size - pm.width()) // 2,
                         (size - pm.height()) // 2, pm)
            p.end()
            return QIcon(canvas)
        except Exception:
            return QIcon()

    def eventFilter(self, obj, ev):
        """缩略图墙：拖拽起手时记下被拖的项（Qt InternalMove 拖完才知道结果）。

        文本框：记住最近操作过的那个，插图时才知道该往哪个框里放。
        """
        if obj is getattr(self, 'img_list', None):
            if ev.type() == QEvent.MouseButtonPress:
                it = self.img_list.itemAt(ev.position().toPoint())
                if it is not None:
                    self._drag_data = it.data(Qt.UserRole)
            elif ev.type() == QEvent.MouseButtonRelease:
                if getattr(self, '_drag_data', None) is not None:
                    # 没发生 rowsMoved（只是点了一下）就清掉，别污染下次拖拽
                    QTimer.singleShot(
                        0, lambda: setattr(self, '_drag_data', None))
        elif obj is getattr(self, 't_stem', None) or obj is getattr(self, 't_solution', None):
            if ev.type() in (QEvent.FocusIn, QEvent.MouseButtonPress):
                self._last_box = obj
        return super().eventFilter(obj, ev)

    def _on_rows_moved(self, parent, start, end, dest, row):
        """拖拽重排后：按新行号重写拖拽对象的草稿占位符。"""
        data = getattr(self, '_drag_data', None)
        self._drag_data = None
        if data is None:
            return
        kind, val = data
        new_row = -1
        for i in range(self.img_list.count()):
            if self.img_list.item(i).data(Qt.UserRole) == (kind, val):
                new_row = i
                break
        if new_row < 0:
            return
        self._renumber_token(kind, val, new_row)

    def _renumber_token(self, kind, val, new_row):
        """把某个草稿图在正文里的占位符改成新行号。

        正文里 [[IMG:draftN]] 的 N 一律是**面板行号**（不是 _pending 下标），
        所以拖拽后只需把「该图的原行号」换成「新行号」。
        """
        if kind != 'new':
            return
        # 找这张图在正文里的旧行号：扫一遍正文里的占位符，
        # 看哪个行号对应的图正是 val。
        old_row = None
        for i in range(self.img_list.count()):
            d = self.img_list.item(i).data(Qt.UserRole)
            if d == (kind, val):
                old_row = i
                break
        if old_row is None or old_row == new_row:
            return
        for edit in (self.t_stem, self.t_solution):
            raw = edit.toPlainText()
            if self._draft_token(old_row) in raw:
                edit.blockSignals(True)
                cur_pos = edit.textCursor().position()
                new = raw.replace(self._draft_token(old_row),
                                  self._draft_token(new_row))
                edit.setPlainText(new)
                c = edit.textCursor()
                c.setPosition(min(cur_pos, len(new)))
                edit.setTextCursor(c)
                edit.blockSignals(False)
                edit._apply_formula_format()

    def _add_image_from_path(self, path):
        """题干/解析粘贴或拖入的图片 -> 登记为待入库配图。

        同时在**获得焦点的那个文本框的光标处**留下草稿占位符，
        这样导出时能按位置排（不再是全部堆到选项后面）。
        """
        if path not in self._pending_attachments:
            self._pending_attachments.append(path)
        it = QListWidgetItem(QIcon(self._make_thumb_icon(path)),
                             os.path.basename(path))
        it.setData(Qt.UserRole, ('new', path))
        it.setToolTip(path)
        self.img_list.addItem(it)
        # ★ 占位符编号 = 该图在面板里的**行号**（刚加完就是最后一行）
        idx = self.img_list.count() - 1
        row_of_path = {f['val']: f['row'] for f in self._iter_figs()}
        idx = row_of_path.get(path, idx)

        # 插到光标处：优先**最近操作过的框**，否则题干。
        # ★ 只看 hasFocus() 不够稳：用户点「添加图片…」按钮时焦点
        #   已经跑到按钮上，hasFocus 会全 False，图就会默默跑进题干。
        target, field = self._last_text_box(), 'stem'
        field = 'solution' if target is self.t_solution else 'stem'
        target.blockSignals(True)
        cur = target.textCursor()
        cur.insertText(self._draft_token(idx))
        target.setTextCursor(cur)
        target.blockSignals(False)
        target._apply_formula_format()
        self._img_field[path] = field      # 记住这张图属于哪个框
        self.img_list.setCurrentRow(self.img_list.count() - 1)

    def _last_text_box(self):
        """返回最近操作过的文本框：先看当前焦点，再退回上次记住的。"""
        fw = QApplication.focusWidget()
        if fw is self.t_stem or fw is self.t_solution:
            self._last_box = fw
            return fw
        for b in (self.t_stem, self.t_solution):
            if b.hasFocus():
                self._last_box = b
                return b
        return getattr(self, '_last_box', None) or self.t_stem

    def _pick_stem_image(self):
        files, _ = QFileDialog.getOpenFileNames(self, '选择图片', '', IMG_FILTER)
        for f in files:
            self._add_image_from_path(f)

    def _add_images(self):
        files, _ = QFileDialog.getOpenFileNames(self, '选择图片', '', IMG_FILTER)
        for f in files:
            self._add_image_from_path(f)

    def _remove_image(self):
        """删掉一张图：面板、正文占位符、待入库队列三处一起改。

        ★ 占位符里的数字一律是**面板行号**（不是 _pending 下标），
          删除后行号会断档，必须把后面所有行号整体左移一位。
        """
        row = self.img_list.currentRow()
        if row < 0:
            return
        it = self.img_list.item(row)
        data = it.data(Qt.UserRole)
        if data and data[0] == 'new' and data[1] in self._pending_attachments:
            del self._pending_attachments[self._pending_attachments.index(data[1])]
            self._img_field.pop(data[1], None)
        # 先取出、再重编号：takeItem 之前记下后面每张图的新行号
        self.img_list.blockSignals(True)
        self.img_list.takeItem(row)
        self.img_list.blockSignals(False)
        # 旧行号 -> 新行号：删掉的那行 -> 消失；>row 的整体 -1
        self._renumber_after_delete(row)

    def _renumber_after_delete(self, dead_row):
        """删掉 dead_row 后，把正文占位符里的行号重写。

        旧行号 k -> 新行号：
          k < dead_row : k（不变）
          k == dead_row: 删除该占位符
          k > dead_row : k-1
        """
        def _sub_factory():
            def _sub(m):
                k = int(m.group(1))
                if k == dead_row:
                    return ''
                return self._draft_token(k - 1 if k > dead_row else k)
            return _sub

        for edit in (self.t_stem, self.t_solution):
            raw = edit.toPlainText()
            if not self._re_draft().search(raw):
                continue
            new = self._re_draft().sub(_sub_factory(), raw)
            if new != raw:
                edit.blockSignals(True)
                pos = edit.textCursor().position()
                edit.setPlainText(new)
                c = edit.textCursor()
                c.setPosition(min(pos, len(new)))
                edit.setTextCursor(c)
                edit.blockSignals(False)
                edit._apply_formula_format()

    def _show_big_preview(self, item):
        """双击缩略图 -> 弹大图窗口。"""
        d = item.data(Qt.UserRole)
        if not d:
            return
        kind, val = d
        path = val if kind == 'new' else db.abs_attach_path(val)
        if not os.path.exists(path):
            QMessageBox.warning(self, '预览', '文件不存在：\n' + path)
            return
        dlg = ImagePreviewDialog(path, parent=self)
        dlg.exec()

    def _preview_image(self, row):
        if row < 0:
            return
        it = self.img_list.item(row)
        data = it.data(Qt.UserRole)
        if not data:
            return
        kind, val = data
        path = val if kind == 'new' else db.abs_attach_path(val)
        if os.path.exists(path):
            pix = QPixmap(path)
            if pix.width() > 500:
                pix = pix.scaledToWidth(500, Qt.SmoothTransformation)
            if pix.height() > 300:
                pix = pix.scaledToHeight(300, Qt.SmoothTransformation)
            self.img_preview.setPixmap(pix)
        else:
            self.img_preview.setText('文件不存在')

    # ---------------- 公式 ----------------

    def _insert_stem_formula(self):
        tex = self.stem_formula.latex()
        if tex:
            self.t_stem.textCursor().insertText(f'${tex}$')

    def _insert_solution_formula(self):
        tex = self.stem_formula.latex()          # 两个按钮共用一个输入框
        if tex:
            self.t_solution.textCursor().insertText(f'${tex}$')

    # ---------------- 载入 / 保存 ----------------

    def _load(self, qid):
        q = db.get_question(qid)
        if not q:
            return

        # 兼容旧数据：老库里选项是分开存的，载入时把它们拼回题干
        stem = q['stem']
        if q['options'] and not db.split_stem_options(stem)[1]:
            parts = [stem.rstrip()]
            for o in q['options']:
                parts.append(f"{o['label']}. {o['content']}")
            stem = '\n'.join(parts)

        # 把已有的**带锚点图**在文本里还原成占位符，这样用户能看到
        # 它们在哪个位置，也能接着拖拽换位。无锚点的旧图不占位。
        stem_marks, sol_marks = [], []
        for a in (q['attachments'] or []):
            off = a.get('anchor_offset')
            if off is None or int(off) < 0:
                continue
            fld = a.get('field') or 'stem'
            (stem_marks if fld == 'stem' else sol_marks).append((int(off), a))

        def _weave(txt, marks):
            """把已入库的带锚点图还原成占位符。

            ★ 必须**从后往前**插：anchor_offset 是对「原始纯文本」量的，
              若从前往后插，每插一个占位符都会把后面的坐标推偏，
              最终占位符互相嵌套（实测烂成 `[[IM[[IMG:db18]]G:db17]]`）。
              倒序插则前面待插位置不受影响。
            """
            for off, a in sorted(marks, key=lambda x: x[0], reverse=True):
                pos = max(0, min(int(off), len(txt)))
                tok = '[[IMG:db%d]]' % a['id']
                txt = txt[:pos] + tok + txt[pos:]
            return txt

        stem = _weave(stem, stem_marks)
        solution = _weave(q['solution'], sol_marks)
        self._stem_anchor_marks = stem_marks
        self._sol_anchor_marks = sol_marks

        self.t_stem.setPlainText(stem)
        self.t_answer.setPlainText(q['answer'])
        self.t_solution.setPlainText(solution)

        idx = self.c_type.findText(
            self.type_labels.get(q['question_type'], q['question_type']))
        if idx < 0 and q['question_type'] == 'choice':
            # 旧数据：尚未拆分的 choice。按答案字母数推断单选/多选。
            import re as _re
            n = len(_re.findall(r'[A-Ha-h]', q['answer'] or ''))
            guess = 'choice_multi' if n >= 2 else 'choice_single'
            idx = self.c_type.findText(self.type_labels[guess])
        if idx >= 0:
            self.c_type.setCurrentIndex(idx)
        self.s_diff.setValue(q['difficulty'] or 3)
        self.e_source.setText(q['source'] or '')
        gi = self.c_grade.findText(q['grade'] or '')
        self.c_grade.setCurrentIndex(gi if gi >= 0 else 0)
        self.e_note.setText(q['note'] or '')

        kp_ids = {k['id'] for k in q['kp']}
        for i in range(self.kp_list.count()):
            it = self.kp_list.item(i)
            if it.data(Qt.UserRole) in kp_ids:
                it.setSelected(True)

        self._existing_attachments = q['attachments']
        for a in q['attachments']:
            kind = 'new' if a['rel_path'] in self._orphan_rels else 'existing'
            it = QListWidgetItem(
                QIcon(self._make_thumb_icon(db.abs_attach_path(a['rel_path']))),
                os.path.basename(a['rel_path']))
            it.setData(Qt.UserRole, (kind if kind == 'existing' else 'new',
                                     a['rel_path']))
            it.setToolTip(db.abs_attach_path(a['rel_path']))
            self.img_list.addItem(it)

    def _save(self):
        # 保存失败必须让用户看见，绝不能静默吞掉
        # （历史 bug：图片已拷进 q_<id>/ 但 attachments 行没写成，
        #   报错被吞进 Qt 槽，用户只看到“图丢了”。）
        try:
            qid = self._do_save()
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(
                self, '保存失败',
                f'保存时出错，数据未写入：\n\n{type(e).__name__}: {e}')
            return
        if qid is None:
            return          # 校验没过（如答案与单选题/多选题不符），不关窗
        self.accept()

    # ---------------- 查重 ----------------

    def _confirm_duplicate(self, stem):
        """检查库里有没有相同/相似的题。有则弹窗让用户二次确认。

        返回 True = 继续保存，False = 用户选择返回修改。
        """
        try:
            hits = db.find_similar_questions(stem, exclude_id=self.qid)
        except Exception:
            # 查重只是辅助，不能因为查重本身出错而不让存题
            import traceback
            traceback.print_exc()
            return True
        if not hits:
            return True

        exact = [h for h in hits if h['kind'] == 'exact']
        dlg = DuplicateDialog(hits, exact=bool(exact), parent=self)
        # ★ 重复题对照框可能反复弹出（用户反复录入相似题），必须关闭即销毁
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        return dlg.exec() == QDialog.Accepted

    # ---- 占位符 <-> 锚点下标 换算 ----
    _RE_ALL_TOK_C = None

    @classmethod
    def _re_all_tok(cls):
        """草稿占位符 + 已入库占位符，一把梭。"""
        if cls._RE_ALL_TOK_C is None:
            cls._RE_ALL_TOK_C = re.compile(r'\[\[IMG:(?:draft|db)\d+\]\]')
        return cls._RE_ALL_TOK_C

    def _extract_anchors(self, raw, field):
        """从带占位符的文本里抽出锚点。

        返回 (纯文本, [(anchor_offset, ref), ...])
          ref = ('new', 路径, field)     —— 本次新加的图
              = ('exist', att_id, field) —— 库里已有的图

        anchor_offset = 占位符所在位置在**纯文本**里的字符下标。
        两个陷阱：
          1. 占位符本身不算字符（导出时它是图，不是文字）。
          2. U+FFFC（OCR / 网页粘贴带来的历史图标记）也不算字符。
             它是本函数内一并剥掉的对象，若不先剔就累加长度，
             OCR 老图一多，锚点下标就会整体偏大、图文错位。
        """
        pat = self._re_all_tok()
        out = []
        parts = []
        running = 0          # 已输出的**纯文本**长度
        prev = 0
        by_row = {f['row']: f for f in self._iter_figs()}
        for m in pat.finditer(raw):
            seg_clean = raw[prev:m.start()].replace(ImageTextEdit.ANCHOR, '')
            parts.append(seg_clean)
            running += len(seg_clean)
            tok = m.group(0)
            if tok.startswith('[[IMG:draft'):
                idx = int(tok[len('[[IMG:draft'):-2])
                fr = by_row.get(idx)
                if fr and fr['kind'] == 'new':
                    fld = self._img_field.get(fr['val'], field)
                    out.append((running, ('new', fr['val'], fld)))
            else:
                aid = int(tok[len('[[IMG:db'):-2])
                out.append((running, ('exist', aid, field)))
            prev = m.end()
        parts.append(raw[prev:].replace(ImageTextEdit.ANCHOR, ''))
        plain = ''.join(parts)
        return plain, out

    def _do_save(self):
        # 题干/解析里内联的图片会留下 U+FFFC（对象替换符）占位，
        # 存库前剔掉（图靠 attachments 存，文本不该带控制符）。
        def _clean(box):
            t = box.toPlainText().replace('\ufffc', '')
            return '\n'.join(ln.rstrip() for ln in t.splitlines()).strip()

        stem_raw = self.t_stem.toPlainText()
        stem, stem_anchors = self._extract_anchors(stem_raw, 'stem')
        stem = '\n'.join(ln.rstrip() for ln in stem.splitlines()).strip()
        if not stem:
            QMessageBox.warning(self, '提示', '题干不能为空')
            return None

        sol_raw = self.t_solution.toPlainText()
        solution, sol_anchors = self._extract_anchors(sol_raw, 'solution')
        solution = '\n'.join(ln.rstrip() for ln in solution.splitlines()).strip()

        # ---- 查重：新录/编辑时先看库里有没有一样的 ----------------
        # 只在真正写库前问一次；用户确认“仍要保存”就放过。
        if not self._confirm_duplicate(stem):
            return None

        type_key = self.c_type.currentText()
        for lab, name in self.type_labels.items():
            if name == type_key:
                type_key = lab
                break

        # 单选/多选/判断 答案硬性校验
        ans_raw = self.t_answer.toPlainText().replace('\ufffc', '').strip()
        if type_key in ('choice_single', 'choice_multi'):
            import re as _re
            letters = _re.findall(r'[A-Ha-h]', ans_raw)
            if type_key == 'choice_single' and len(letters) > 1:
                QMessageBox.warning(
                    self, '答案不符合单选题',
                    f'当前题型是「单选题」，但答案填了 {len(letters)} 个选项：{ans_raw}\n\n'
                    '单选题答案只能是一个字母（如 A）。\n'
                    '若这是多选题，请把「题型」改成「多选题」。')
                return None
            if type_key == 'choice_multi' and len(letters) < 2:
                QMessageBox.warning(
                    self, '答案不符合多选题',
                    f'当前题型是「多选题」，但答案只填了 {len(letters)} 个选项：{ans_raw or "（空）"}\n\n'
                    '多选题答案应为两个及以上字母（如 AC）。\n'
                    '若这是单选题，请把「题型」改成「单选题」。')
                return None
        elif type_key == 'judgment':
            # 判断题：答案必须是 √ / × 之一（对/错、T/F 等常见写法自动转换）
            norm = db.normalize_judgment_answer(ans_raw)
            if norm is None:
                QMessageBox.warning(
                    self, '答案不符合判断题',
                    f'当前题型是「判断题」，但答案填的是：{ans_raw or "（空）"}\n\n'
                    '判断题答案请填「√」或「×」。\n（也认对/错、正确/错误、T/F、是/否）')
                return None
            ans_raw = norm

        kp_ids = self._selected_kp_ids()

        # 页码字段已从界面移除。但旧库里有题存了 source_page，
        # 编辑时不能把它抹掉 —— 沿用库中现有值，仅新建时置空。
        keep_page = None
        if self.qid:
            _old = db.get_question(self.qid)
            if _old:
                keep_page = _old['source_page']

        payload = dict(
            stem=stem,
            answer=ans_raw,             # 已归一化（判断题 -> √/×）
            solution=solution,
            question_type=type_key,
            difficulty=self.s_diff.value(),
            source=self.e_source.text().strip(),
            source_page=keep_page,      # 界面已移除该字段，保留旧值不丢
            grade=self.c_grade.currentText(),
            note=self.e_note.text().strip(),
        )

        if self.qid:
            db.update_question(self.qid, **payload)
            # 选项已并入题干，旧选项表清空（保留表结构以兼容历史数据）
            db.replace_options(self.qid, [])
            conn = db.get_conn()
            conn.execute('DELETE FROM question_kp WHERE question_id=?', (self.qid,))
            for kp in kp_ids:
                conn.execute(
                    'INSERT OR IGNORE INTO question_kp(question_id, kp_id) VALUES (?,?)',
                    (self.qid, kp))
            conn.commit()
            conn.close()
            qid = self.qid
            db._refresh_index(qid)     # 题干变了，重建检索索引
        else:
            qid = db.add_question(options=None, kp_ids=kp_ids, **payload)

        # ---- 写配图（带位置信息）----
        # 先把「本次新加的图」入库，拿到 id；再统一写锚点。
        path2id = {}
        for f in self._pending_attachments:
            try:
                aid = db.add_attachment(qid, f, role='stem_figure')
                path2id[f] = aid
            except Exception as e:
                QMessageBox.warning(self, '配图',
                                    f'导入失败 {os.path.basename(f)}: {e}')
        self._pending_attachments = []

        # 锚点写到 attachments 行上；带着位置的导出就能图文对齐
        try:
            for off, ref in (stem_anchors + sol_anchors):
                kind, val, fld = ref
                if kind == 'new':
                    aid = path2id.get(val)
                    if aid is None:
                        continue
                else:
                    aid = val
                db.update_attachment_position(
                    aid, field=fld, anchor_offset=int(off))
        except Exception:
            import traceback
            traceback.print_exc()      # 没写完也不算失败：图已入库，只是位置退回默认

        # 本次没入正文的图（无锚点）统一标 -1，导出时倒堆到题干后
        try:
            placed = set(path2id.values())
            for off, ref in (stem_anchors + sol_anchors):
                kind, val, fld = ref
                if kind == 'exist':
                    placed.add(val)
            for a in db.list_attachments(qid):
                if a['id'] not in placed:
                    db.update_attachment_position(
                        a['id'], field=a.get('field') or 'stem',
                        anchor_offset=-1)
        except Exception:
            import traceback
            traceback.print_exc()

        # 返回 qid，供测试/调用方确认（accept 由外层 _save 统一做）
        return qid
