"""
底部日志面板 - 只读 QTextEdit，实时追加带时间戳的日志。
"""

from datetime import datetime
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit
from PySide6.QtCore import Qt
from qfluentwidgets import PushButton


class LogPanel(QWidget):
    """底部日志面板。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 8)
        layout.setSpacing(4)

        # 日志文本区（圆角）
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFixedHeight(120)
        self._text_edit.setStyleSheet("""
            QTextEdit {
                font-family: Consolas, 'Microsoft YaHei';
                font-size: 12px;
                color: #111111;
                background: #ffffff;
                border: 1px solid #e0e0e0;
                border-radius: 10px;
                padding: 8px;
            }
        """)
        layout.addWidget(self._text_edit)

        # 清空按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self._clear_btn = PushButton("清空")
        self._clear_btn.clicked.connect(self._text_edit.clear)
        btn_layout.addWidget(self._clear_btn)
        layout.addLayout(btn_layout)

    def append(self, text: str):
        """追加一行日志（带时间戳）。"""
        ts = datetime.now().strftime("%H:%M:%S")
        self._text_edit.append(f"[{ts}] {text}")
        # 自动滚到底部
        sb = self._text_edit.verticalScrollBar()
        sb.setValue(sb.maximum())
