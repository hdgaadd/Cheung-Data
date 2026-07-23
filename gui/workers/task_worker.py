"""
TaskWorker - 在 QThread 中执行耗时任务，通过 signal 与 UI 通信。

支持进度上报、日志输出、完成/失败通知。
"""

import sys
import traceback
from PySide6.QtCore import QThread, Signal


class SignalStream:
    """
    将 write() 调用转为 Qt Signal，同时保留原始 stream 的底层属性。

    这样 funasr 等库访问 sys.stdout.buffer / fileno() 时不会报错。
    """

    def __init__(self, signal, original_stream):
        self._signal = signal
        self._original = original_stream

    def write(self, text):
        if text and text.strip():
            self._signal.emit(text.rstrip("\n"))
        # 同时写入原始 stream（保持底层 I/O 正常）
        if self._original and hasattr(self._original, 'write'):
            try:
                self._original.write(text)
            except (ValueError, OSError):
                pass

    def flush(self):
        if self._original and hasattr(self._original, 'flush'):
            try:
                self._original.flush()
            except (ValueError, OSError):
                pass

    @property
    def buffer(self):
        if self._original and hasattr(self._original, 'buffer'):
            return self._original.buffer
        raise AttributeError("no buffer")

    def fileno(self):
        if self._original and hasattr(self._original, 'fileno'):
            return self._original.fileno()
        raise OSError("no fileno")

    def isatty(self):
        return False

    @property
    def encoding(self):
        if self._original and hasattr(self._original, 'encoding'):
            return self._original.encoding
        return 'utf-8'

    @property
    def errors(self):
        if self._original and hasattr(self._original, 'errors'):
            return self._original.errors
        return 'replace'


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
        # 重定向 stdout 和 stderr 到日志 signal
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = SignalStream(self.log, old_stdout)
        sys.stderr = SignalStream(self.log, old_stderr)
        try:
            result = self._func(*self._args, **self._kwargs)
            self.finished_ok.emit(result)
        except Exception as e:
            self.log.emit(f"❌ {e}")
            self.finished_err.emit(str(e))
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
