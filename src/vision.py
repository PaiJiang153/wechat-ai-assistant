"""识图 / 看表情包：把微信里的图片和表情包交给 DeepSeek 视觉模型变成文字。

为什么这么做
============
群聊上下文是纯文本的，而 `deepseek-flash` 支持图像理解。所以收到图片/表情包时，
先把图片内容转成一句中文描述（带缓存，同一张图只识别一次），再把描述塞进对话上下文。
这样人设、上下文、拆分发送等逻辑完全不用改。

限制
====
* 单张图在模型里最多算 1024 token，`detail=low` 更快更省（默认）；
* 大图/动图会先本地压缩（动图取首帧、超大图缩到 1600px 内）；
* 识别结果按图片内容 SHA1 缓存，避免同一张图反复花钱。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import time
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from .config import DeepSeekConfig, VisionConfig
from . import net

log = logging.getLogger("dsbot.vision")

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class VisionClient:
    """用 DeepSeek 视觉能力把图片/表情包转成一句话描述。"""

    def __init__(self, ds: DeepSeekConfig, vcfg: VisionConfig) -> None:
        self.ds = ds
        self.vcfg = vcfg
        self._cache: dict[str, str] = {}
        self._call_times: deque[float] = deque()
        self._lock = threading.RLock()
        from openai import OpenAI

        self._client = OpenAI(
            api_key=ds.resolve_api_key(),
            base_url=ds.base_url,
            timeout=max(60.0, ds.timeout),
            max_retries=0,
            http_client=net.openai_http_client(max(60.0, ds.timeout)),
        )

    # ------------------------------------------------------------------
    def describe(self, image_path: str | Path, kind: str = "image") -> str:
        """识别一张图；kind 为 image / sticker。失败返回空字符串。"""
        if not self.vcfg.enabled:
            return ""
        path = Path(image_path)
        if not path.exists():
            return ""
        try:
            with open(path, "rb") as fh:
                head = fh.read(12)
            valid = (
                head[:3] == b"\xff\xd8\xff"
                or head[:4] in (b"\x89PNG", b"GIF8", b"RIFF")
            )
            if not valid:
                log.debug("跳过非图片文件（加密表情缓存/未知格式）：%s", path.name)
                return ""
            data, mime = self._prepare(path, self.vcfg.detail)
        except Exception as exc:  # noqa: BLE001
            log.warning("图片预处理失败 %s：%s", path, exc)
            return ""

        key = kind + ":" + hashlib.sha1(data).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not self._rate_ok():
            return ""

        prompt = self.vcfg.sticker_prompt if kind == "sticker" else self.vcfg.image_prompt
        b64 = base64.b64encode(data).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                            "detail": self.vcfg.detail,
                        },
                    },
                ],
            }
        ]
        started = time.time()
        try:
            resp = self._client.chat.completions.create(
                model=self.ds.model,
                messages=messages,
                # 推理模型会把一部分预算花在"看图思考"上，给足 2000，
                # 否则可能 reasoning 用满、正文为空。
                max_tokens=2000,
                temperature=0.3,
            )
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            # 模型偶尔会在结尾自报字数："（49字）"，去掉再记日志
            text = " ".join(text.split())
            text = re.sub(r"[（(]\s*\d+\s*字\s*[）)]\s*$", "", text).strip()
            text = text[: self.vcfg.max_chars]
            log.info(
                "识图完成（%s，%.1fs，%s tokens）：%s",
                "表情包" if kind == "sticker" else "图片",
                time.time() - started,
                getattr(usage, "total_tokens", "?"),
                text[:60] or "（空）",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("识图调用失败：%s", exc)
            return ""

        if text:
            with self._lock:
                self._cache[key] = text
                while len(self._cache) > self.vcfg.cache_size:
                    self._cache.pop(next(iter(self._cache)))
        return text

    # ------------------------------------------------------------------
    def _rate_ok(self) -> bool:
        with self._lock:
            return self._rate_ok_locked()

    def _rate_ok_locked(self) -> bool:
        limit = self.vcfg.max_calls_per_minute
        if limit <= 0:
            return True
        now = time.time()
        while self._call_times and now - self._call_times[0] > 60:
            self._call_times.popleft()
        if len(self._call_times) >= limit:
            return False
        self._call_times.append(now)
        return True

    @staticmethod
    def _prepare(path: Path, detail: str = "low") -> tuple[bytes, str]:
        """读图片并按需压缩：动图取首帧、超大图按 detail 缩放、格式不识别就转 JPEG。

        detail=high 时保留更高分辨率（2048px）并提高 JPEG 质量，方便看清图里的文字。
        """
        data = path.read_bytes()
        suffix = path.suffix.lower()
        mime = _MIME.get(suffix, "")

        need_convert = False
        if not mime:
            need_convert = True
        if suffix == ".gif" and len(data) > 4 * 1024 * 1024:
            need_convert = True
        if len(data) > 4 * 1024 * 1024:
            need_convert = True

        if need_convert:
            from PIL import Image

            max_side = 2048 if detail in ("high", "original") else 1600
            quality = 90 if detail in ("high", "original") else 85
            with Image.open(io.BytesIO(data)) as im:
                if suffix == ".gif":
                    im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=quality)
            return buf.getvalue(), "image/jpeg"
        return data, mime
