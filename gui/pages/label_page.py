"""
页面 2：👥 角色标注

对应命令：python process.py --namespace {ns} --apply-edit {wav_stem}
"""

import os
import json
from pathlib import Path
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtCore import Qt
from qfluentwidgets import PrimaryPushButton, PushButton, RadioButton

from gui.workers.task_worker import TaskWorker
from gui.widgets.clip_table import ClipTable


class LabelPage(QWidget):
    """角色标注页面。"""

    def __init__(self, main_window):
        super().__init__()
        self._main = main_window
        self._worker = None
        self._segments = []
        self._changes = []
        self._old_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # 按钮栏
        btn_layout = QHBoxLayout()
        self._exec_btn = PrimaryPushButton("▶ 执行")
        self._exec_btn.setFixedWidth(100)
        self._exec_btn.clicked.connect(self._run)
        btn_layout.addWidget(self._exec_btn)
        btn_layout.addStretch()

        self._open_btn = PushButton("打开文件夹")
        self._open_btn.clicked.connect(self._open_clips_dir)
        btn_layout.addWidget(self._open_btn)
        layout.addLayout(btn_layout)

        # Toggle + 切片数信息
        self._toggle_layout = QHBoxLayout()
        self._radio_changes = RadioButton("显示变化(0)")
        self._radio_all = RadioButton("显示全部(0)")
        self._radio_changes.clicked.connect(self._show_changes_view)
        self._radio_all.clicked.connect(self._show_all_view)
        self._toggle_layout.addWidget(self._radio_changes)
        self._toggle_layout.addWidget(self._radio_all)
        self._count_label = QLabel("")
        self._toggle_layout.addWidget(self._count_label)
        self._toggle_layout.addStretch()
        layout.addLayout(self._toggle_layout)

        # 表格
        self._table = ClipTable(self, settings_key="label_table")
        layout.addWidget(self._table)

    def refresh(self):
        """刷新页面数据。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            self._segments = []
            return

        segments_path = Path("output") / ns / stem / "segments.json"
        if segments_path.exists():
            with open(segments_path, "r", encoding="utf-8") as f:
                self._segments = json.load(f)
            self._show_all_view()
            self._radio_all.setChecked(True)
            self._radio_all.setText(f"显示全部({len(self._segments)})")
            self._radio_changes.setText(f"显示变化({len(self._changes)})")
        else:
            self._segments = []
            self._table.set_data([])

    def _run(self):
        """执行标注。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            return

        self._exec_btn.setEnabled(False)
        self._main.show_progress(10, "解析 edit.txt...")

        from process import apply_edit, load_config, compare_with_previous_data

        # 记录旧 segments
        segments_path = Path("output") / ns / stem / "segments.json"
        old_segments = None
        if segments_path.exists():
            with open(segments_path, "r", encoding="utf-8") as f:
                old_segments = json.load(f)
        self._old_count = len(old_segments) if old_segments else 0

        def task():
            config = load_config()
            apply_edit(ns, stem, config, no_label=False)
            # 读取新 segments 做对比
            with open(segments_path, "r", encoding="utf-8") as f:
                new_segments = json.load(f)
            changes = []
            if old_segments:
                changes = compare_with_previous_data(old_segments, new_segments)
            return {"segments": new_segments, "changes": changes or []}

        self._worker = TaskWorker(task)
        self._worker.log.connect(self._on_log)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    def _on_log(self, text):
        """日志回调，同时根据关键字更新进度。"""
        self._main.log_panel.append(text)
        if "解析完成" in text:
            self._main.show_progress(15, "解析完成，切分中...")
        elif "切分完成" in text:
            self._main.show_progress(40, "切分完成，提取embedding...")
        elif "提取声纹" in text:
            self._main.show_progress(50, "提取声纹 embedding...")
        elif "层次聚类" in text:
            self._main.show_progress(70, "聚类中...")
        elif "聚类完成" in text:
            self._main.show_progress(80, "标注中...")
        elif "标注完成" in text:
            self._main.show_progress(90, "保存中...")
        elif "已保存" in text:
            self._main.show_progress(95, "对比结果...")

    def _on_done(self, result):
        self._exec_btn.setEnabled(True)
        self._main.hide_progress()
        if result:
            self._segments = result["segments"]
            self._changes = result["changes"]
            new_count = len(self._segments)

            self._radio_all.setText(f"显示全部({new_count})")
            self._radio_changes.setText(f"显示变化({len(self._changes)})")

            if self._old_count > 0 and self._changes:
                self._count_label.setText(f"切片数变化: {self._old_count} → {new_count}")
                self._radio_changes.setChecked(True)
                self._show_changes_view()
            else:
                self._count_label.setText("")
                self._radio_all.setChecked(True)
                self._show_all_view()

    def _on_error(self, msg):
        self._exec_btn.setEnabled(True)
        self._main.hide_progress()
        from qfluentwidgets import MessageBox
        MessageBox("执行失败", msg, self._main).exec()

    def _show_changes_view(self):
        """显示变化项视图。"""
        columns = [
            ("#", "index", 50),
            ("变化", "type", 80),
            ("文件名", "file", 200),
            ("详情", "detail", 0),
        ]
        self._table.setup_columns(columns)

        data = []
        for change_type, index, text, old_range, new_range in self._changes:
            detail = f"{old_range} → {new_range}" if old_range else new_range
            # 找对应 segment 的 file
            seg_file = ""
            for seg in self._segments:
                if seg.get("index") == index:
                    seg_file = seg.get("file", "")
                    break
            data.append({
                "index": f"{index:03d}",
                "type": change_type,
                "file": seg_file,
                "detail": detail,
                "text": text,
            })

        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        clips_dir = str(Path("output") / ns / stem / "clips") if ns and stem else ""
        self._table.set_data(data, clips_dir)

    def _show_all_view(self):
        """显示全部切片视图。"""
        columns = [
            ("#", "index", 50),
            ("文件名", "file", 220),
            ("角色", "speaker", 80),
            ("文本", "text", 0),
        ]
        self._table.setup_columns(columns)

        data = []
        for seg in self._segments:
            data.append({
                "index": f"{seg.get('index', 0):03d}",
                "file": seg.get("file", ""),
                "speaker": seg.get("speaker") or seg.get("cluster") or "",
                "text": seg.get("text", ""),
            })

        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        clips_dir = str(Path("output") / ns / stem / "clips") if ns and stem else ""
        self._table.set_data(data, clips_dir)

    def _open_clips_dir(self):
        """打开 clips 文件夹。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            return
        clips_dir = Path("output") / ns / stem / "clips"
        if clips_dir.exists():
            os.startfile(str(clips_dir))
