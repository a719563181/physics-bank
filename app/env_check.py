"""PySide6 环境验证窗口 —— 阶段 0 验收
显示环境信息，确认 GUI 框架可用
"""
import sys
import platform
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QLabel, QTextEdit, QPushButton, QHBoxLayout
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


def collect_env():
    lines = []
    lines.append(f"Python      : {sys.version.split()[0]}  ({platform.architecture()[0]})")
    lines.append(f"Platform    : {platform.system()} {platform.release()}")
    try:
        import PySide6
        lines.append(f"PySide6     : {PySide6.__version__}")
    except Exception as e:
        lines.append(f"PySide6     : ERROR {e}")
    try:
        import pymupdf
        lines.append(f"PyMuPDF     : {pymupdf.__version__}")
    except Exception as e:
        lines.append(f"PyMuPDF     : ERROR {e}")
    try:
        import docx
        lines.append("python-docx : OK")
    except Exception as e:
        lines.append(f"python-docx : ERROR {e}")
    try:
        import pptx
        lines.append("python-pptx : OK")
    except Exception as e:
        lines.append(f"python-pptx : ERROR {e}")
    try:
        import numpy
        lines.append(f"numpy       : {numpy.__version__}")
    except Exception as e:
        lines.append(f"numpy       : ERROR {e}")
    # torch / CUDA
    try:
        import torch
        lines.append(f"torch       : {torch.__version__}")
        if torch.cuda.is_available():
            lines.append(f"CUDA        : AVAILABLE - {torch.cuda.get_device_name(0)}")
            lines.append(f"VRAM        : {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
        else:
            lines.append("CUDA        : NOT AVAILABLE (CPU only)")
    except Exception as e:
        lines.append(f"torch       : NOT INSTALLED")
    return lines


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("物理题库 — 环境验证 (阶段 0)")
        self.resize(760, 520)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel("高中物理题库管理软件 — 环境验证")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        self.info = QTextEdit()
        self.info.setReadOnly(True)
        self.info.setFont(QFont("Consolas", 10))
        self.info.setPlainText("\n".join(collect_env()))
        layout.addWidget(self.info)

        btn_row = QHBoxLayout()
        btn = QPushButton("重新检测")
        btn.clicked.connect(lambda: self.info.setPlainText("\n".join(collect_env())))
        btn2 = QPushButton("关闭")
        btn2.clicked.connect(self.close)
        btn_row.addWidget(btn)
        btn_row.addWidget(btn2)
        layout.addLayout(btn_row)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    print("GUI WINDOW SHOWN OK")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
