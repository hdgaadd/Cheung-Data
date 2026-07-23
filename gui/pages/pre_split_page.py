"""
页面 1：📁 素材预处理

对应命令：python process.py --namespace {ns} --gen-edit {wav_stem}.wav
"""

from pathlib import Path
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout
from PySide6.QtCore import Qt
from qfluentwidgets import PushButton, PrimaryPushButton

from gui.workers.task_worker import TaskWorker


class PreSplitPage(QWidget):
    """素材预处理页面。"""

    def __init__(self, main_window):
        super().__init__()
        self._main = main_window
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # 执行按钮
        btn_layout = QHBoxLayout()
        self._exec_btn = PrimaryPushButton("▶ 执行预切分")
        self._exec_btn.setFixedWidth(160)
        self._exec_btn.clicked.connect(self._run)
        btn_layout.addWidget(self._exec_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 状态标签
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        layout.addStretch()

    def refresh(self):
        """页面切换或选择器变更时刷新状态。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            self._status_label.setText("")
            return

        episode_dir = Path("output") / ns / stem
        edit_path = episode_dir / "edit.txt"
        ref_path = episode_dir / "reference.mp4"

        lines = []
        if edit_path.exists():
            line_count = sum(1 for l in edit_path.read_text(encoding="utf-8").splitlines() if l.strip())
            lines.append(f"✓ edit.txt 已生成 ({line_count}行)")
        else:
            lines.append("○ edit.txt 未生成")

        if ref_path.exists():
            lines.append("✓ reference.mp4 已生成")

        self._status_label.setText("状态: " + "\n状态: ".join(lines))

    def _run(self):
        """执行预切分。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            return

        self._exec_btn.setEnabled(False)
        self._main.show_progress(10, "音频转换...")

        from process import gen_edit, load_config

        def task():
            config = load_config()
            self._main.progress_signal.emit(10, "音频转换...")
            gen_edit(ns, f"{stem}.wav", config)

        self._worker = TaskWorker(task)
        self._worker.log.connect(self._main.log_panel.append)
        self._worker.progress.connect(self._main.show_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    def _on_done(self, result):
        self._exec_btn.setEnabled(True)
        self._main.hide_progress()
        self.refresh()

    def _on_error(self, msg):
        self._exec_btn.setEnabled(True)
        self._main.hide_progress()
        from qfluentwidgets import MessageBox
        MessageBox("执行失败", msg, self._main).exec()
