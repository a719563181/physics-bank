# -*- coding: utf-8 -*-
"""
ppt_theme_dialog.py —— PPT 模板（主题）编辑对话框。

功能
----
* 左：主题列表（内置 + 用户），可新建 / 复制 / 删除 / 导入 / 导出
* 右：编辑当前主题 —— 名称、字体、配色（带取色按钮）、字号、文案
* 底部：**实时预览**（一个小 PPT 页面缩略图，随改随变）
* 「保存」写入 themes/<name>.json；内置主题只读，改内置名会提示另存

设计上刻意不做「所见即所得的整页渲染」——那需要跑一遍导出，
太重。这里用轻量预览：色块 + 字号示意 + 封面/题目页小样。
"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QVBoxLayout, QWidget,
)

import ppt_theme as th

# 界面上暴露的颜色字段（键, 中文名）
_COLOR_ROWS = [
    ('accent', '主色（标题栏/分节页）'),
    ('accent_fg', '主色上的文字'),
    ('title', '标题字色'),
    ('body', '正文字色'),
    ('muted', '次要字色'),
    ('answer', '答案字色'),
    ('solution', '解析字色'),
    ('cover_bg', '封面底色'),
]

# 字号字段（键, 中文名, 范围）
_SIZE_ROWS = [
    ('size_title', '封面标题', 20, 60),
    ('size_section', '分节页标题', 20, 60),
    ('size_end', '结尾页文字', 18, 60),
    ('size_stem', '题干', 12, 32),
    ('size_tex', '题干公式', 12, 40),
    ('size_option', '选项', 12, 30),
    ('size_option_tex', '选项公式', 12, 36),
    ('size_answer', '答案', 12, 36),
    ('size_answer_tex', '答案公式', 12, 40),
    ('size_solution', '解析正文', 10, 28),
    ('size_solution_tex', '解析公式', 10, 34),
]

# 文案字段（键, 中文名, 提示）
_TEXT_ROWS = [
    ('cover_footer', '封面页脚', '留空则不显示'),
    ('end_text', '结尾页文字', ''),
    ('bar_left', '标题栏左侧', '可用 {idx} 和 {total}'),
    ('answer_prefix', '答案前缀', ''),
    ('solution_label', '解析标签', ''),
]


def _c(rgb_hex):
    return QColor(rgb_hex)


class ColorButton(QPushButton):
    """一个显示当前颜色的小方块，点击弹取色器。"""

    changed = Signal(str)

    def __init__(self, hexv):
        super().__init__()
        self._hex = hexv
        self.setFixedSize(64, 26)
        self.clicked.connect(self._pick)
        self._paint()

    def _paint(self):
        self.setText(self._hex)
        self.setStyleSheet(
            f"background:{self._hex};color:"
            f"{'#000' if self._light() else '#fff'};"
            "border:1px solid #999;border-radius:3px;font-size:11px;")

    def _light(self):
        c = _c(self._hex)
        return (c.red() * 299 + c.green() * 587 + c.blue() * 114) / 1000 > 150

    def value(self):
        return self._hex

    def set_value(self, hexv, emit=False):
        self._hex = hexv
        self._paint()
        if emit:
            self.changed.emit(self._hex)

    def _pick(self):
        col = QColorDialog.getColor(_c(self._hex), self, '选择颜色')
        if col.isValid():
            self._hex = '#%02X%02X%02X' % (col.red(), col.green(), col.blue())
            self._paint()
            self.changed.emit(self._hex)


class ThemePreview(QWidget):
    """轻量预览：画一张 16:9 小样，含标题栏 / 题干 / 选项 / 答案。"""

    def __init__(self, theme=None):
        super().__init__()
        self._t = theme or th.load_theme('默认蓝')
        self.setMinimumHeight(230)

    def set_theme(self, theme):
        self._t = theme
        self.update()

    def paintEvent(self, ev):
        t = self._t
        W = self.width()
        H = self.height()
        pad = 8
        w = W - 2 * pad
        h = int(w * 9 / 16.0)
        if h > H - 2 * pad:
            h = H - 2 * pad
            w = int(h * 16 / 9.0)
        x0 = (W - w) // 2
        y0 = (H - h) // 2

        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.fillRect(0, 0, W, H, QColor('#EDEDED'))

        # 页面底
        g.setBrush(QBrush(_c(t['cover_bg'])))
        g.setPen(QPen(QColor('#BBBBBB'), 1))
        g.drawRect(x0, y0, w, h)

        s = w / 13.333          # 缩放比：1 英寸 -> s 像素
        bar_h = max(10, int(t['bar_h'] * s))
        # 标题栏
        g.setBrush(QBrush(_c(t['accent'])))
        g.setPen(Qt.NoPen)
        g.drawRect(x0, y0, w, bar_h)
        g.setPen(QPen(_c(t['accent_fg'])))
        f = QFont(t['font'])
        f.setPointSizeF(max(5, t['size_bar'] * s / 3.0))
        f.setBold(True)
        g.setFont(f)
        g.drawText(x0 + int(t['margin'] * s), y0, w, bar_h,
                   Qt.AlignVCenter | Qt.AlignLeft,
                   t['bar_left'].format(idx=1, total=8))

        # 题干
        g.setPen(QPen(_c(t['body'])))
        f2 = QFont(t['font'])
        f2.setPointSizeF(max(4.5, t['size_stem'] * s / 4.4))
        g.setFont(f2)
        ty = y0 + bar_h + int(0.18 * s)
        g.drawText(x0 + int(t['margin'] * s), ty, w - 2 * int(t['margin'] * s),
                   int(0.5 * s), Qt.AlignLeft | Qt.AlignTop,
                   '物体沿斜面下滑，求加速度大小。')

        # 公式示意块
        tex_h = int(0.5 * s)
        g.setPen(QPen(_c(t['solution'])))
        g.drawLine(x0 + int(t['margin'] * s), ty + int(0.55 * s),
                   x0 + int(t['margin'] * s) + int(1.6 * s),
                   ty + int(0.55 * s))

        # 选项
        f3 = QFont(t['font'])
        f3.setPointSizeF(max(4.5, t['size_option'] * s / 4.4))
        g.setFont(f3)
        g.setPen(QPen(_c(t['body'])))
        oy = ty + int(0.95 * s)
        for i, lab in enumerate('ABCD'):
            g.drawText(x0 + int((t['margin'] + 0.15) * s), oy + i * int(0.30 * s),
                       w, int(0.3 * s), Qt.AlignLeft | Qt.AlignTop,
                       f'{lab}. 选项内容')

        # 答案
        f4 = QFont(t['font'])
        f4.setPointSizeF(max(5, t['size_answer'] * s / 3.6))
        f4.setBold(True)
        g.setFont(f4)
        g.setPen(QPen(_c(t['answer'])))
        ay = y0 + h - int(0.62 * s)
        g.drawText(x0 + int(t['margin'] * s), ay, w, int(0.5 * s),
                   Qt.AlignLeft | Qt.AlignTop,
                   f"{t['answer_prefix']}C")

        g.end()


class ThemeDialog(QDialog):
    """主题编辑器。exec() 后若 accepted，取 .result_theme() 得到 Theme。"""

    def __init__(self, parent=None, current=None):
        super().__init__(parent)
        self.setWindowTitle('PPT 模板')
        self.resize(980, 640)
        self._cur = th.load_theme(current) if current else th.load_theme('默认蓝')
        self._loading = False
        self._build()
        self._load_list(select=self._cur['name'])

    # ---------------------------------------------------------- 构建界面
    def _build(self):
        root = QVBoxLayout(self)

        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        # ---- 左：主题列表
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel('主题列表'))
        self.lst = QListWidget()
        self.lst.currentItemChanged.connect(self._on_pick)
        lv.addWidget(self.lst, 1)
        row = QHBoxLayout()
        for txt, fn in (('新建', self._new), ('复制', self._copy),
                        ('删除', self._del)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        lv.addLayout(row)
        row2 = QHBoxLayout()
        for txt, fn in (('导入…', self._import), ('导出…', self._export)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row2.addWidget(b)
        lv.addLayout(row2)
        split.addWidget(left)

        # ---- 右：编辑 + 预览
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        self.preview = ThemePreview(self._cur)
        rv.addWidget(self.preview)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        iv = QVBoxLayout(inner)

        # 名称
        gb0 = QGroupBox('基本')
        f0 = QFormLayout(gb0)
        self.ed_name = QLineEdit()
        self.ed_name.textChanged.connect(self._sync)
        f0.addRow('名称', self.ed_name)
        self.ed_font = QFontComboBox()
        self.ed_font.currentFontChanged.connect(self._sync)
        f0.addRow('字体', self.ed_font)
        iv.addWidget(gb0)

        # 配色
        gb1 = QGroupBox('配色')
        f1 = QFormLayout(gb1)
        self._colors = {}
        for key, label in _COLOR_ROWS:
            b = ColorButton(self._cur[key])
            b.changed.connect(self._sync)
            self._colors[key] = b
            f1.addRow(label, b)
        iv.addWidget(gb1)

        # 字号
        gb2 = QGroupBox('字号（磅）')
        f2 = QFormLayout(gb2)
        self._sizes = {}
        for key, label, lo, hi in _SIZE_ROWS:
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.valueChanged.connect(self._sync)
            self._sizes[key] = sp
            f2.addRow(label, sp)
        iv.addWidget(gb2)

        # 版式
        gb3 = QGroupBox('版式（英寸）')
        f3 = QFormLayout(gb3)
        self._geo = {}
        for key, label, lo, hi, step in (
                ('margin', '页边距', 0.2, 1.5, 0.05),
                ('bar_h', '标题栏高', 0.4, 1.2, 0.02),
                ('body_top', '正文起始', 0.6, 2.0, 0.05),
                ('body_bottom', '正文下限', 5.5, 7.4, 0.05)):
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(2)
            sp.valueChanged.connect(self._sync)
            self._geo[key] = sp
            f3.addRow(label, sp)
        iv.addWidget(gb3)

        # 文案
        gb4 = QGroupBox('文案')
        f4 = QFormLayout(gb4)
        self._texts = {}
        for key, label, hint in _TEXT_ROWS:
            e = QLineEdit()
            if hint:
                e.setPlaceholderText(hint)
            e.textChanged.connect(self._sync)
            self._texts[key] = e
            f4.addRow(label, e)
        iv.addWidget(gb4)

        scroll.setWidget(inner)
        rv.addWidget(scroll, 1)

        # 保存为当前 / 另存
        brow = QHBoxLayout()
        self.btn_save = QPushButton('保存到该主题')
        self.btn_save.clicked.connect(self._save)
        self.btn_saveas = QPushButton('另存为新主题…')
        self.btn_saveas.clicked.connect(self._saveas)
        brow.addWidget(self.btn_save)
        brow.addWidget(self.btn_saveas)
        brow.addStretch(1)
        rv.addLayout(brow)

        split.addWidget(right)
        split.setSizes([230, 750])

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText('使用此模板')
        bb.button(QDialogButtonBox.Cancel).setText('取消')
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._load_fields(self._cur)

    # ---------------------------------------------------------- 数据 <-> 界面
    def _load_list(self, select=None):
        self.lst.blockSignals(True)
        self.lst.clear()
        builtin = set(th.builtin_names())
        for n in th.list_themes():
            it = QListWidgetItem(n + ('（内置）' if n in builtin else ''))
            it.setData(Qt.UserRole, n)
            if n in builtin:
                it.setForeground(QColor('#666666'))
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        if select:
            for i in range(self.lst.count()):
                if self.lst.item(i).data(Qt.UserRole) == select:
                    self.lst.setCurrentRow(i)
                    break

    def _load_fields(self, t):
        self._loading = True
        self.ed_name.setText(t['name'])
        self.ed_font.setCurrentFont(QFont(t['font']))
        for k, b in self._colors.items():
            b.set_value(t[k], emit=False)
        for k, sp in self._sizes.items():
            sp.setValue(int(round(float(t[k]))))
        for k, sp in self._geo.items():
            sp.setValue(float(t[k]))
        for k, e in self._texts.items():
            e.setText(t[k] if t[k] else '')
        self._loading = False

    def _collect(self):
        d = self._cur.to_dict()
        d['name'] = self.ed_name.text().strip() or '自定义'
        d['font'] = self.ed_font.currentFont().family()
        for k, b in self._colors.items():
            d[k] = b.value()
        for k, sp in self._sizes.items():
            d[k] = sp.value()
        for k, sp in self._geo.items():
            d[k] = round(sp.value(), 2)
        for k, e in self._texts.items():
            d[k] = e.text().strip()
        # body_top 必须小于 body_bottom，否则版面塌
        if d['body_top'] >= d['body_bottom'] - 0.5:
            d['body_bottom'] = min(7.4, d['body_top'] + 0.5)
        return th.Theme(**d)

    def _sync(self, *a):
        if self._loading:
            return
        self._cur = self._collect()
        self.preview.set_theme(self._cur)

    # ---------------------------------------------------------- 列表操作
    def _on_pick(self, cur, prev):
        if cur is None or self._loading:
            return
        name = cur.data(Qt.UserRole)
        t = th.load_theme(name)
        self._load_fields(t)
        self._cur = t
        self.preview.set_theme(t)
        # 内置只读：禁用「保存到该主题」，提示用另存
        is_builtin = name in th.builtin_names()
        self.btn_save.setEnabled(not is_builtin)
        self.btn_save.setToolTip(
            '内置主题不可直接覆盖，请用「另存为新主题」'
            if is_builtin else '')

    def _new(self):
        t = th.Theme(name='新主题')
        name, ok = QInputDialog.getText(self, '新建主题', '名称：',
                                        text='新主题')
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in th.builtin_names():
            QMessageBox.warning(self, '名称冲突', '不能占用内置主题名。')
            return
        t = t.copy(name=name)
        th.save_theme(t)
        self._load_list(select=name)
        self._load_fields(t)
        self._cur = t
        self.preview.set_theme(t)
        self.btn_save.setEnabled(True)

    def _copy(self):
        cur = self.lst.currentItem()
        base = th.load_theme(cur.data(Qt.UserRole)) if cur else self._cur
        name, ok = QInputDialog.getText(
            self, '复制主题', '新名称：', text=base['name'] + ' 副本')
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in th.builtin_names():
            QMessageBox.warning(self, '名称冲突', '不能占用内置主题名。')
            return
        t = base.copy(name=name)
        th.save_theme(t)
        self._load_list(select=name)
        self._load_fields(t)
        self._cur = t
        self.preview.set_theme(t)
        self.btn_save.setEnabled(True)

    def _del(self):
        cur = self.lst.currentItem()
        if not cur:
            return
        name = cur.data(Qt.UserRole)
        if name in th.builtin_names():
            QMessageBox.information(self, '不可删除', '内置主题不能删除。')
            return
        if QMessageBox.question(self, '删除主题',
                                f'确定删除「{name}」？') != QMessageBox.Yes:
            return
        th.delete_theme(name)
        self._load_list()

    def _import(self):
        p, _ = QFileDialog.getOpenFileName(self, '导入主题', '',
                                           '主题文件 (*.json)')
        if not p:
            return
        try:
            import json
            with open(p, 'r', encoding='utf-8') as f:
                d = json.load(f)
            t = th.Theme(**d)
        except Exception as e:
            QMessageBox.critical(self, '导入失败', str(e))
            return
        name = t['name']
        if name in th.builtin_names():
            name = name + '（导入）'
            t = t.copy(name=name)
        th.save_theme(t, name)
        self._load_list(select=name)
        self._load_fields(t)
        self._cur = t
        self.preview.set_theme(t)

    def _export(self):
        name = self._cur['name']
        p, _ = QFileDialog.getSaveFileName(self, '导出主题',
                                           name + '.json',
                                           '主题文件 (*.json)')
        if not p:
            return
        th.export_theme(self._cur, p)
        QMessageBox.information(self, '已导出', p)

    def _save(self):
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.warning(self, '名称不能为空', '请输入主题名称。')
            return
        if name in th.builtin_names():
            QMessageBox.information(
                self, '内置主题',
                '内置主题不能覆盖，请用「另存为新主题」。')
            return
        t = self._collect()
        th.save_theme(t, name)
        self._load_list(select=name)
        self._cur = t
        QMessageBox.information(self, '已保存', f'主题「{name}」已保存。')

    def _saveas(self):
        name, ok = QInputDialog.getText(self, '另存为',
                                        '新主题名称：',
                                        text=self.ed_name.text().strip()
                                        + ' 副本')
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in th.builtin_names():
            QMessageBox.warning(self, '名称冲突', '不能占用内置主题名。')
            return
        t = self._collect().copy(name=name)
        th.save_theme(t, name)
        self._load_list(select=name)
        self.ed_name.setText(name)
        self._cur = t
        QMessageBox.information(self, '已保存', f'主题「{name}」已保存。')

    def result_theme(self):
        return self._cur
