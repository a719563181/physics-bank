"""
excel_import_dialog.py — Excel/CSV 批量导入界面
流程：选文件 → 预览解析结果（表头映射 + 逐题预览）→ 导入 → 报告
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import excel_import

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit,
    QTableWidget, QTableWidgetItem, QSplitter, QMessageBox, QFileDialog,
    QGroupBox, QCheckBox, QWidget, QHeaderView, QAbstractItemView, QLineEdit
)
from PySide6.QtCore import Qt

TYPE_CN = {'choice_single': '单选题', 'choice_multi': '多选题',
           'fill': '填空题', 'judgment': '判断题',
           'calculation': '计算题', 'experiment': '实验题'}


def _default_imports_dir():
    """导入文件对话框的默认目录；跟随 db.ROOT，不存在则返回空串。"""
    d = os.path.join(db.ROOT, 'imports')
    return d if os.path.isdir(d) else ''


class ExcelImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Excel / CSV 批量导入')
        self.resize(1200, 720)
        self.preview_data = None

        root = QVBoxLayout(self)

        # ---- 文件选择 ----
        box = QGroupBox('数据文件')
        bl = QHBoxLayout(box)
        self.e_path = QLineEdit()
        self.e_path.setPlaceholderText('选择 .xlsx / .xls / .csv 文件...')
        self.e_path.textChanged.connect(self.on_path_change)
        b_pick = QPushButton('选择文件')
        b_pick.clicked.connect(self.pick)
        b_tpl = QPushButton('下载模版')
        b_tpl.clicked.connect(self.save_template)
        bl.addWidget(self.e_path, 1)
        bl.addWidget(b_pick)
        bl.addWidget(b_tpl)
        root.addWidget(box)

        # ---- 表头映射 ----
        self.lab_map = QLabel('选择文件后显示表头识别结果')
        self.lab_map.setWordWrap(True)
        self.lab_map.setStyleSheet(
            'background:#f0f4f8; padding:6px; border-radius:4px;'
            'font-family:Consolas; font-size:11px; color:#333;')
        root.addWidget(self.lab_map)

        # ---- 预览表 ----
        split = QSplitter(Qt.Vertical)

        self.tbl = QTableWidget()
        self.tbl.setColumnCount(6)
        self.tbl.setHorizontalHeaderLabels(
            ['Excel行', '题型', '难度', '知识点', '答案', '题干'])
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.clicked.connect(self.on_row)
        split.addWidget(self.tbl)

        self.txt_detail = QTextEdit()
        self.txt_detail.setReadOnly(True)
        self.txt_detail.setStyleSheet(
            'font-family:Consolas; font-size:11px; background:#fafafa;')
        self.txt_detail.setPlaceholderText('点击上方某行查看完整内容...')
        split.addWidget(self.txt_detail)
        split.setSizes([420, 240])
        root.addWidget(split, 1)

        # ---- 问题提示 ----
        self.lab_prob = QLabel('')
        self.lab_prob.setWordWrap(True)
        self.lab_prob.setStyleSheet('color:#b35; font-size:11px;')
        root.addWidget(self.lab_prob)

        # ---- 底部 ----
        bot = QHBoxLayout()
        self.cb_src = QCheckBox('用文件名作为「来源」（行内没有来源时）')
        self.cb_src.setChecked(True)
        bot.addWidget(self.cb_src)
        bot.addStretch()
        self.lab_stat = QLabel('')
        bot.addWidget(self.lab_stat)
        self.b_import = QPushButton('导入全部')
        self.b_import.setEnabled(False)
        self.b_import.clicked.connect(self.do_import)
        self.b_import.setStyleSheet('font-weight:bold; padding:4px 14px;')
        bot.addWidget(self.b_import)
        b_close = QPushButton('关闭')
        b_close.clicked.connect(self.reject)
        bot.addWidget(b_close)
        root.addLayout(bot)

    # ------------------------------------------------ 文件

    def pick(self):
        p, _ = QFileDialog.getOpenFileName(
            self, '选择数据文件', _default_imports_dir(),
            '表格文件 (*.xlsx *.xls *.csv);;Excel (*.xlsx *.xls);;CSV (*.csv);;所有文件 (*)')
        if p:
            self.e_path.setText(p)

    def save_template(self):
        d = _default_imports_dir()
        p, _ = QFileDialog.getSaveFileName(
            self, '保存导入模版',
            os.path.join(d, '题目导入模版.xlsx') if d else '题目导入模版.xlsx',
            'Excel (*.xlsx);;CSV (*.csv)')
        if not p:
            return
        try:
            excel_import.write_template(p)
            QMessageBox.information(
                self, '已生成',
                f'模版已保存：\n{p}\n\n'
                '模版含 3 道示例题和 14 列说明。\n'
                '题干里的公式用 $...$ 包住，例如 $v=v_0+at$。')
        except Exception as e:
            QMessageBox.critical(self, '出错', str(e))

    def on_path_change(self, p):
        p = p.strip()
        if not p or not os.path.exists(p):
            return
        try:
            self.preview_data = excel_import.preview(p)
        except Exception as e:
            self.lab_map.setText(f'读取失败：{e}')
            self.b_import.setEnabled(False)
            return
        self.show_preview()

    # ------------------------------------------------ 预览

    def show_preview(self):
        pv = self.preview_data
        if not pv:
            return
        if not pv['mapped']:
            self.lab_map.setText(
                '⚠ 没有识别到任何列。请确认表头行包含「题干」等名称，'
                '或点「下载模版」按模版格式填写。')
            self.b_import.setEnabled(False)
            return

        parts = [f"<b>{os.path.basename(self.e_path.text())}</b> — "
                 f"识别到 {len(pv['rows'])} 道题"]
        parts.append('<br>列映射：' + '，'.join(
            f"{k}→{v}" for k, v in pv['mapped'].items()))
        self.lab_map.setText(''.join(parts))

        rows = pv['rows']
        self.tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = [str(r['excel_row']),
                    TYPE_CN.get(r['question_type'], r['question_type']),
                    str(r['difficulty']),
                    '、'.join(r['kp']),
                    r['answer'],
                    r['stem']]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    it.setTextAlignment(Qt.AlignCenter)
                self.tbl.setItem(i, c, it)
        self.tbl.resizeColumnsToContents()
        self.tbl.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.Stretch)

        probs = pv['problems']
        self.lab_prob.setText(
            ('⚠ ' + '；'.join(probs[:5]) + (f'（共 {len(probs)} 条）'
             if len(probs) > 5 else '')) if probs else '✓ 未发现明显问题')
        self.lab_prob.setStyleSheet(
            'color:#b35; font-size:11px;' if probs else
            'color:#2a7; font-size:11px;')

        self.lab_stat.setText(f'待导入 {len(rows)} 题')
        self.b_import.setEnabled(len(rows) > 0)
        if rows:
            self.tbl.selectRow(0)
            self.on_row(0, 0)

    def on_row(self, row, _col=None):
        if not self.preview_data or row < 0 or row >= len(self.preview_data['rows']):
            return
        r = self.preview_data['rows'][row]
        L = [f"Excel 第 {r['excel_row']} 行",
             f"题型: {TYPE_CN.get(r['question_type'])}   难度: {r['difficulty']}",
             f"知识点: {'、'.join(r['kp']) or '(无)'}",
             '', '—— 题干 ——', r['stem']]
        if r['options']:
            L.append('')
            L.append('—— 选项 ——')
            for o in r['options']:
                mark = '  ✓' if o.get('is_correct') else ''
                L.append(f"{o['label']}. {o['content']}{mark}")
        if r['answer']:
            L += ['', f"—— 答案 ——  {r['answer']}"]
        if r['solution']:
            L += ['', '—— 解析 ——', r['solution']]
        self.txt_detail.setPlainText('\n'.join(L))

    # ------------------------------------------------ 导入

    def do_import(self):
        p = self.e_path.text().strip()
        if not p:
            return
        n = len(self.preview_data['rows'])
        if QMessageBox.question(
                self, '确认导入',
                f'把 {n} 道题写入题库？\n\n'
                '题干中的 $...$ 会作为公式保存，可在主界面预览。'
                ) != QMessageBox.Yes:
            return

        try:
            default_src = (os.path.splitext(os.path.basename(p))[0]
                           if self.cb_src.isChecked() else '')
            res = excel_import.do_import(p, default_src)
        except Exception as e:
            QMessageBox.critical(self, '导入失败',
                                 f'{e}\n\n{traceback.format_exc()}')
            return

        msg = f"成功导入 {res['ok']} 道题。"
        if res['new_kp']:
            msg += f"\n\n自动新建知识点 {len(res['new_kp'])} 个：\n" + \
                   '、'.join(res['new_kp'])
        if res['failed']:
            msg += f"\n\n失败 {len(res['failed'])} 行：\n" + '\n'.join(
                f"  第{r}行: {e}" for r, e in res['failed'][:10])
        QMessageBox.information(self, '导入完成', msg)
        self.accept()
