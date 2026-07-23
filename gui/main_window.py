"""
主窗口 - 整体布局：顶部选择器 + 左侧导航 + 中间内容 + 进度条 + 底部日志。
"""

import json
from pathlib import Path
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QLabel,
)
from PySide6.QtCore import Qt, Signal, QSettings
from qfluentwidgets import (
    NavigationInterface, NavigationItemPosition,
    ComboBox, ProgressBar,
)
from qfluentwidgets import FluentIcon as FIF

from gui.widgets.log_panel import LogPanel
from gui.pages.pre_split_page import PreSplitPage
from gui.pages.label_page import LabelPage
from gui.pages.manual_fix_page import ManualFixPage
from gui.pages.export_page import ExportPage


class MainWindow(QMainWindow):
    """Cheung-Data GUI 主窗口。"""

    progress_signal = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cheung-Data")
        self.resize(1100, 750)

        # 圆角窗口 + 深色文字
        self.setStyleSheet("""
            QMainWindow {
                background: #fafafa;
                border-radius: 10px;
            }
            QLabel {
                color: #111111;
                font-size: 13px;
                font-weight: 500;
            }
            QTableWidget {
                color: #111111;
                font-size: 13px;
                font-weight: 400;
            }
            QTextEdit {
                color: #111111;
                font-weight: 400;
                border-radius: 8px;
                border: 1px solid #e0e0e0;
                background: #ffffff;
            }
        """)

        # 恢复上次窗口大小
        self._settings = QSettings("CheungData", "GUI")
        self._restore_geometry()

        # 中央 widget
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ═══ 顶部选择器 ═══
        top_bar = QWidget()
        top_bar.setFixedHeight(48)
        top_bar.setStyleSheet("background: #f0f0f0; border-bottom: 1px solid #ddd;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 8, 16, 8)

        top_layout.addWidget(QLabel("命名空间:"))
        self._ns_combo = ComboBox()
        self._ns_combo.setFixedWidth(160)
        self._ns_combo.currentTextChanged.connect(self._on_ns_changed)
        top_layout.addWidget(self._ns_combo)

        top_layout.addSpacing(24)
        top_layout.addWidget(QLabel("源文件:"))
        self._wav_combo = ComboBox()
        self._wav_combo.setFixedWidth(200)
        self._wav_combo.currentTextChanged.connect(self._on_wav_changed)
        top_layout.addWidget(self._wav_combo)
        top_layout.addStretch()

        root_layout.addWidget(top_bar)

        # ═══ 中间区域：导航 + 内容 ═══
        middle = QWidget()
        middle_layout = QHBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)

        # 左侧导航
        self._nav = NavigationInterface(self, showMenuButton=False, showReturnButton=False)
        self._nav.setFixedWidth(180)
        middle_layout.addWidget(self._nav)

        # 内容区
        self._stack = QStackedWidget()
        middle_layout.addWidget(self._stack)

        root_layout.addWidget(middle, 1)

        # ═══ 进度条 ═══
        progress_widget = QWidget()
        progress_widget.setFixedHeight(36)
        progress_layout = QHBoxLayout(progress_widget)
        progress_layout.setContentsMargins(16, 4, 16, 4)

        self._progress_bar = ProgressBar()
        self._progress_bar.setFixedHeight(8)
        self._progress_bar.setCustomBarColor("#4a9eff", "#4a9eff")
        progress_layout.addWidget(self._progress_bar, 1)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("font-size: 12px; color: #333; padding-left: 12px;")
        self._progress_label.setFixedWidth(200)
        progress_layout.addWidget(self._progress_label)

        self._progress_widget = progress_widget
        self._progress_widget.setVisible(False)
        root_layout.addWidget(self._progress_widget)

        # ═══ 底部日志 ═══
        self.log_panel = LogPanel()
        root_layout.addWidget(self.log_panel)

        # ═══ 初始化页面 ═══
        self._init_pages()
        self._load_namespaces()

        # 连接 progress signal
        self.progress_signal.connect(self.show_progress)

    def _init_pages(self):
        """初始化 4 个页面并注册到导航。"""
        self._page_pre_split = PreSplitPage(self)
        self._page_label = LabelPage(self)
        self._page_manual_fix = ManualFixPage(self)
        self._page_export = ExportPage(self)

        self._add_page(self._page_pre_split, FIF.FOLDER, "素材预处理")
        self._add_page(self._page_label, FIF.PEOPLE, "角色标注")
        self._add_page(self._page_manual_fix, FIF.EDIT, "手动修正")
        self._add_page(self._page_export, FIF.SEND, "导出与注册")

        # 默认选中第一个
        self._stack.setCurrentIndex(0)
        self._nav.setCurrentItem("素材预处理")

    def _add_page(self, page: QWidget, icon, text: str):
        """添加页面到 stack 和导航。"""
        self._stack.addWidget(page)
        self._nav.addItem(
            routeKey=text,
            icon=icon,
            text=text,
            onClick=lambda checked=None, p=page: self._stack.setCurrentWidget(p),
        )

    # ═══ 顶部选择器逻辑 ═══

    def _load_namespaces(self):
        """扫描 wavs/ 加载命名空间。"""
        wavs_dir = Path("wavs")
        if not wavs_dir.exists():
            return
        namespaces = sorted([d.name for d in wavs_dir.iterdir() if d.is_dir()])
        self._ns_combo.clear()
        self._ns_combo.addItems(namespaces)

    def _on_ns_changed(self, ns: str):
        """命名空间变更。"""
        self._load_wavs(ns)
        self._refresh_pages()

    def _load_wavs(self, ns: str):
        """加载该命名空间下的 WAV 文件。"""
        self._wav_combo.clear()
        if not ns:
            return
        wavs_dir = Path("wavs") / ns
        if not wavs_dir.exists():
            return
        wavs = sorted([f.stem for f in wavs_dir.glob("*.wav")])
        self._wav_combo.addItems(wavs)

    def _on_wav_changed(self, wav_stem: str):
        """源文件变更。"""
        self._refresh_pages()

    def _refresh_pages(self):
        """刷新所有页面。"""
        for page in [self._page_pre_split, self._page_label, self._page_manual_fix, self._page_export]:
            page.refresh()

    # ═══ 公共方法 ═══

    def current_namespace(self) -> str:
        return self._ns_combo.currentText()

    def current_wav_stem(self) -> str:
        return self._wav_combo.currentText()

    def show_progress(self, percent: int, text: str = ""):
        """显示进度。"""
        self._progress_widget.setVisible(True)
        self._progress_bar.setValue(percent)
        self._progress_label.setText(text)

    def hide_progress(self):
        """隐藏进度。"""
        self._progress_widget.setVisible(False)
        self._progress_bar.setValue(0)
        self._progress_label.setText("")

    # ═══ 窗口大小记忆 ═══

    def _restore_geometry(self):
        """恢复上次窗口大小和位置。"""
        geom = self._settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)

    def closeEvent(self, event):
        """关闭时保存窗口大小。"""
        self._settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)
