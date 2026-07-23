"""
页面 4：📦 导出与注册
"""

import json
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtCore import Qt
from qfluentwidgets import PrimaryPushButton, ComboBox, SubtitleLabel

from gui.workers.task_worker import TaskWorker


class ExportPage(QWidget):
    """导出与注册页面。"""

    def __init__(self, main_window):
        super().__init__()
        self._main = main_window
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # ── 导出 ──
        layout.addWidget(SubtitleLabel("导出"))
        export_layout = QHBoxLayout()
        self._export_btn = PrimaryPushButton("▶ 导出当前命名空间")
        self._export_btn.setFixedWidth(200)
        self._export_btn.clicked.connect(self._run_export)
        export_layout.addWidget(self._export_btn)
        export_layout.addStretch()
        layout.addLayout(export_layout)

        layout.addSpacing(24)

        # ── 声纹注册 ──
        layout.addWidget(SubtitleLabel("声纹注册"))
        enroll_layout = QHBoxLayout()
        enroll_layout.addWidget(QLabel("角色:"))
        self._speaker_combo = ComboBox()
        self._speaker_combo.setFixedWidth(160)
        enroll_layout.addWidget(self._speaker_combo)
        enroll_layout.addSpacing(16)

        self._enroll_btn = PrimaryPushButton("▶ 注册")
        self._enroll_btn.setFixedWidth(100)
        self._enroll_btn.clicked.connect(self._run_enroll)
        enroll_layout.addWidget(self._enroll_btn)
        enroll_layout.addStretch()
        layout.addLayout(enroll_layout)

        layout.addSpacing(12)

        # 当前声纹库状态
        layout.addWidget(QLabel("当前声纹库状态:"))
        self._profiles_label = QLabel("")
        self._profiles_label.setStyleSheet("font-size: 12px; color: #555;")
        self._profiles_label.setWordWrap(True)
        layout.addWidget(self._profiles_label)

        layout.addStretch()

    def refresh(self):
        """刷新页面。"""
        ns = self._main.current_namespace()
        if not ns:
            return

        # 角色下拉框: 来自 speaker_sources/{ns}/ 子文件夹
        sources_dir = Path("speaker_sources") / ns
        speakers = []
        if sources_dir.exists():
            speakers = sorted([d.name for d in sources_dir.iterdir() if d.is_dir()])

        self._speaker_combo.clear()
        self._speaker_combo.addItems(speakers)

        # 声纹库状态
        profiles_json = Path("speaker_profiles") / ns / "profiles.json"
        if profiles_json.exists():
            with open(profiles_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
            lines = []
            for name, info in meta.items():
                count = info.get("sample_count", "?")
                updated = info.get("last_updated", "?")
                lines.append(f"  {name}.npy     ({count}个素材, 更新于 {updated})")
            self._profiles_label.setText("\n".join(lines))
        else:
            self._profiles_label.setText("  （无声纹库）")

    def _run_export(self):
        """导出。"""
        ns = self._main.current_namespace()
        if not ns:
            return

        self._export_btn.setEnabled(False)
        self._main.show_progress(50, "导出中...")

        from export import export_namespace, load_config

        def task():
            config = load_config()
            export_namespace(ns, config)

        self._worker = TaskWorker(task)
        self._worker.log.connect(self._main.log_panel.append)
        self._worker.finished_ok.connect(self._on_export_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    def _on_export_done(self, result):
        self._export_btn.setEnabled(True)
        self._main.hide_progress()

    def _run_enroll(self):
        """声纹注册。"""
        ns = self._main.current_namespace()
        speaker = self._speaker_combo.currentText()
        if not ns or not speaker:
            return

        self._enroll_btn.setEnabled(False)
        self._main.show_progress(50, f"注册「{speaker}」...")

        from enroll import enroll_namespace, load_embedding_model

        def task():
            model = load_embedding_model()
            enroll_namespace(model, ns, speaker)

        self._worker = TaskWorker(task)
        self._worker.log.connect(self._main.log_panel.append)
        self._worker.finished_ok.connect(self._on_enroll_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    def _on_enroll_done(self, result):
        self._enroll_btn.setEnabled(True)
        self._main.hide_progress()
        self.refresh()

    def _on_error(self, msg):
        self._export_btn.setEnabled(True)
        self._enroll_btn.setEnabled(True)
        self._main.hide_progress()
        from qfluentwidgets import MessageBox
        MessageBox("执行失败", msg, self._main).exec()
