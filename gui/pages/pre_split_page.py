"""
页面 1：📁 素材预处理

对应命令：python process.py --namespace {ns} --gen-edit {wav_stem}.wav
"""

import os
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

        self._open_btn = PushButton("打开文件夹")
        self._open_btn.clicked.connect(self._open_output_dir)
        btn_layout.addWidget(self._open_btn)

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

    def _open_output_dir(self):
        """打开当前源文件对应的 output 文件夹。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            return
        output_dir = Path("output") / ns / stem
        output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(output_dir))

    def _run(self):
        """执行预切分。"""
        ns = self._main.current_namespace()
        stem = self._main.current_wav_stem()
        if not ns or not stem:
            return

        self._exec_btn.setEnabled(False)
        self._main.show_progress(5, "准备中...")

        from process import gen_edit, load_config

        def task():
            config = load_config()
            self._main.progress_signal.emit(10, "音频转换...")
            # gen_edit 内部会打印各步骤，通过 stdout 重定向到日志
            # 这里无法插入中间进度，但可以监听日志关键字在外部更新
            gen_edit(ns, f"{stem}.wav", config)
            self._main.progress_signal.emit(100, "完成")

        self._worker = TaskWorker(task)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._main.show_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    def _on_log(self, text):
        """日志回调，同时根据关键字更新进度。"""
        self._main.log_panel.append(text)
        if "转为 16kHz" in text:
            self._main.show_progress(10, "音频转换...")
        elif "ASR 模型加载完成" in text:
            self._main.show_progress(30, "ASR 识别中...")
        elif "语音识别中" in text:
            self._main.show_progress(40, "ASR 识别中...")
        elif "识别完成" in text:
            self._main.show_progress(70, "生成 edit.txt...")
        elif "edit.txt 已生成" in text:
            self._main.show_progress(80, "生成 reference...")
        elif "reference" in text and "已生成" in text:
            self._main.show_progress(95, "打开文件...")

    def _on_done(self, result):
        self._exec_btn.setEnabled(True)
        self._main.hide_progress()
        self.refresh()

    def _on_error(self, msg):
        self._exec_btn.setEnabled(True)
        self._main.hide_progress()
        from qfluentwidgets import MessageBox
        MessageBox("执行失败", msg, self._main).exec()
