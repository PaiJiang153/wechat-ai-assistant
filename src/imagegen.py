"""生图：模型自己决定何时画图，生成后由适配器以图片消息发出。

默认用 Pollinations（免费、无需 Key，底层 FLUX），也支持任意 OpenAI 兼容的
生图接口（OpenAI Images / 硅基流动 / 智谱等），在 config.yaml 的 imagegen 里切换。

流程
====
模型在 Tool Calls 里调用 generate_image(prompt) → 本模块生成/下载图片到
data/media/generated/ → 返回文件路径 → engine 记下来 → bot 用 send_file 发出去
（走 GUI 的 send_image，微信里显示为图片/动图，不是文件）。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import net
from .config import ImageGenConfig
from .runtime import atomic_json

log = logging.getLogger("dsbot.imagegen")

GENERATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "当对方让你画一张图、生成图片、来张图、做表情包/示意图/头像时调用。"
            "生成后图片会自动发到聊天里，你只需要再配一句话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "画面的详细中文描述：主体（长相/表情）、动作、场景、风格"
                        "（卡通插画/写实/水彩/像素等）、配色、构图；写具体，别只写一句话"
                    ),
                },
                "size": {
                    "type": "string",
                    "description": "可选，如 1024x1024、1024x768",
                },
            },
            "required": ["prompt"],
        },
    },
}


class ImageGenerator:
    def __init__(self, cfg: ImageGenConfig, llm: Any = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.out_dir = Path(cfg.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._day = time.strftime("%Y-%m-%d")
        self._sent_today = 0
        self._minute_calls: list[float] = []
        self._quota_path = self.out_dir / "quota.json"
        if self._quota_path.exists():
            try:
                import json
                saved = json.loads(self._quota_path.read_text(encoding="utf-8"))
                if saved.get("day") == self._day:
                    self._sent_today = int(saved.get("attempts", 0))
            except (ValueError, OSError):
                pass

    # ------------------------------------------------------------------
    def generate(self, prompt: str, size: str | None = None) -> str | None:
        """生成一张图，返回本地文件路径；失败返回 None。"""
        prompt = (prompt or "").strip()
        if not self.cfg.enabled or not prompt:
            return None
        size = (size or self.cfg.size or "1024x1024").strip() or "1024x1024"
        if not re.fullmatch(r"\d{2,4}x\d{2,4}", size):
            size = self.cfg.size or "1024x1024"
        # Pollinations(FLUX) 对中文提示词理解很差，先翻成英文再画；Gemini 中文理解好，不用翻
        original_prompt = prompt
        if self.cfg.translate_prompt and self.cfg.provider == "pollinations":
            prompt = self._english_prompt(prompt)
            if not prompt:
                prompt = original_prompt
        if self.cfg.style_suffix:
            prompt = f"{prompt}, {self.cfg.style_suffix}"

        key = hashlib.sha1(f"{prompt}|{size}|{self.cfg.provider}|{self.cfg.model}".encode()).hexdigest()[:16]
        cached = self._find_cached(key)
        if cached:
            log.info("生图命中缓存：%s", cached.name)
            return str(cached)
        with self._lock:
            if not self._rate_ok():
                log.warning("生图频率超限（今天 %d 次），本次跳过", self._sent_today)
                return None
            self._sent_today += 1
            self._minute_calls.append(time.time())
            atomic_json(self._quota_path, {"day": self._day, "attempts": self._sent_today})

        started = time.time()
        try:
            if self.cfg.provider in ("openai", "zhipu"):
                path = self._generate_openai(prompt, size, key)
            elif self.cfg.provider == "gemini":
                path = self._generate_gemini(prompt, key)
            else:
                path = self._generate_pollinations(prompt, size, key)
        except Exception as exc:  # noqa: BLE001
            log.warning("生图失败（%s）：%s", self.cfg.provider, exc)
            return None
        if path:
            # Explicit retention cleanup avoids deleting queued attachments.
            log.info("生图完成（%s，%.1fs）：%s", self.cfg.provider, time.time() - started, Path(path).name)
            return str(path)
        return None

    # ------------------------------------------------------------------ 提示词
    def _english_prompt(self, prompt: str) -> str:
        """把中文画面描述翻成英文提示词（图像模型对英文更敏感）。失败返回空串。"""
        if not self.llm or not re.search(r"[\u4e00-\u9fff]", prompt):
            return prompt
        try:
            text = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You turn an image description into a short English prompt for an image model. "
                            "Keep subject, action, style and colors; output ONLY the English prompt, no quotes, "
                            "no explanation, max 60 words."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=300,
            )
            text = " ".join((text or "").split()).strip().strip('"')
            if text:
                log.info("生图提示词翻译：%s", text[:120])
                return text
        except Exception as exc:  # noqa: BLE001
            log.debug("提示词翻译失败，沿用原文：%s", exc)
        return prompt

    # ------------------------------------------------------------------ 供应商
    def _generate_pollinations(self, prompt: str, size: str, key: str) -> str | None:
        width, height = size.split("x")
        params = {
            "width": width,
            "height": height,
            "model": self.cfg.model or "flux",
            "nologo": "true",
            "seed": str(random.randint(1, 2**31 - 1)),
        }
        url = f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}"
        last_error = ""
        for attempt in range(3):
            resp = net.get(url, params=params, timeout=self.cfg.timeout)
            ctype = (resp.headers.get("content-type") or "").lower()
            if resp.status_code == 200 and "image" in ctype:
                ext = ".png" if "png" in ctype else ".jpg"
                return str(self._write(resp.content, key, ext))
            # 免费版限制：每 45 秒 1 张；按服务端提示的秒数等待后重试
            text = resp.text[:300] if "json" in ctype else f"HTTP {resp.status_code}"
            last_error = text
            retry_after = 20
            if "Retry after" in text:
                m = re.search(r"Retry after (\d+)s", text)
                if m:
                    retry_after = min(45, int(m.group(1)) + 1)
            if resp.status_code in (402, 429, 500, 502, 503, 504) and attempt < 2:
                log.warning("生图限流/失败（%s），%ds 后重试", resp.status_code, retry_after)
                time.sleep(retry_after)
                continue
            break
        raise RuntimeError(f"Pollinations 未返回图片：{last_error[:160]}")

    def _generate_openai(self, prompt: str, size: str, key: str) -> str | None:
        api_key = self.cfg.resolve_api_key()
        if not api_key:
            raise RuntimeError("imagegen.api_key 未配置（或设置环境变量 IMAGEGEN_API_KEY）")
        base_url = self.cfg.base_url or ""
        model = self.cfg.model or "gpt-image-1"
        if self.cfg.provider == "zhipu":
            base_url = base_url or "https://open.bigmodel.cn/api/paas/v4"
            model = self.cfg.model or "cogview-3-flash"
        url = base_url.rstrip("/")
        if not url.endswith("/images/generations"):
            url += "/images/generations"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": 1,
        }
        payload.update(self.cfg.extra_params or {})
        resp = net.post(url, headers=headers, json=payload, timeout=self.cfg.timeout)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or []
        if not data:
            raise RuntimeError(f"接口没有返回图片：{resp.text[:120]}")
        item = data[0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
            return str(self._write(raw, key, self._sniff_ext(raw)))
        if item.get("url"):
            img = net.get(item["url"], timeout=self.cfg.timeout)
            img.raise_for_status()
            raw = img.content
            ctype = img.headers.get("content-type") or ""
            return str(self._write(raw, key, self._sniff_ext(raw, ctype)))
        raise RuntimeError("接口返回里既没有 b64_json 也没有 url")

    @staticmethod
    def _sniff_ext(content: bytes, ctype: str = "") -> str:
        """按文件头判断真实图片格式（有的接口 URL 后缀是 png，内容其实是 jpeg）。"""
        head = content[:12]
        if head[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if head[:4] == b"\x89PNG":
            return ".png"
        if head[:4] == b"GIF8":
            return ".gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        if "jpeg" in ctype.lower():
            return ".jpg"
        if "webp" in ctype.lower():
            return ".webp"
        return ".png"

    def _generate_gemini(self, prompt: str, cache_key: str) -> str | None:
        """Google Gemini 生图（gemini-2.5-flash-image / Nano Banana）。"""
        api_key = self.cfg.resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "Gemini Key 未配置：去 https://aistudio.google.com/apikey 生成一个，"
                "填到 config.yaml 的 imagegen.api_key（或环境变量 GEMINI_API_KEY）"
            )
        base = (self.cfg.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        if "/v1beta" not in base and "/v1" not in base:
            base += "/v1beta"
        model = self.cfg.model or "gemini-2.5-flash-image"
        url = f"{base}/models/{model}:generateContent"
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        aspect = self._aspect_ratio()
        if aspect:
            payload["generationConfig"]["imageConfig"] = {"aspectRatio": aspect}
        if self.cfg.extra_params:
            payload["generationConfig"].update(self.cfg.extra_params)
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

        last_error = ""
        for attempt in range(3):
            resp = net.post(url, headers=headers, json=payload, timeout=self.cfg.timeout)
            if resp.status_code == 200:
                data = resp.json() or {}
                parts = (
                    ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                )
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data") or {}
                    if inline.get("data"):
                        mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
                        ext = ".jpg" if "jpeg" in mime else ".webp" if "webp" in mime else ".png"
                        return str(self._write(base64.b64decode(inline["data"]), cache_key, ext))
                texts = [p.get("text") for p in parts if p.get("text")]
                raise RuntimeError(f"Gemini 没有返回图片：{' / '.join(texts)[:160]}")
            last_error = f"HTTP {resp.status_code} {resp.text[:200]}"
            if resp.status_code == 429:
                text = resp.text or ""
                if "free_tier" in text or "billing" in text or "limit: 0" in text:
                    raise RuntimeError(
                        "Gemini 图像模型需要开通结算（免费层图像额度为 0），"
                        "去 https://aistudio.google.com/billing 绑定后即可；"
                        "或改用 provider: zhipu（智谱 CogView-3-Flash 免费）"
                    )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                wait = 8 * (attempt + 1)
                log.warning("Gemini 生图失败（%s），%ds 后重试", resp.status_code, wait)
                time.sleep(wait)
                continue
            break
        raise RuntimeError(f"Gemini 生图失败：{last_error}")

    def _aspect_ratio(self) -> str:
        try:
            w, h = (int(x) for x in str(self.cfg.size).lower().split("x"))
        except Exception:  # noqa: BLE001
            return ""
        from math import gcd

        g = gcd(w, h)
        ratio = f"{w // g}:{h // g}"
        return ratio if ratio in ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9") else ""

    # ------------------------------------------------------------------ 工具
    def _write(self, content: bytes, key: str, ext: str) -> Path:
        if not content or len(content) < 200:
            raise RuntimeError(f"图片内容异常（{len(content)} 字节）")
        path = self.out_dir / f"gen_{key}{ext}"
        path.write_bytes(content)
        return path

    def _find_cached(self, key: str) -> Path | None:
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            path = self.out_dir / f"gen_{key}{ext}"
            if path.exists() and path.stat().st_size > 200:
                path.touch()  # Last use, for maintenance retention and queued sends.
                return path
        return None

    def _rate_ok(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._sent_today = 0
        if self.cfg.max_per_day > 0 and self._sent_today >= self.cfg.max_per_day:
            return False
        now = time.time()
        self._minute_calls = [t for t in self._minute_calls if now - t < 60]
        if self.cfg.per_minute > 0 and len(self._minute_calls) >= self.cfg.per_minute:
            return False
        return True

    def _cleanup(self) -> None:
        if self.cfg.max_keep <= 0:
            return
        files = sorted(self.out_dir.glob("gen_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[self.cfg.max_keep :]:
            try:
                old.unlink()
            except OSError:
                pass
