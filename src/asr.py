"""语音转文字：微信 SILK 语音 → WAV → 本地 faster-whisper 识别。

流程
====
1. wechatauto 的 MediaDownloader 把语音从微信库里取出，落盘成 `.silk`；
2. `pilk` 把 SILK 解成 24kHz WAV（纯 Python，不需要 ffmpeg）；
3. 用 numpy 重采样到 16kHz 单声道 float32（**绕过 PyAV**，避免 av 版本兼容问题）；
4. faster-whisper 在本地 CPU 上识别（int8，首次运行自动下载模型）。

为什么用本地 whisper
====================
DeepSeek API 目前只支持文本/图片输入，没有语音接口；本地 whisper 免费、离线、
语音数据不出本机。默认 `base` 模型（约 145MB），想更准可改 `small`。
"""
from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import Any, Optional

from .config import ASRConfig

log = logging.getLogger("dsbot.asr")


class SpeechToText:
    def __init__(self, cfg: ASRConfig) -> None:
        self.cfg = cfg
        self._model: Any = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def transcribe_silk(self, silk_path: str | Path) -> str:
        """识别一条微信语音；失败返回空字符串。"""
        if not self.cfg.enabled:
            return ""
        path = Path(silk_path)
        if not path.exists():
            return ""
        wav_path = path.with_suffix(".wav")
        try:
            import pilk

            pilk.silk_to_wav(str(path), str(wav_path))  # 自带 0x02 头处理
        except Exception as exc:  # noqa: BLE001
            log.warning("SILK 解码失败 %s：%s", path.name, exc)
            return ""
        try:
            audio = self._read_wav_16k(wav_path)
            if audio is None or len(audio) == 0:
                return ""
            if len(audio) / 16000.0 > self.cfg.max_seconds:
                log.warning("语音过长（%.0fs），跳过识别", len(audio) / 16000.0)
                return ""
            model = self._get_model()
            segments, info = model.transcribe(
                audio,
                language=self.cfg.language or None,
                vad_filter=True,
                beam_size=5,
            )
            text = "".join(seg.text for seg in segments).strip()
            log.info(
                "语音识别完成（%.1fs 音频，用时 %.1fs）：%s",
                getattr(info, "duration", 0.0),
                getattr(info, "duration_after_vad", 0.0),
                text[:60] or "（空）",
            )
            return text
        except Exception as exc:  # noqa: BLE001
            log.warning("语音识别失败：%s", exc)
            return ""
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            Path(self.cfg.models_dir).mkdir(parents=True, exist_ok=True)
            log.info("加载语音模型 %s（首次会下载，请稍等）…", self.cfg.model)
            self._model = WhisperModel(
                self.cfg.model,
                device=self.cfg.device,
                compute_type=self.cfg.compute_type,
                download_root=self.cfg.models_dir,
            )
            log.info("语音模型已就绪")
            return self._model

    @staticmethod
    def _read_wav_16k(path: Path) -> Optional[Any]:
        import numpy as np

        try:
            with wave.open(str(path), "rb") as w:
                sr = w.getframerate()
                channels = w.getnchannels()
                frames = w.readframes(w.getnframes())
        except Exception:  # noqa: BLE001
            return None
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        if sr != 16000 and len(data) > 0:
            new_len = int(len(data) * 16000 / sr)
            data = np.interp(
                np.linspace(0, len(data), new_len, endpoint=False),
                np.arange(len(data)),
                data,
            ).astype(np.float32)
        return data
