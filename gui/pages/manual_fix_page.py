"""
页面 3：✏️ 手动修正

对 segments.json 中 speaker 为 null 或 cluster 为"过短"的片段，手动指定角色。
"""

import json
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QHeaderView, QAbstractItemView, QTableWidgetItem,
)
from PySide6.QtCore import Qt
from qfluentwidgets import (
    PrimaryPushButton, ComboBox, TableWidget, CheckBox, ToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from gui.widgets.audio_player import AudioPlayer
from process import format_time, rename_clips


class ManualFixPage(QWidget):
    """手动修正页面。"""

    def __init__(self, main_window):
        super().__init__()
        self._main = main_window
        self._player = AudioPlayer()
        self._segments = []
        self._untagged = []  # 筛选后的待标注片段
        self._checkboxes = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

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

        # 底部操作栏
        action_layout = QHBoxLayout()
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
        self._info_label.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(self._info_label)

    def refresh(self):
        """刷新页面。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            self._segments = []
            self._untagged = []
            self._table.setRowCount(0)
            return

        # 加载 segments
        segments_path = Path("output") / ns / stem / "segments.json"
        if not segments_path.exists():
            self._segments = []
            self._untagged = []
            self._table.setRowCount(0)
            return

        with open(segments_path, "r", encoding="utf-8") as f:
            self._segments = json.load(f)

        # 筛选: speaker 为 null 且 cluster 不为 null，或 cluster 为"过短"
        self._untagged = []
        for seg in self._segments:
            cluster = seg.get("cluster")
            speaker = seg.get("speaker")
            if speaker:
                continue
            if cluster == "过短" or (cluster and not speaker):
                self._untagged.append(seg)

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
        """填充表格。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        clips_dir = Path("output") / ns / stem / "clips" if ns and stem else Path(".")

        self._table.setRowCount(len(self._untagged))
        self._checkboxes = []

        for row, seg in enumerate(self._untagged):
            # checkbox
            cb = CheckBox()
            cb.setChecked(True)
            self._checkboxes.append(cb)
            self._table.setCellWidget(row, 0, cb)

            # #
            self._table.setItem(row, 1, QTableWidgetItem(f"{seg.get('index', 0):03d}"))

            # 文件名
            self._table.setItem(row, 2, QTableWidgetItem(seg.get("file", "")))

            # 状态
            cluster = seg.get("cluster", "")
            status = "过短" if cluster == "过短" else "未标注"
            self._table.setItem(row, 3, QTableWidgetItem(status))

            # 文本
            text = seg.get("text", "")
            item = QTableWidgetItem(text[:20] + "..." if len(text) > 20 else text)
            item.setToolTip(text)
            self._table.setItem(row, 4, item)

            # 播放按钮
            play_btn = ToolButton(FIF.PLAY)
            play_btn.setFixedSize(30, 30)
            file_path = seg.get("file", "")
            if file_path:
                full_path = str(clips_dir / file_path)
                play_btn.clicked.connect(lambda checked, p=full_path: self._player.play(p))
            else:
                play_btn.setEnabled(False)
            self._table.setCellWidget(row, 5, play_btn)

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

        # 收集选中的片段
        selected_indices = []
        for i, cb in enumerate(self._checkboxes):
            if cb.isChecked():
                seg = self._untagged[i]
                selected_indices.append(seg.get("index"))

        if not selected_indices:
            return

        # 更新 segments.json
        for seg in self._segments:
            if seg.get("index") in selected_indices:
                seg["speaker"] = speaker
                seg["score"] = None

        # 重命名文件
        rename_clips(self._segments, clips_dir)

        # 保存
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(self._segments, f, ensure_ascii=False, indent=2)

        self._main.log_panel.append(f"✓ 已标注 {len(selected_indices)} 个片段为「{speaker}」")

        # 刷新
        self.refresh()
