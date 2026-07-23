"""
页面 3：✏️ 手动修正

对 segments.json 中 speaker 为 null 或 cluster 为"过短"的片段，手动指定角色。
"""

import json
import math
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QHeaderView, QAbstractItemView, QTableWidgetItem,
)
from PySide6.QtCore import Qt
from qfluentwidgets import (
    PrimaryPushButton, PushButton, ComboBox, TableWidget, CheckBox, ToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from gui.widgets.audio_player import AudioPlayer
from process import format_time, rename_clips


PAGE_SIZE = 10


class ManualFixPage(QWidget):
    """手动修正页面。"""

    def __init__(self, main_window):
        super().__init__()
        self._main = main_window
        self._player = AudioPlayer()
        self._segments = []
        self._untagged = []
        self._checkboxes = []
        self._page = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # 顶部操作栏：全选 + 角色选择 + 执行
        action_layout = QHBoxLayout()

        self._select_all_cb = CheckBox("全选")
        self._select_all_cb.setChecked(True)
        self._select_all_cb.stateChanged.connect(self._toggle_all)
        action_layout.addWidget(self._select_all_cb)

        action_layout.addSpacing(24)
        action_layout.addWidget(QLabel("指定角色:"))
        self._speaker_combo = ComboBox()
        self._speaker_combo.setFixedWidth(160)
        action_layout.addWidget(self._speaker_combo)
        action_layout.addSpacing(16)

        self._exec_btn = PrimaryPushButton("▶ 执行标注")
        self._exec_btn.setFixedWidth(120)
        self._exec_btn.clicked.connect(self._apply)
        action_layout.addWidget(self._exec_btn)
        action_layout.addStretch()
        layout.addLayout(action_layout)

        # 可选角色提示
        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color: #555; font-size: 12px;")
        layout.addWidget(self._info_label)

        # 标题
        layout.addWidget(QLabel("未标注/过短的片段:"))

        # 表格
        self._table = TableWidget(self)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["☑", "#", "文件名", "状态", "文本", "▶"])
        self._table.setColumnWidth(0, 40)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(2, 240)
        self._table.setColumnWidth(3, 70)
        self._table.setColumnWidth(5, 40)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self._table)

        # 分页栏
        pager_layout = QHBoxLayout()
        pager_layout.addStretch()
        self._btn_prev = PushButton("◀")
        self._btn_prev.setFixedWidth(40)
        self._btn_prev.clicked.connect(self._prev_page)
        pager_layout.addWidget(self._btn_prev)

        self._page_label = PushButton("1 / 1")
        self._page_label.setEnabled(False)
        self._page_label.setFixedWidth(80)
        pager_layout.addWidget(self._page_label)

        self._btn_next = PushButton("▶")
        self._btn_next.setFixedWidth(40)
        self._btn_next.clicked.connect(self._next_page)
        pager_layout.addWidget(self._btn_next)
        pager_layout.addStretch()
        layout.addLayout(pager_layout)

    def refresh(self):
        """刷新页面。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            self._segments = []
            self._untagged = []
            self._table.setRowCount(0)
            return

        segments_path = Path("output") / ns / stem / "segments.json"
        if not segments_path.exists():
            self._segments = []
            self._untagged = []
            self._table.setRowCount(0)
            return

        with open(segments_path, "r", encoding="utf-8") as f:
            self._segments = json.load(f)

        self._untagged = []
        for seg in self._segments:
            cluster = seg.get("cluster")
            speaker = seg.get("speaker")
            if speaker:
                continue
            if cluster == "过短" or (cluster and not speaker):
                self._untagged.append(seg)

        self._page = 0
        self._fill_table()

        # 加载角色列表
        profiles_dir = Path("speaker_profiles") / ns
        speakers = []
        if profiles_dir.exists():
            speakers = sorted([f.stem for f in profiles_dir.glob("*.npy")])

        self._speaker_combo.clear()
        self._speaker_combo.addItems(speakers)
        self._info_label.setText(f"可选角色（来自 .npy）: {'、'.join(speakers)}" if speakers else "无可用角色声纹")

    def _fill_table(self):
        """填充当前页表格。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        clips_dir = Path("output") / ns / stem / "clips" if ns and stem else Path(".")

        total_pages = max(1, math.ceil(len(self._untagged) / PAGE_SIZE))
        start = self._page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_data = self._untagged[start:end]

        self._table.setRowCount(len(page_data))
        self._checkboxes = []

        for row, seg in enumerate(page_data):
            cb = CheckBox()
            cb.setChecked(self._select_all_cb.isChecked())
            self._checkboxes.append(cb)
            self._table.setCellWidget(row, 0, cb)

            self._table.setItem(row, 1, QTableWidgetItem(f"{seg.get('index', 0):03d}"))
            self._table.setItem(row, 2, QTableWidgetItem(seg.get("file", "")))

            cluster = seg.get("cluster", "")
            status = "过短" if cluster == "过短" else "未标注"
            self._table.setItem(row, 3, QTableWidgetItem(status))

            text = seg.get("text", "")
            item = QTableWidgetItem(text[:20] + "..." if len(text) > 20 else text)
            item.setToolTip(text)
            self._table.setItem(row, 4, item)

            play_btn = ToolButton(FIF.PLAY)
            play_btn.setFixedSize(30, 30)
            file_path = seg.get("file", "")
            if file_path:
                full_path = str(clips_dir / file_path)
                play_btn.clicked.connect(lambda checked, p=full_path: self._player.play(p))
            else:
                play_btn.setEnabled(False)
            self._table.setCellWidget(row, 5, play_btn)

        self._page_label.setText(f"{self._page + 1} / {total_pages}")
        self._btn_prev.setEnabled(self._page > 0)
        self._btn_next.setEnabled(self._page < total_pages - 1)

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._fill_table()

    def _next_page(self):
        total_pages = max(1, math.ceil(len(self._untagged) / PAGE_SIZE))
        if self._page < total_pages - 1:
            self._page += 1
            self._fill_table()

    def _toggle_all(self, state):
        """全选/取消全选当前页。"""
        checked = state == 2
        for cb in self._checkboxes:
            cb.setChecked(checked)

    def _apply(self):
        """执行手动标注。"""
        speaker = self._speaker_combo.currentText()
        if not speaker:
            from qfluentwidgets import MessageBox
            MessageBox("提示", "请选择一个角色", self._main).exec()
            return

        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            return

        segments_path = Path("output") / ns / stem / "segments.json"
        clips_dir = Path("output") / ns / stem / "clips"

        start = self._page * PAGE_SIZE
        page_data = self._untagged[start:start + PAGE_SIZE]

        selected_indices = []
        for i, cb in enumerate(self._checkboxes):
            if cb.isChecked():
                seg = page_data[i]
                selected_indices.append(seg.get("index"))

        if not selected_indices:
            return

        for seg in self._segments:
            if seg.get("index") in selected_indices:
                seg["speaker"] = speaker
                seg["score"] = None

        rename_clips(self._segments, clips_dir)

        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(self._segments, f, ensure_ascii=False, indent=2)

        self._main.log_panel.append(f"✓ 已标注 {len(selected_indices)} 个片段为「{speaker}」")
        self.refresh()
