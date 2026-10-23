"""
formula_edit.py — 公式输入控件
输入 LaTeX 源码，实时预览渲染结果；支持常用公式库（存在 db.settings）。
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel,
    QPushButton, QScrollArea, QFrame, QComboBox, QInputDialog,
    QMessageBox, QDialog, QListWidget, QListWidgetItem, QDialogButtonBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap

from formula import render_formula, diagnose, COMMON_SNIPPETS


# ========================= 分组符号面板 =========================
# 每条： (按钮显示文字, 插入的 LaTeX, 工具提示, 插入后光标相对末尾的回退位)
#
# ★ move_back = len(tex) - 目标位置。**必须用代码数，不能心算**。
#   实测我连续数错两次（\frac{}{} 当成 10 又当成 5，\left|\right| 当成 14），
#   最终这几个值由 _check_move_backs() 对着真实下标推出，可信。
#   正确落点（光标停在开括号之后）：
#     \frac{}{}      len=9   { 在 5  -> 目标 6 -> 退 3
#     \dfrac{}{}     len=10  { 在 6  -> 目标 7 -> 退 3
#     \sqrt{}        len=7   { 在 5  -> 目标 6 -> 退 1
#     \sqrt[3]{}     len=10  { 在 8  -> 目标 9 -> 退 1
#     ^{}            len=3   { 在 1  -> 目标 2 -> 退 1
#     _{}            len=3   { 在 1  -> 目标 2 -> 退 1
#     _{}^{}         len=6   { 在 1  -> 目标 2 -> 退 4（先填下标）
#     \left|\right|  len=13  | 在 5  -> 目标 6 -> 退 7
#     \left(\right)  len=13  ( 在 5  -> 目标 6 -> 退 7
#     \mathrm{}      len=9   { 在 7  -> 目标 8 -> 退 1
#     \text{}        len=7   { 在 5  -> 目标 6 -> 退 1
#     \overline{}    len=11  { 在 9  -> 目标 10 -> 退 1
#     \vec{}         len=6   { 在 4  -> 目标 5 -> 退 1
#     _{}#           len=4   # 在 3  -> 目标 3 -> 退 1（退到 # 前面）
SYMBOL_GROUPS = [
    ('分式・根号', [
        ('a/b',    r'\frac{}{}',        '分式：斜杠上下',        3),
        ('a/b 大', r'\dfrac{}{}',       '大分式（行内不缩小）',  3),
        ('√',      r'\sqrt{}',          '根号',                  1),
        ('n次√',   r'\sqrt[3]{}',       'n 次根号，把 3 改掉',   1),
        ('上标',   r'^{}',               '上标（平方用）',        1),
        ('下标',   r'_{}',               '下标',                  1),
        ('上下标', r'_{}^{}',            '同时上下标',            4),
        ('|x|',    r'\left|\right|',    '绝对值，光标在中间',    7),
        ('( )',    r'\left(\right)',    '可伸缩括号',            7),
    ]),
    ('希腊字母', [
        ('α', r'\alpha',  '', 0), ('β', r'\beta', '', 0),
        ('γ', r'\gamma',  '', 0), ('δ', r'\delta', '', 0),
        ('θ', r'\theta',  '', 0), ('λ', r'\lambda', '', 0),
        ('μ', r'\mu',     '', 0), ('π', r'\pi', '', 0),
        ('ρ', r'\rho',    '', 0), ('σ', r'\sigma', '', 0),
        ('τ', r'\tau',    '', 0), ('φ', r'\varphi', '', 0),
        ('ω', r'\omega',  '', 0), ('Δ', r'\Delta', '', 0),
        ('Ω', r'\Omega',  '', 0), ('η', r'\eta', '', 0),
    ]),
    ('运算符', [
        ('×', r'\times',  '', 0), ('÷', r'\div', '', 0),
        ('±', r'\pm',     '', 0), ('∓', r'\mp', '', 0),
        ('≈', r'\approx', '', 0), ('≠', r'\neq', '', 0),
        ('≤', r'\leq',    '', 0), ('≥', r'\geq', '', 0),
        ('∝', r'\propto', '', 0), ('∞', r'\infty', '', 0),
        ('·', r'\cdot',   '', 0), ('∑', r'\sum', '', 0),
        ('∫', r'\int',    '', 0), ('∂', r'\partial', '', 0),
        ('→', r'\to',     '', 0), ('∴', r'\therefore', '需 pandoc 公式支持', 0),
    ]),
    ('物理常用', [
        ('v₀',    r'v_0',            '初速度',                 0),
        ('Δx',    r'\Delta x',      '位移变化',               0),
        ('矢量F', r'\vec{F}',       '矢量符号',               0),
        ('平均值', r'\bar{v}',      '平均值',                 0),
        ('度',    r'^{\circ}',      '角度符号（上方）',       0),
        ('单位',  r'\,\text{m/s}', '单位，把 m/s 改掉',      0),
        ('中文下标', r'_{}#',        '中文下标：退一格删掉#',   1),
        ('复合下标', r'_{}#',        '如 v_0t：退一格删掉#',    1),
    ]),
    ('排版', [
        ('正体', r'\mathrm{}',  '正体（单位/化学式）',  1),
        ('文本', r'\text{}',    '文本（中文用）',        1),
        ('上划线', r'\overline{}', '上划线/平均',        1),
        ('矢量', r'\vec{}',     '矢量',                  1),
        ('空格', r'\,',         '细空格',                0),
        ('换行', r'\\',        '公式内换行',            0),
    ]),
]


def _tip(tex, tip):
    """按钮提示：显示 LaTeX 源码，方便照着学。"""
    return ('%s\n\nLaTeX：%s' % (tip, tex)) if tip else ('LaTeX：%s' % tex)


def _check_move_backs():
    """自检：move_back 必须正好把光标停在开括号（或 |）之后。

    ★ 不信任任何手写的数字 —— 这里从字符串本身算出「第一个空位」的位置，
      再与实际 move_back 对比。实测手算错过三次，所以要用机器算。
    返回 [(按钮名, tex, 实际落点位置, 期望位置, 字符, 是否 OK)]
    """
    out = []
    for title, items in SYMBOL_GROUPS:
        for text, tex, _tip_s, back in items:
            if back <= 0:
                continue                      # 无空位的按钮不检查
            # 该模板的第一个「空位」：第一个空 {} 的左花括号位置 + 1
            want = -1
            i = 0
            while i < len(tex):
                if tex[i] in '\\([':
                    i += 2                     # 跳过转义/开符
                    continue
                if tex[i] == '\\':
                    i += 1
                    # 跳过命令名
                    while i < len(tex) and tex[i].isalpha():
                        i += 1
                    continue
                if tex[i] == '<' and i + 1 < len(tex):  # 此处不用 <>
                    i += 1
                    continue
                if tex[i] in '{[':
                    close = '}' if tex[i] == '{' else ']'
                    # 空括号 = 下一个字符就是闭符
                    if i + 1 < len(tex) and tex[i + 1] == close:
                        want = i + 1
                        break
                    # 非空括号直接跳过整块
                    depth = 0
                    j = i
                    while j < len(tex):
                        if tex[j] == tex[i]:
                            depth += 1
                        elif tex[j] == close:
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    i = j + 1
                    continue
                i += 1
            # 竖线型（\left| ... \right|）：取两个 | 之间
            if want < 0 and '|' in tex:
                a = tex.find('|')
                b = tex.find('|', a + 1)
                if b > a:
                    want = a + 1
            # \leftX ... \rightY 型：X 与 Y 之间
            if want < 0 and '\\left' in tex and '\\right' in tex:
                after_left = tex.index('\\left') + len('\\left')
                want = after_left + 1          # 跳过 X 本身，落在其右
            got = len(tex) - back
            # 允许落在 # 占位处（_}# 这类）
            good = (got == want) or (0 <= got < len(tex) and tex[got] == '#')
            out.append((text, tex, got, want, tex[got] if 0 <= got < len(tex) else '', good))
    return out


class _GroupBox(QWidget):
    """一个可折叠的符号分组：标题可点，展开后是一堆小按钮。"""

    def __init__(self, title, items, on_pick):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 2)
        lay.setSpacing(2)

        self.btn_head = QPushButton('▾ %s' % title)
        self.btn_head.setCheckable(True)
        self.btn_head.setChecked(True)
        self.btn_head.setStyleSheet(
            'QPushButton{text-align:left; border:none; font-weight:bold;'
            ' color:#2b4fa8; padding:2px 4px; background:transparent;}'
            'QPushButton:hover{background:#eef3ff; border-radius:3px;}')
        self.btn_head.clicked.connect(self._toggle)
        lay.addWidget(self.btn_head)

        self.body = QWidget()
        flow = QHBoxLayout(self.body)
        flow.setContentsMargins(10, 0, 0, 2)
        flow.setSpacing(3)
        for text, tex, tip, back in items:
            b = QPushButton(text)
            b.setMinimumWidth(34)
            b.setMaximumWidth(72)
            b.setToolTip(_tip(tex, tip))
            b.setStyleSheet('font-size:11px; padding:2px 5px;')
            b.clicked.connect(
                lambda checked=False, t=tex, k=back: on_pick(t, k))
            flow.addWidget(b)
        flow.addStretch()
        lay.addWidget(self.body)

    def _toggle(self):
        vis = self.btn_head.isChecked()
        self.body.setVisible(vis)
        t = self.btn_head.text()[2:]
        self.btn_head.setText(('▾ ' if vis else '▸ ') + t)


class FormulaPreview(QLabel):
    """显示单个公式渲染结果。同时对外报告 render_ok，
    让上层（诊断条）不用重复渲染一次。"""

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(40)
        self.setStyleSheet('background:#fafafa; border:1px solid #ddd; border-radius:3px;')
        self._pix = None
        self.render_ok = False

    def show_latex(self, latex):
        path = render_formula(latex)
        if path:
            pix = QPixmap(path)
            self._pix = pix
            self.render_ok = True
            if pix.width() > 560:
                pix = pix.scaledToWidth(560, Qt.SmoothTransformation)
            self.setPixmap(pix)
            self.setText('')
        else:
            self.render_ok = False
            self.setPixmap(QPixmap())
            if latex.strip():
                # 具体原因由上层诊断条说明，这里只给一个醒目的占位
                self.setText('⚠ 无法渲染')
                self.setStyleSheet(
                    'background:#fff4f4; border:1px solid #e0b4b4; '
                    'border-radius:3px; color:#a33;')
            else:
                self.setText('')
                self.setStyleSheet(
                    'background:#fafafa; border:1px solid #ddd; border-radius:3px;')


class FormulaLibraryDialog(QDialog):
    """常用公式库管理：新增 / 改名 / 删除。带渲染预览。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('常用公式库')
        self.resize(760, 560)
        import db
        self.db = db

        root = QVBoxLayout(self)
        root.addWidget(QLabel('这些公式会出现在录入界面的「公式库」下拉框里，一键插入。'))

        body = QHBoxLayout()
        root.addLayout(body, 1)

        self.list = QListWidget()
        self.list.setMinimumWidth(320)
        self.list.currentRowChanged.connect(self._preview)
        body.addWidget(self.list, 1)

        right = QVBoxLayout()
        self.pv = FormulaPreview()
        self.pv.setMinimumHeight(90)
        right.addWidget(self.pv)
        self.lbl_tex = QLabel('')
        self.lbl_tex.setWordWrap(True)
        self.lbl_tex.setStyleSheet('color:#555; font-family:Consolas; font-size:11px;')
        self.lbl_tex.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right.addWidget(self.lbl_tex)
        right.addStretch()
        body.addLayout(right, 1)

        row = QHBoxLayout()
        b_add = QPushButton('新增…')
        b_add.clicked.connect(self._add)
        b_ren = QPushButton('改名…')
        b_ren.clicked.connect(self._rename)
        b_del = QPushButton('删除')
        b_del.clicked.connect(self._del)
        row.addWidget(b_add)
        row.addWidget(b_ren)
        row.addWidget(b_del)
        row.addStretch()
        root.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        root.addWidget(bb)

        self._reload()

    def _reload(self):
        self.list.clear()
        for it in self.db.list_formula_lib():
            name = it['name'] or '（无名）'
            item = QListWidgetItem('%s\n    %s' % (name, it['latex']))
            item.setData(Qt.UserRole, it['latex'])
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _cur_latex(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _preview(self, row):
        latex = self._cur_latex()
        self.lbl_tex.setText(latex or '')
        self.pv.show_latex(latex or '')

    def _add(self):
        tex, ok = QInputDialog.getText(
            self, '新增常用公式', 'LaTeX 源码（不用写 $）：')
        if not ok or not tex.strip():
            return
        tex = tex.strip()
        good, why = diagnose(tex)
        if not good:
            QMessageBox.warning(self, '无法渲染', why)
            return
        name, ok2 = QInputDialog.getText(
            self, '公式名称', '给这个公式起个名字（可空）：')
        if not ok2:
            return
        if not self.db.add_formula_lib(name, tex):
            QMessageBox.information(self, '提示', '这个公式已经在库里了。')
            return
        self._reload()

    def _rename(self):
        latex = self._cur_latex()
        if not latex:
            QMessageBox.information(self, '提示', '先选中一条。')
            return
        cur_name = ''
        for it in self.db.list_formula_lib():
            if it['latex'] == latex:
                cur_name = it['name']
        new, ok = QInputDialog.getText(
            self, '改名字', '新名字（可空）：', text=cur_name)
        if not ok:
            return
        self.db.rename_formula_lib(latex, new)
        self._reload()

    def _del(self):
        latex = self._cur_latex()
        if not latex:
            QMessageBox.information(self, '提示', '先选中一条。')
            return
        if QMessageBox.question(self, '删除', '确定删除这条常用公式？') == QMessageBox.Yes:
            self.db.delete_formula_lib(latex)
            self._reload()


class FormulaInput(QWidget):
    """
    公式录入控件：文本框 + 实时预览 + 人话诊断 + 分组符号面板 + 公式库
    父级在提交时读 latex()。
    """

    def __init__(self, on_insert=None):
        super().__init__()
        self._on_insert = on_insert

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ---- 输入框 + 预览并排 ----
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QTextEdit()
        self.edit.setPlaceholderText(
            r'输入 LaTeX，例如 \frac{1}{2}mv^2   —— 不会写就直接点下面的符号按钮')
        self.edit.setMinimumHeight(64)
        self.edit.setMaximumHeight(96)
        row.addWidget(self.edit, 1)

        self.preview = FormulaPreview()
        self.preview.setMinimumWidth(190)
        row.addWidget(self.preview, 1)
        layout.addLayout(row)

        # ---- 诊断条（有错才显示）----
        self.lbl_diag = QLabel('')
        self.lbl_diag.setWordWrap(True)
        self.lbl_diag.setVisible(False)
        layout.addWidget(self.lbl_diag)

        # ---- 分组符号面板（可折叠）----
        self.cat = QComboBox()
        self.cat.addItem('全部')
        for title, _items in SYMBOL_GROUPS:
            self.cat.addItem(title)
        self.cat.setToolTip('只要某一类符号时，在这里筛选')
        self.cat.currentIndexChanged.connect(self._filter_groups)
        layout.addWidget(self.cat)

        self.groups = []
        for title, items in SYMBOL_GROUPS:
            g = _GroupBox(title, items, self._insert_template)
            self.groups.append(g)
            layout.addWidget(g)

        # ---- 公式库 ----
        lib_row = QHBoxLayout()
        lib_row.setSpacing(4)
        lib_row.addWidget(QLabel('公式库:'))
        self.c_lib = QComboBox()
        self.c_lib.setMinimumWidth(200)
        self.c_lib.addItem('（选择常用公式插入）', '')
        self.c_lib.currentIndexChanged.connect(self._pick_lib)
        lib_row.addWidget(self.c_lib, 1)
        b_save = QPushButton('存入')
        b_save.setToolTip('把当前文本框里的公式存进公式库，以后一键复用')
        b_save.clicked.connect(self._save_to_lib)
        lib_row.addWidget(b_save)
        b_manage = QPushButton('管理…')
        b_manage.setToolTip('改名 / 删除 / 新增常用公式')
        b_manage.clicked.connect(self._manage_lib)
        lib_row.addWidget(b_manage)
        layout.addLayout(lib_row)
        self.reload_lib()

        # 防抖预览 + 诊断
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._refresh)
        self.edit.textChanged.connect(self._timer.start)

    # ---- 分组筛选 ----
    def _filter_groups(self, idx):
        want = self.cat.currentText()
        for title, g in zip([t for t, _ in SYMBOL_GROUPS], self.groups):
            g.setVisible(want in ('全部', title))

    # ---- 公式库 ----
    def reload_lib(self):
        import db
        self.c_lib.blockSignals(True)
        self.c_lib.clear()
        self.c_lib.addItem('（选择常用公式插入）', '')
        for it in db.list_formula_lib():
            label = '%s    %s' % (it['name'] or '（无名）', it['latex'])
            self.c_lib.addItem(label, it['latex'])
        self.c_lib.blockSignals(False)

    def _pick_lib(self, idx):
        tex = self.c_lib.itemData(idx)
        if tex:
            self.edit.setPlainText(tex)
            self._refresh()
        # 复位，方便下次再选同一条
        self.c_lib.setCurrentIndex(0)

    def _save_to_lib(self):
        import db
        tex = self.edit.toPlainText().strip()
        if not tex:
            QMessageBox.information(self, '提示', '先在上面输入公式。')
            return
        ok, why = diagnose(tex)
        if not ok:
            QMessageBox.warning(self, '无法渲染', why)
            return
        if any(it['latex'] == tex for it in db.list_formula_lib()):
            QMessageBox.information(self, '提示', '这个公式已经在公式库里了。')
            return
        names = db.get_formula_lib_names()
        hint = ('已有：' + '、'.join(names[:6])) if names else '如：牛顿第二定律'
        name, ok2 = QInputDialog.getText(
            self, '存入公式库', '起个名字（可空）：\n' + hint)
        if not ok2:
            return
        db.add_formula_lib(name, tex)
        self.reload_lib()
        QMessageBox.information(self, '已保存', '已存入公式库。')

    def _manage_lib(self):
        dlg = FormulaLibraryDialog(self)
        # ★ 关闭即销毁：这个框从每个 $...$ 输入点都能开，不销毁会累积
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.exec()
        self.reload_lib()

    def _insert_template(self, tex, move_back=0):
        """插入符号模板，并把光标退回第一个空位。

        ★ 关键：move_back 是从「插入后光标位置」往回退的字符数。
          例：\frac{}{} 插 10 字符，退 6 位→落在分子括号内。
        """
        cur = self.edit.textCursor()
        cur.insertText(tex)
        if move_back:
            for _ in range(move_back):
                cur.movePosition(cur.MoveOperation.Left)
        self.edit.setTextCursor(cur)
        self._timer.start()

    def _refresh(self):
        tex = self.edit.toPlainText()
        self.preview.show_latex(tex)
        self._show_diag(tex)

    def _show_diag(self, tex):
        """诊断条：空/正常时不占地方，有错时红底说人话。"""
        if not tex.strip():
            self.lbl_diag.setVisible(False)
            return
        if self.preview.render_ok:
            self.lbl_diag.setVisible(False)
            return
        ok, why = diagnose(tex)
        if ok:
            self.lbl_diag.setVisible(False)
            return
        self.lbl_diag.setText('⚠ ' + why)
        self.lbl_diag.setStyleSheet(
            'background:#fff4f4; border:1px solid #e0b4b4; border-radius:3px;'
            ' color:#a33; padding:4px 6px; font-size:12px;')
        self.lbl_diag.setVisible(True)

    def latex(self):
        return self.edit.toPlainText().strip()

    def set_latex(self, tex):
        self.edit.setPlainText(tex or '')
        self._refresh()
