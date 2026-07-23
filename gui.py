"""
Cheung-Data GUI 入口

启动方式: python gui.py
"""

import sys
from PySide6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme

from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    setTheme(Theme.LIGHT)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
