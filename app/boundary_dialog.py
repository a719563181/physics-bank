"""
boundary_dialog.py — 人工分界预览窗口

用户 2026-10-07 需求：
  「选取文件后先打开预览，人工分界后依据分界点识别」

为什么要它：纯自动切分有天花板——OCR 引擎漏读某行时（如高三卷缺的
16 号），任何规则都救不回来。让用户点两行之间的横线，就能把这些
机器分不出来的边界显式标出来。

交互：
  · 左侧列出各页缩略图，右侧大图预览
  · 在大图上「点一下」= 在该处加一条分界线（= 一道新题开始）
  · 右键 / 选中删除 = 取消某条线
  · 「本页全不切」开关
  · 完成后返回 {页码: [y比例, ...]}

★ 坐标一律用【比例 0~1】（相对页高），与渲染 dpi 无关，
  这样预览图多大都不影响后台 OCR 用的比例。
"""
import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QListWidget,
    QListWidgetItem, QSplitter, QMessageBox, QCheckBox, QWidget,
    QScrollArea, QSizePolicy
)
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor


class PageView(QLabel):
    """一张页面图，支持在上面点出分界线。"""

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pix = None
        self._cuts = []          # 比例 0~1，升序
        self.on_change = on_change
        self.currentPage = 1

    def set_page(self, pixmap):
        self._pix = pixmap
        self._refresh()

    def set_cuts(self, cuts):
        self._cuts = sorted(float(c) for c in (cuts or []))
        self._refresh()

    def cuts(self):
        return list(self._cuts)

    # ---- 坐标换算：图上像素 <-> 比例 ----
    def _img_rect(self):
        """返回图片实际绘制区域 (x, y, w, h)（居中）。"""
        if self._pix is None:
            return (0, 0, 0, 0)
        aw, ah = self.width(), self.height()
        iw, ih = self._pix.width(), self._pix.height()
        # 等比缩放到适应
        scale = min(aw / iw, ah / ih) if iw and ih else 1.0
        scale = min(scale, 1.0)   # 不放大
        w, h = int(iw * scale), int(ih * scale)
        x = (aw - w) // 2
        y = 0
        return (x, y, w, h)

    def _refresh(self):
        if self._pix is None:
            self.setText('（无图）')
            return
        x, y, w, h = self._img_rect()
        canvas = QPixmap(self.size())
        canvas.fill(QColor('#2b2b2b'))
        painter = QPainter(canvas)
        painter.drawPixmap(x, y, w, h, self._pix)
        # 画分界线
        pen = QPen(QColor('#e53935'))
        pen.setWidth(2)
        painter.setPen(pen)
        for c in self._cuts:
            yy = y + int(h * c)
            painter.drawLine(x, yy, x + w, yy)
            painter.setPen(QPen(QColor('#e53935')))
            painter.drawText(x + 4, max(yy - 3, y + 12), f'题({c:.2f})')
            painter.setPen(pen)
        painter.end()
        self.setPixmap(canvas)

    def mousePressEvent(self, ev):
        if self._pix is None:
            return
        if ev.button() == Qt.LeftButton:
            x, y, w, h = self._img_rect()
            if w <= 0 or h <= 0:
                return
            pos = ev.position() if hasattr(ev, 'position') else ev.pos()
            py = pos.y()
            if not (y <= py <= y + h):
                return
            ratio = (py - y) / float(h)
            ratio = min(max(ratio, 0.0), 1.0)
            # 太靠近已有线就忽略（避免叠条）
            if any(abs(ratio - c) < 0.015 for c in self._cuts):
                return
            self._cuts.append(ratio)
            self._cuts.sort()
            self._refresh()
            if self.on_change:
                self.on_change(self.currentPage, self._cuts)
        elif ev.button() == Qt.RightButton:
            # 右键：删掉最近的一条线
            x, y, w, h = self._img_rect()
            if w <= 0 or h <= 0 or not self._cuts:
                return
            pos = ev.position() if hasattr(ev, 'position') else ev.pos()
            ratio = (pos.y() - y) / float(h)
            near = min(self._cuts, key=lambda c: abs(c - ratio))
            if abs(near - ratio) < 0.08:
                self._cuts.remove(near)
                self._refresh()
                if self.on_change:
                    self.on_change(self.currentPage, self._cuts)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._refresh()


class BoundaryDialog(QDialog):
    """人工分界窗口。

    pages: [(页码, 图片路径), ...]
    返回：self.result_map = {页码: [y比例, ...]}
    """

    def __init__(self, pages, parent=None):
        super().__init__(parent)
        self.setWindowTitle('人工分界 —— 点一下 = 一道题开始（右键取消）')
        self.resize(1200, 820)
        self.pages = pages
        self.result_map = {}
        self._cut_store = {}   # 页码 -> [比例]

        root = QVBoxLayout(self)

        tip = QLabel(
            '操作：在右侧页面上【单击】加一条分界线（红线 = 新题开始）；'
            '【右键】删除最近的线。每页至少要有 1 条线（页首）。'
            '分好后点「开始识别」，程序按你的分界线切题，不再猜题号。')
        tip.setWordWrap(True)
        tip.setStyleSheet('color:#444; background:#fff8e1; padding:6px; '
                          'border:1px solid #ffe082; border-radius:4px;')
        root.addWidget(tip)

        split = QSplitter(Qt.Horizontal)

        # 左：页列表
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('页面:'))
        self.lst = QListWidget()
        self.lst.currentRowChanged.connect(self.on_page_change)
        for n, p in pages:
            self.lst.addItem(QListWidgetItem(f'第 {n} 页'))
        ll.addWidget(self.lst)
        self.cb_nocut = QCheckBox('本页全不切（整页=1道）')
        self.cb_nocut.stateChanged.connect(self.on_nocut)
        ll.addWidget(self.cb_nocut)
        b_clr = QPushButton('清空本页分界')
        b_clr.clicked.connect(self.on_clear)
        ll.addWidget(b_clr)
        b_all = QPushButton('每页加页首线')
        b_all.clicked.connect(self.on_add_all_first)
        ll.addWidget(b_all)
        split.addWidget(lw)

        # 右：大图
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.view = PageView(on_change=self.on_cuts_changed)
        self.scroll.setWidget(self.view)
        split.addWidget(self.scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([180, 1000])
        root.addWidget(split)

        # 底
        bot = QHBoxLayout()
        self.lab = QLabel('')
        bot.addWidget(self.lab)
        bot.addStretch(1)
        b_ok = QPushButton('开始识别')
        b_ok.clicked.connect(self.on_ok)
        b_cancel = QPushButton('取消')
        b_cancel.clicked.connect(self.reject)
        bot.addWidget(b_ok)
        bot.addWidget(b_cancel)
        root.addLayout(bot)

        self._pix_cache = {}
        if self.lst.count():
            self.lst.setCurrentRow(0)
        self.refresh_label()

    # ---- 取图 ----
    def _pix(self, page):
        if page not in self._pix_cache:
            path = dict(self.pages)[page]
            pm = QPixmap(path)
            self._pix_cache[page] = pm
        return self._pix_cache[page]

    def _ensure_first_cut(self, page):
        if page not in self._cut_store or not self._cut_store[page]:
            self._cut_store[page] = [0.0]

    # ---- 事件 ----
    def on_page_change(self, row):
        if row < 0:
            return
        page = self.pages[row][0]
        self.view.currentPage = page
        self.view.set_page(self._pix(page))
        self._ensure_first_cut(page)
        self.view.set_cuts(self._cut_store[page])
        self.cb_nocut.blockSignals(True)
        self.cb_nocut.setChecked(self._cut_store.get(page) == [0.0]
                                 and self._is_nocut(page))
        self.cb_nocut.blockSignals(False)
        self.refresh_label()

    def _is_nocut(self, page):
        return self._nocut.get(page, False) if hasattr(self, '_nocut') else False

    def on_cuts_changed(self, page, cuts):
        self._cut_store[page] = list(cuts)
        self.refresh_label()

    def on_clear(self):
        page = self.view.currentPage
        self._cut_store[page] = [0.0]
        self.view.set_cuts([0.0])
        self.refresh_label()

    def on_nocut(self, *_):
        page = self.view.currentPage
        if not hasattr(self, '_nocut'):
            self._nocut = {}
        if self.cb_nocut.isChecked():
            self._nocut[page] = True
            self._cut_store[page] = [0.0]
            self.view.set_cuts([0.0])
        else:
            self._nocut[page] = False
        self.refresh_label()

    def on_add_all_first(self):
        for n, _ in self.pages:
            if not self._cut_store.get(n):
                self._cut_store[n] = [0.0]
        self.view.set_cuts(self._cut_store[self.view.currentPage])
        self.refresh_label()

    def refresh_label(self):
        total = sum(len(v) for v in self._cut_store.values())
        self.lab.setText(f'共 {len(self.pages)} 页，'
                         f'已标记 {total} 条分界线')

    def on_ok(self):
        # 每页保证有页首线
        for n, _ in self.pages:
            if not self._cut_store.get(n):
                self._cut_store[n] = [0.0]
        self.result_map = {n: sorted(self._cut_store[n]) for n, _ in self.pages}
        self.accept()
