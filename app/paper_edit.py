"""
paper_edit.py — 组卷对话框
左侧：题库候选（可筛选）  右侧：已选题目（可排序/设分）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QLabel, QAbstractItemView, QHeaderView,
    QMessageBox, QGroupBox, QSplitter, QSpinBox, QDoubleSpinBox, QCheckBox,
    QFileDialog, QWidget, QFormLayout
)
from PySide6.QtCore import Qt

TYPE_LABELS = {'choice_single': '单选题', 'choice_multi': '多选题',
               'fill': '填空题', 'judgment': '判断题',
               'calculation': '计算题', 'experiment': '实验题'}


class PaperEditDialog(QDialog):
    def __init__(self, parent=None, pid=None):
        super().__init__(parent)
        self.pid = pid
        self.setWindowTitle('组卷 — ' + ('编辑试卷' if pid else '新建试卷'))
        self.resize(1200, 760)

        root = QVBoxLayout(self)

        # ---- 试卷信息 ----
        info = QGroupBox('试卷信息')
        il = QFormLayout(info)
        self.e_title = QLineEdit()
        self.e_title.setPlaceholderText('例：高三物理十月月考卷')
        self.e_desc = QLineEdit()
        self.e_desc.setPlaceholderText('副标题/说明（可空）')
        il.addRow('试卷名称:', self.e_title)
        il.addRow('说明:', self.e_desc)
        root.addWidget(info)

        split = QSplitter(Qt.Horizontal)

        # ---- 左：候选题库 ----
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        filt = QHBoxLayout()
        self.f_type = QComboBox()
        self.f_type.addItem('全部题型', '')
        for k, v in TYPE_LABELS.items():
            self.f_type.addItem(v, k)
        self.f_kp = QComboBox()
        self.f_kp.addItem('全部知识点', 0)
        for kp in db.list_kp():
            self.f_kp.addItem(kp['name'], kp['id'])
        self.f_kw = QLineEdit()
        self.f_kw.setPlaceholderText('关键词')
        self.f_kw.setMaximumWidth(140)
        b_f = QPushButton('筛选')
        b_f.clicked.connect(self.load_candidates)
        filt.addWidget(self.f_type)
        filt.addWidget(self.f_kp)
        filt.addWidget(self.f_kw)
        filt.addWidget(b_f)
        filt.addStretch()
        ll.addLayout(filt)

        self.t_cand = QTableWidget()
        self.t_cand.setColumnCount(5)
        self.t_cand.setHorizontalHeaderLabels(['序号', '题型', '难度', '来源', '题干'])
        self.t_cand.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_cand.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.t_cand.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.t_cand.verticalHeader().setVisible(False)
        # 本页行序 → 真实 id（列表不显示真实 id）
        self._cand_qids = []
        hh = self.t_cand.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.t_cand.doubleClicked.connect(self.add_selected)
        ll.addWidget(self.t_cand)

        bl = QHBoxLayout()
        b_add = QPushButton('加入试卷 →')
        b_add.clicked.connect(self.add_selected)
        b_addall = QPushButton('全部加入')
        b_addall.clicked.connect(self.add_all)
        bl.addWidget(b_add)
        bl.addWidget(b_addall)
        ll.addLayout(bl)
        split.addWidget(left)

        # ---- 右：已选 ----
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        self.lab_sum = QLabel('已选 0 题，共 0 分')
        rl.addWidget(self.lab_sum)

        self.t_pick = QTableWidget()
        self.t_pick.setColumnCount(5)
        self.t_pick.setHorizontalHeaderLabels(
            ['卷序', '题型', '分值', '题干', '操作'])
        self.t_pick.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_pick.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.t_pick.verticalHeader().setVisible(False)
        ph = self.t_pick.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        ph.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ph.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        ph.setSectionResizeMode(3, QHeaderView.Stretch)
        ph.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        rl.addWidget(self.t_pick)

        bl2 = QHBoxLayout()
        b_up = QPushButton('↑ 上移')
        b_up.clicked.connect(lambda: self.move_pick(-1))
        b_dn = QPushButton('↓ 下移')
        b_dn.clicked.connect(lambda: self.move_pick(1))
        b_rm = QPushButton('移除')
        b_rm.clicked.connect(self.remove_pick)
        b_clr = QPushButton('清空')
        b_clr.clicked.connect(self.clear_pick)
        for b in (b_up, b_dn, b_rm, b_clr):
            bl2.addWidget(b)
        bl2.addStretch()
        rl.addLayout(bl2)

        # 批量设分
        sb = QHBoxLayout()
        sb.addWidget(QLabel('统一分值:'))
        self.sp_score = QDoubleSpinBox()
        self.sp_score.setRange(0, 100)
        self.sp_score.setValue(4)
        self.sp_score.setDecimals(1)
        sb.addWidget(self.sp_score)
        b_setall = QPushButton('应用到全部')
        b_setall.clicked.connect(self.apply_score_all)
        sb.addWidget(b_setall)
        sb.addStretch()
        rl.addLayout(sb)

        split.addWidget(right)
        split.setSizes([640, 560])
        root.addWidget(split, 1)

        # ---- 底部 ----
        bot = QHBoxLayout()
        self.cb_ans = QCheckBox('含答案')
        self.cb_ans.setChecked(True)
        self.cb_sol = QCheckBox('含解析')
        self.cb_sol.setChecked(True)
        self.cb_fig = QCheckBox('含配图')
        self.cb_fig.setChecked(True)
        bot.addWidget(self.cb_ans)
        bot.addWidget(self.cb_sol)
        bot.addWidget(self.cb_fig)
        bot.addStretch()

        b_save = QPushButton('保存试卷')
        b_save.clicked.connect(self.save_paper)
        b_export = QPushButton('保存并导出 Word')
        b_export.clicked.connect(self.save_and_export)
        b_close = QPushButton('关闭')
        b_close.clicked.connect(self.reject)
        bot.addWidget(b_save)
        bot.addWidget(b_export)
        bot.addWidget(b_close)
        root.addLayout(bot)

        self.picked = []   # [{'id','stem','type','score'}]

        if pid:
            self.load_paper(pid)
        self.load_candidates()

    # ---------------- 数据 ----------------

    def load_candidates(self):
        rows = db.list_questions(
            question_type=self.f_type.currentData() or None,
            kp_id=self.f_kp.currentData() or None,
            keyword=self.f_kw.text().strip() or None)
        picked_ids = {p['id'] for p in self.picked}
        rows = [r for r in rows if r['id'] not in picked_ids]

        self.t_cand.setRowCount(len(rows))
        self._cand_qids = [q['id'] for q in rows]
        for i, q in enumerate(rows):
            self.t_cand.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.t_cand.setItem(i, 1, QTableWidgetItem(
                TYPE_LABELS.get(q['question_type'], q['question_type'])))
            self.t_cand.setItem(i, 2, QTableWidgetItem(str(q['difficulty'] or '')))
            self.t_cand.setItem(i, 3, QTableWidgetItem(q['source'] or ''))
            stem = (q['stem'] or '').replace('\n', ' ')
            self.t_cand.setItem(i, 4, QTableWidgetItem(
                stem[:80] + ('…' if len(stem) > 80 else '')))
            self.t_cand.item(i, 0).setData(Qt.UserRole, q['id'])

    def load_paper(self, pid):
        p = db.get_paper(pid)
        if not p:
            return
        self.e_title.setText(p['title'])
        self.e_desc.setText(p.get('description') or '')
        self.picked = []
        for q in p['questions']:
            self.picked.append({
                'id': q['id'],
                'stem': q['stem'] or '',
                'type': q['question_type'],
                'score': q.get('pq_score') or 0,
            })
        self.refresh_pick()

    def refresh_pick(self):
        self.t_pick.setRowCount(len(self.picked))
        for i, p in enumerate(self.picked):
            self.t_pick.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.t_pick.setItem(i, 1, QTableWidgetItem(
                TYPE_LABELS.get(p['type'], p['type'])))
            self.t_pick.setItem(i, 2, QTableWidgetItem(f"{p['score']:g}"))
            stem = (p['stem'] or '').replace('\n', ' ')
            self.t_pick.setItem(i, 3, QTableWidgetItem(
                stem[:70] + ('…' if len(stem) > 70 else '')))
            b = QPushButton('×')
            b.setMaximumWidth(30)
            b.clicked.connect(lambda _=False, idx=i: self.remove_at(idx))
            self.t_pick.setCellWidget(i, 4, b)
            self.t_pick.item(i, 0).setData(Qt.UserRole, p['id'])

        tot = sum(p['score'] for p in self.picked)
        self.lab_sum.setText(f'已选 {len(self.picked)} 题，共 {tot:g} 分')

    # ---------------- 操作 ----------------

    def _cand_ids(self, all_rows=False):
        if all_rows:
            return [self.t_cand.item(r, 0).data(Qt.UserRole)
                    for r in range(self.t_cand.rowCount())]
        return [self.t_cand.item(r, 0).data(Qt.UserRole)
                for r in {i.row() for i in self.t_cand.selectedIndexes()}]

    def add_selected(self):
        self._add_ids(self._cand_ids())
        self.load_candidates()

    def add_all(self):
        self._add_ids(self._cand_ids(all_rows=True))
        self.load_candidates()

    def _add_ids(self, ids):
        default = self.sp_score.value()
        for qid in ids:
            if qid is None or any(p['id'] == qid for p in self.picked):
                continue
            q = db.get_question(qid)
            if not q:
                continue
            self.picked.append({
                'id': qid, 'stem': q['stem'] or '',
                'type': q['question_type'], 'score': default})
        self.refresh_pick()

    def remove_pick(self):
        rows = sorted({i.row() for i in self.t_pick.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.picked):
                self.picked.pop(r)
        self.refresh_pick()
        self.load_candidates()

    def remove_at(self, idx):
        if 0 <= idx < len(self.picked):
            self.picked.pop(idx)
            self.refresh_pick()
            self.load_candidates()

    def move_pick(self, delta):
        rows = sorted({i.row() for i in self.t_pick.selectedIndexes()})
        if not rows:
            return
        r = rows[0]
        j = r + delta
        if 0 <= j < len(self.picked):
            self.picked[r], self.picked[j] = self.picked[j], self.picked[r]
            self.refresh_pick()
            self.t_pick.selectRow(j)

    def clear_pick(self):
        self.picked = []
        self.refresh_pick()
        self.load_candidates()

    def apply_score_all(self):
        v = self.sp_score.value()
        for p in self.picked:
            p['score'] = v
        self.refresh_pick()

    # ---------------- 保存 / 导出 ----------------

    def _validate(self):
        if not self.e_title.text().strip():
            QMessageBox.warning(self, '提示', '请填写试卷名称')
            return False
        if not self.picked:
            QMessageBox.warning(self, '提示', '试卷里还没有题目')
            return False
        return True

    def save_paper(self, silent=False):
        if not self._validate():
            return None
        title = self.e_title.text().strip()
        desc = self.e_desc.text().strip()
        if self.pid:
            conn = db.get_conn()
            conn.execute('UPDATE papers SET title=?, description=? WHERE id=?',
                         (title, desc, self.pid))
            conn.execute('DELETE FROM paper_questions WHERE paper_id=?', (self.pid,))
            conn.commit()
            conn.close()
            pid = self.pid
        else:
            pid = db.add_paper(title, desc)
            self.pid = pid
        for i, p in enumerate(self.picked):
            db.add_paper_question(pid, p['id'], sort_order=i, score=p['score'])
        if not silent:
            QMessageBox.information(self, '完成', f'试卷已保存（id={pid}）')
        return pid

    def save_and_export(self):
        pid = self.save_paper(silent=True)
        if not pid:
            return
        title = self.e_title.text().strip()
        safe = ''.join(c for c in title if c not in '\\/:*?"<>|').strip() or f'paper{pid}'
        default = os.path.join(db.export_dir(), safe + '.docx')
        path, _ = QFileDialog.getSaveFileName(
            self, '导出为 Word', default, 'Word 文档 (*.docx)')
        if not path:
            return
        try:
            import export_docx
            out, stats = export_docx.export_paper(
                pid, path,
                include_answer=self.cb_ans.isChecked(),
                include_solution=self.cb_sol.isChecked(),
                include_figures=self.cb_fig.isChecked())
            msg = (f"导出成功\n\n{out}\n\n"
                   f"题目 {stats['questions']} 道，共 {stats['total_score']:g} 分\n"
                   f"公式：原生 {stats['formula_ok']} 个"
                   + (f"，失败 {stats['formula_fail']} 个" if stats['formula_fail'] else '')
                   + f"\n配图 {stats['figures']} 张")
            QMessageBox.information(self, '导出完成', msg)
        except Exception as e:
            QMessageBox.critical(self, '导出失败', str(e))
