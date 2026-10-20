"""语音转文字单元测试（不下载模型、不调用网络）。"""
from __future__ import annotations

import wave

import numpy as np

from src.asr import SpeechToText
from src.config import ASRConfig


def _write_sine_wav(path, seconds=1.0, sr=24000, freq=440.0):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    data = (np.sin(2 * np.pi * freq * t) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())


def test_resample_to_16k(tmp_path):
    p = tmp_path / "a.wav"
    _write_sine_wav(p, seconds=1.0, sr=24000)
    audio = SpeechToText._read_wav_16k(p)
    assert audio is not None
    assert len(audio) == 16000
    assert audio.dtype == np.float32
    assert -1.0 <= float(audio.max()) <= 1.0


def test_missing_silk_returns_empty(tmp_path):
    cfg = ASRConfig(enabled=True)
    stt = SpeechToText(cfg)
    assert stt.transcribe_silk(tmp_path / "nope.silk") == ""


def test_disabled_returns_empty(tmp_path):
    p = tmp_path / "x.silk"
    p.write_bytes(b"\x02#!SILK_V3")
    cfg = ASRConfig(enabled=False)
    stt = SpeechToText(cfg)
    assert stt.transcribe_silk(p) == ""
