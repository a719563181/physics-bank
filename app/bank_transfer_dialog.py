"""
bank_transfer_dialog.py — 题库导出 / 导入 对话框

菜单入口：[工具] -> 题库导出 / 导入
两个页签：
  · 导出题库 —— 打成一个 .pbk 文件，可拷到别的电脑
  · 导入题库 —— 从 .pbk 文件恢复/合并进来
"""

import os
import traceback
from datetime import datetime

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget,
    QWidget, QFileDialog, QMessageBox, QCheckBox, QTextEdit, QGroupBox,
    QFormLayout, QRadioButton, QButtonGroup, QProgressBar
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

import db


def _fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit) if unit != 'B' else '%d B' % n
        n /= 1024.0


def _default_name():
    return '物理题库_%s.pbk' % datetime.now().strftime('%Y%m%d')


class BankTransferDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('题库导出 / 导入')
        self.resize(760, 580)
        self._log_lines = []

        root = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_export_tab(), '  导出题库  ')
        tabs.addTab(self._build_import_tab(), '  导入题库  ')
        root.addWidget(tabs, 1)

        # 共用日志区
        box = QGroupBox('运行日志')
        lay = QVBoxLayout(box)
        self.t_log = QTextEdit()
        self.t_log.setReadOnly(True)
        self.t_log.setFont(QFont('Consolas', 9))
        self.t_log.setMinimumHeight(150)
        lay.addWidget(self.t_log)
        root.addWidget(box, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        b_close = QPushButton('关闭')
        b_close.clicked.connect(self.accept)
        bottom.addWidget(b_close)
        root.addLayout(bottom)

        self._refresh_local_stats()

    # ---------------------------------------------------------------- 日志
    def log(self, msg):
        self._log_lines.append(str(msg))
        self.t_log.setPlainText('\n'.join(self._log_lines))
        self.t_log.verticalScrollBar().setValue(
            self.t_log.verticalScrollBar().maximum())
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

    # ---------------------------------------------------------------- 导出页
    def _build_export_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        info = QGroupBox('本机题库现状')
        f = QFormLayout(info)
        self.l_local = QLabel('—')
        f.addRow('内容：', self.l_local)
        self.l_path = QLabel(db.DB_PATH)
        self.l_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.l_path.setStyleSheet('color:#666;')
        f.addRow('数据文件：', self.l_path)
        lay.addWidget(info)

        opt = QGroupBox('导出选项')
        o = QVBoxLayout(opt)
        self.cb_att = QCheckBox('包含题目配图（图片较大，建议勾选）')
        self.cb_att.setChecked(True)
        o.addWidget(self.cb_att)
        lay.addWidget(opt)

        hint = QLabel(
            '导出的 .pbk 文件可以在另一台电脑上通过「导入题库」恢复。\n'
            '包含：题目、知识点、试卷、配图、公式库、自定义 PPT 模板。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color:#555;padding:6px;')
        lay.addWidget(hint)

        lay.addStretch(1)
        b = QPushButton('导出为 .pbk 文件...')
        b.setMinimumHeight(38)
        b.clicked.connect(self.do_export)
        lay.addWidget(b)
        return w

    def do_export(self):
        default = os.path.join(db.export_dir(), _default_name())
        path, _ = QFileDialog.getSaveFileName(
            self, '导出题库', default, '题库包 (*.pbk);;压缩包 (*.zip)')
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += '.pbk'

        self._log_lines = []
        self.t_log.clear()
        self.log('开始导出...')
        try:
            import bank_transfer
            out, stats = bank_transfer.export_bank(
                path, include_attachments=self.cb_att.isChecked(),
                progress=self.log)
            self.log('')
            self.log('═' * 46)
            self.log('导出成功！')
            self.log('文件：%s' % out)
            self.log('大小：%s' % _fmt_size(stats['size']))
            self.log('内容：%d 题 / %d 知识点 / %d 卷 / %d 配图 / %d 主题'
                     % (stats['questions'], stats['kp'], stats['papers'],
                        stats['attachments'], stats['themes']))
            self.log('')
            self.log('把这个文件拷到另一台电脑，用「导入题库」即可。')
            box = QMessageBox(self)
            box.setWindowTitle('导出成功')
            box.setIcon(QMessageBox.Information)
            box.setText('题库已导出')
            box.setInformativeText(
                '%s\n\n%d 题 / %d 知识点 / %d 卷\n大小 %s'
                % (os.path.basename(out), stats['questions'], stats['kp'],
                   stats['papers'], _fmt_size(stats['size'])))
            b_open = box.addButton('打开所在文件夹', QMessageBox.ActionRole)
            box.addButton('好', QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is b_open:
                try:
                    os.startfile(os.path.dirname(out))
                except Exception:
                    pass
        except Exception as e:
            self.log('')
            self.log('!! 导出失败：%s' % e)
            self.log(traceback.format_exc())
            QMessageBox.critical(self, '导出失败', '%s\n\n%s' % (e, traceback.format_exc()[:800]))

    # ---------------------------------------------------------------- 导入页
    def _build_import_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        pick = QHBoxLayout()
        self.e_pack = QLabel('（未选择文件）')
        self.e_pack.setStyleSheet('color:#666;')
        b_pick = QPushButton('选择 .pbk 文件...')
        b_pick.clicked.connect(self.pick_pack)
        pick.addWidget(QLabel('题库包：'))
        pick.addWidget(self.e_pack, 1)
        pick.addWidget(b_pick)
        lay.addLayout(pick)

        self.l_pack = QLabel('—')
        self.l_pack.setWordWrap(True)
        self.l_pack.setStyleSheet(
            'background:#f5f7fb;border:1px solid #dde3ee;border-radius:6px;'
            'padding:10px;color:#333;')
        lay.addWidget(self.l_pack)

        mode = QGroupBox('导入方式')
        m = QVBoxLayout(mode)
        self.rb_merge = QRadioButton('合并进来（推荐）—— 保留本机现有题目，新题追加')
        self.rb_merge.setChecked(True)
        self.rb_repl = QRadioButton('清空后导入 —— 删除本机全部题目/试卷，只留包里内容')
        self.bg = QButtonGroup(self)
        self.bg.addButton(self.rb_merge)
        self.bg.addButton(self.rb_repl)
        m.addWidget(self.rb_merge)
        m.addWidget(self.rb_repl)
        warn = QLabel('提示：知识点按名称自动匹配，同名复用；公式库和主题按内容去重。')
        warn.setStyleSheet('color:#888;padding-left:18px;')
        m.addWidget(warn)
        lay.addWidget(mode)

        lay.addStretch(1)
        self.b_import = QPushButton('开始导入')
        self.b_import.setMinimumHeight(38)
        self.b_import.setEnabled(False)
        self.b_import.clicked.connect(self.do_import)
        lay.addWidget(self.b_import)
        return w

    def pick_pack(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择题库包', db.export_dir(),
            '题库包 (*.pbk *.zip);;所有文件 (*)')
        if not path:
            return
        self.pack_path = path
        self.e_pack.setText(os.path.basename(path))
        try:
            import bank_transfer
            mf = bank_transfer.peek_pack(path)
            c = mf.get('counts') or {}
            txt = (
                '<b>文件：</b>%s<br>'
                '<b>大小：</b>%s<br>'
                '<b>格式版本：</b>%s<br>'
                '<b>导出时间：</b>%s<br>'
                '<b>包含内容：</b>%d 题 / %d 知识点 / %d 卷 / %d 配图 / %d 主题'
                % (os.path.basename(path), _fmt_size(os.path.getsize(path)),
                   mf.get('pack_version', '?'), mf.get('created_at') or '未知',
                   c.get('questions', 0), c.get('knowledge_points', 0),
                   c.get('papers', 0), c.get('attachments', 0), c.get('themes', 0)))
            self.l_pack.setText(txt)
            self.b_import.setEnabled(True)
        except Exception as e:
            self.l_pack.setText('<span style="color:#b00;">无法读取该文件：%s</span>' % e)
            self.b_import.setEnabled(False)

    def do_import(self):
        path = getattr(self, 'pack_path', None)
        if not path:
            return
        mode = 'replace' if self.rb_repl.isChecked() else 'merge'

        if mode == 'replace':
            r = QMessageBox.warning(
                self, '确认清空',
                '这会删除本机现有的全部题目、试卷和知识点！\n\n'
                '确定要「清空后导入」吗？\n（建议先导出一份备份）',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return

        self._log_lines = []
        self.t_log.clear()
        self.log('开始导入（模式：%s）...' % ('清空后导入' if mode == 'replace' else '合并'))
        try:
            import bank_transfer
            stats = bank_transfer.import_bank(
                path, mode=mode,
                attach_root=db.ATTACH_DIR,
                progress=self.log)
            self.log('')
            self.log('═' * 46)
            self.log('导入完成！')
            self.log('新增题目：%d' % stats['questions'])
            self.log('新增知识点：%d' % stats['kp_new'])
            self.log('新增试卷：%d' % stats['papers'])
            self.log('导入配图：%d' % stats['attachments'])
            if stats.get('formulas'):
                self.log('新增公式：%d' % stats['formulas'])
            if stats.get('themes'):
                self.log('导入主题：%d' % stats['themes'])
            self._refresh_local_stats()
            box = QMessageBox(self)
            box.setWindowTitle('导入完成')
            box.setIcon(QMessageBox.Information)
            box.setText('题库导入完成')
            box.setInformativeText(
                '新增 %d 题 / %d 知识点 / %d 卷\n配图 %d 张'
                % (stats['questions'], stats['kp_new'], stats['papers'],
                   stats['attachments']))
            box.exec()
        except Exception as e:
            self.log('')
            self.log('!! 导入失败：%s' % e)
            self.log(traceback.format_exc())
            QMessageBox.critical(self, '导入失败', '%s\n\n%s' % (e, traceback.format_exc()[:800]))

    # ---------------------------------------------------------------- 状态
    def _refresh_local_stats(self):
        try:
            nq = db.count_questions()
            nkp = len(db.list_kp())
            npa = len(db.list_papers())
            self.l_local.setText('%d 题 ／ %d 知识点 ／ %d 卷' % (nq, nkp, npa))
        except Exception as e:
            self.l_local.setText('读取失败：%s' % e)
