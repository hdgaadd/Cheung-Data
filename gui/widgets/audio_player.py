"""
音频播放组件 - 用 QMediaPlayer 播放 WAV 片段。
"""

from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput


class AudioPlayer:
    """简单的音频播放器，全局单例使用。"""

    def __init__(self):
        self._player = QMediaPlayer()
        self._audio_output = QAudioOutput()
        self._player.setAudioOutput(self._audio_output)

    def play(self, wav_path: str | Path):
        """播放指定 WAV 文件，自动停止上一个。"""
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(Path(wav_path).resolve())))
        self._player.play()

    def stop(self):
        self._player.stop()
