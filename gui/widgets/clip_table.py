"""
切片列表表格 - 支持分页、播放按钮、tooltip、列宽记忆。
"""

import math
from pathlib import Path
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QHeaderView, QAbstractItemView
from PySide6.QtCore import Qt, QSettings
from qfluentwidgets import TableWidget, PushButton, ToolButton
from qfluentwidgets import FluentIcon as FIF

from gui.widgets.audio_player import AudioPlayer


class ClipTable(QWidget):
    """带分页的切片列表表格。"""

    PAGE_SIZE = 10

    def __init__(self, parent=None, settings_key: str = "clip_table"):
        super().__init__(parent)
        self._data = []
        self._columns = []
        self._page = 0
        self._player = AudioPlayer()
        self._settings = QSettings("CheungData", "GUI")
        self._settings_key = settings_key

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 表格
        self._table = TableWidget(self)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().sectionResized.connect(self._save_column_widths)
        layout.addWidget(self._table)

        # 分页栏
        self._pager = QHBoxLayout()
        self._pager.addStretch()
        self._btn_prev = PushButton("◀")
        self._btn_prev.setFixedWidth(40)
        self._btn_prev.clicked.connect(self._prev_page)
        self._pager.addWidget(self._btn_prev)

        self._page_label = PushButton("1")
        self._page_label.setEnabled(False)
        self._page_label.setFixedWidth(80)
        self._pager.addWidget(self._page_label)

        self._btn_next = PushButton("▶")
        self._btn_next.setFixedWidth(40)
        self._btn_next.clicked.connect(self._next_page)
        self._pager.addWidget(self._btn_next)
        self._pager.addStretch()

        layout.addLayout(self._pager)

    def setup_columns(self, columns: list):
        """设置列配置: [(header, key, width), ...]"""
        self._columns = columns
        self._table.setColumnCount(len(columns) + 1)
        headers = [c[0] for c in columns] + ["▶"]
        self._table.setHorizontalHeaderLabels(headers)
        header = self._table.horizontalHeader()

        # 恢复保存的列宽
        saved = self._settings.value(f"{self._settings_key}/col_widths")

        for i, (_, _, width) in enumerate(columns):
            if saved and i < len(saved):
                try:
                    self._table.setColumnWidth(i, int(saved[i]))
                    continue
                except (ValueError, TypeError):
                    pass
            if width > 0:
                self._table.setColumnWidth(i, width)
            else:
                header.setSectionResizeMode(i, QHeaderView.Stretch)

        self._table.setColumnWidth(len(columns), 40)

    def _save_column_widths(self):
        """保存列宽到 QSettings。"""
        if not self._columns:
            return
        widths = [self._table.columnWidth(i) for i in range(len(self._columns))]
        self._settings.setValue(f"{self._settings_key}/col_widths", widths)

    def set_data(self, data: list, clips_dir: str = ""):
        """设置数据并显示第一页。"""
        self._data = data
        self._clips_dir = clips_dir
        self._page = 0
        self._refresh()

    def _refresh(self):
        """刷新当前页显示。"""
        total_pages = max(1, math.ceil(len(self._data) / self.PAGE_SIZE))
        start = self._page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE
        page_data = self._data[start:end]

        self._table.setRowCount(len(page_data))
        from PySide6.QtWidgets import QTableWidgetItem

        for row, item in enumerate(page_data):
            for col, (_, key, _) in enumerate(self._columns):
                val = str(item.get(key, ""))
                cell = QTableWidgetItem(val)
                if key == "text":
                    cell.setToolTip(val)
                    if len(val) > 20:
                        cell.setText(val[:20] + "...")
                self._table.setItem(row, col, cell)

            play_btn = ToolButton(FIF.PLAY)
            play_btn.setFixedSize(30, 30)
            file_path = item.get("file", "")
            if file_path and self._clips_dir:
                full_path = str(Path(self._clips_dir) / file_path)
                play_btn.clicked.connect(lambda checked, p=full_path: self._player.play(p))
            else:
                play_btn.setEnabled(False)
            self._table.setCellWidget(row, len(self._columns), play_btn)

        self._page_label.setText(f"{self._page + 1} / {total_pages}")
        self._btn_prev.setEnabled(self._page > 0)
        self._btn_next.setEnabled(self._page < total_pages - 1)

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._refresh()

    def _next_page(self):
        total_pages = max(1, math.ceil(len(self._data) / self.PAGE_SIZE))
        if self._page < total_pages - 1:
            self._page += 1
            self._refresh()
