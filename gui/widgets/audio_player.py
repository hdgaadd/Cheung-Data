"""
音频播放组件 - 用系统默认播放器打开 WAV 文件。
"""

import os
from pathlib import Path


class AudioPlayer:
    """用系统默认播放器播放音频。"""

    def play(self, wav_path: str | Path):
        """用系统默认播放器打开 WAV 文件。"""
        path = Path(wav_path).resolve()
        if path.exists():
            os.startfile(str(path))

    def stop(self):
        """无需操作（系统播放器独立进程）。"""
        pass
