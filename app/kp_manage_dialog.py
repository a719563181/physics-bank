"""
kp_manage_dialog.py — 知识点管理窗口

功能：
  * 列出全部知识点，显示各自被多少道题引用
  * 新建 / 重命名 / 删除（删除前提示影响到的题目数）
  * 显示使用中的题目数，避免误删常用知识点

主界面菜单「工具 → 知识点管理…」打开；题目对话框里也有「管理…」按钮。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QMessageBox, QInputDialog
)
from PySide6.QtCore import Qt


class KpManageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('知识点管理')
        self.resize(560, 620)

        root = QVBoxLayout(self)

        root.addWidget(QLabel(
            '知识点可自由增删改。重命名会同步更新检索索引；\n'
            '删除只解除与题目的关联，不会删掉题目本身。'))

        # ---- 新建行 ----
        row = QHBoxLayout()
        self.e_new = QLineEdit()
        self.e_new.setPlaceholderText('输入新知识点名称，回车或点「新建」')
        self.e_new.returnPressed.connect(self.add_one)
        b_add = QPushButton('新建')
        b_add.clicked.connect(self.add_one)
        row.addWidget(self.e_new, 1)
        row.addWidget(b_add)
        root.addLayout(row)

        # ---- 列表 ----
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(['ID', '知识点', '用于题目数'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self.rename_one)
        root.addWidget(self.table, 1)

        # ---- 操作 ----
        ops = QHBoxLayout()
        b_ren = QPushButton('重命名...')
        b_ren.clicked.connect(self.rename_one)
        b_del = QPushButton('删除')
        b_del.clicked.connect(self.delete_one)
        ops.addWidget(b_ren)
        ops.addWidget(b_del)
        ops.addStretch()
        root.addLayout(ops)

        # ---- 底部 ----
        bot = QHBoxLayout()
        self.lab_n = QLabel('')
        bot.addWidget(self.lab_n)
        bot.addStretch()
        b_close = QPushButton('关闭')
        b_close.setDefault(True)
        b_close.clicked.connect(self.accept)
        bot.addWidget(b_close)
        root.addLayout(bot)

        self.load()

    # ---------------- 数据 ----------------

    def load(self):
        rows = db.list_kp()
        self.table.setRowCount(len(rows))
        native = 0
        for i, kp in enumerate(rows):
            n = db.kp_usage(kp['id'])
            if n:
                native += 1
            self.table.setItem(i, 0, QTableWidgetItem(str(kp['id'])))
            self.table.setItem(i, 1, QTableWidgetItem(kp['name']))
            it = QTableWidgetItem(str(n))
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 2, it)
            self.table.item(i, 0).setData(Qt.UserRole, kp['id'])
        self.lab_n.setText(f'共 {len(rows)} 个知识点，其中 {native} 个已被题目引用')

    def _sel_id(self):
        r = self.table.currentRow()
        if r < 0:
            return None, None
        return (self.table.item(r, 0).data(Qt.UserRole),
                self.table.item(r, 1).text())

    # ---------------- 操作 ----------------

    def add_one(self):
        name = self.e_new.text().strip()
        if not name:
            QMessageBox.information(self, '提示', '请先输入知识点名称')
            return
        kp_id = db.add_kp(name)
        if kp_id is None:
            QMessageBox.warning(self, '重名', f'已存在知识点「{name}」')
            return
        self.e_new.clear()
        self.load()
        # 选中新建的那条
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == kp_id:
                self.table.selectRow(r)
                break

    def rename_one(self):
        kp_id, old = self._sel_id()
        if kp_id is None:
            QMessageBox.information(self, '提示', '请先选中一个知识点')
            return
        new, ok = QInputDialog.getText(
            self, '重命名知识点', '新名称:', QLineEdit.Normal, old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        good, msg = db.rename_kp(kp_id, new)
        if not good:
            QMessageBox.warning(self, '无法重命名', msg)
            return
        self.load()

    def delete_one(self):
        kp_id, name = self._sel_id()
        if kp_id is None:
            QMessageBox.information(self, '提示', '请先选中一个知识点')
            return
        n = db.kp_usage(kp_id)
        if n:
            txt = (f'知识点「{name}」正被 {n} 道题引用。\n\n'
                   f'删除后这些题将失去该知识点（题目本身不受影响）。\n\n'
                   f'确定删除？')
        else:
            txt = f'确定删除知识点「{name}」？'
        box = QMessageBox(self)
        box.setWindowTitle('删除知识点')
        box.setText(txt)
        box.setIcon(QMessageBox.Warning)
        b_yes = box.addButton('删除', QMessageBox.AcceptRole)
        box.addButton('取消', QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not b_yes:
            return
        db.delete_kp(kp_id)
        self.load()
