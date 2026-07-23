"""
TaskWorker - 在 QThread 中执行耗时任务，通过 signal 与 UI 通信。

支持进度上报、日志输出、完成/失败通知。
"""

import sys
import traceback
from PySide6.QtCore import QThread, Signal


class SignalStream:
    """将 write() 调用转为 Qt Signal，用于重定向 stdout。"""

    def __init__(self, signal):
        self._signal = signal

    def write(self, text):
        if text.strip():
            self._signal.emit(text.rstrip("\n"))

    def flush(self):
        pass


class TaskWorker(QThread):
    """通用任务执行线程。"""

    log = Signal(str)           # 日志文本
    progress = Signal(int, str) # (百分比, 步骤描述)
    finished_ok = Signal(object)  # 任务结果（任意对象）
    finished_err = Signal(str)  # 错误信息

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def run(self):
        # 重定向 stdout 到日志 signal
        old_stdout = sys.stdout
        sys.stdout = SignalStream(self.log)
        try:
            result = self._func(*self._args, **self._kwargs)
            self.finished_ok.emit(result)
        except Exception as e:
            tb = traceback.format_exc()
            self.log.emit(f"❌ {e}")
            self.finished_err.emit(str(e))
        finally:
            sys.stdout = old_stdout
