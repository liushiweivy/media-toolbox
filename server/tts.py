# -*- coding: utf-8 -*-
"""文字转语音：edge-tts（微软在线语音，免费、中文音色好、无需 key）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

VOICES = {
    "xiaoxiao": ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声"),
    "yunxi": ("zh-CN-YunxiNeural", "云希 · 男声"),
    "yunjian": ("zh-CN-YunjianNeural", "云健 · 男声有力"),
    "xiaoyi": ("zh-CN-XiaoyiNeural", "晓伊 · 女声轻快"),
    "en-aria": ("en-US-AriaNeural", "Aria · 英语女声"),
    "en-guy": ("en-US-GuyNeural", "Guy · 英语男声"),
    "ja-nanami": ("ja-JP-NanamiNeural", "Nanami · 日语女声"),
    "ko-sunhi": ("ko-KR-SunHiNeural", "SunHi · 韩语女声"),
}

MAX_CHARS = 8000


def synthesize(text: str, dst: Path, voice_key: str = "xiaoxiao", rate: int = 0) -> dict:
    """合成到 mp3。rate: -50~+50（百分比语速）。返回 meta。"""
    text = text.strip()
    if not text:
        raise ValueError("文本为空")
    if len(text) > MAX_CHARS:
        raise ValueError(f"文本超长（{len(text)} > {MAX_CHARS} 字），请分段合成")
    if voice_key not in VOICES:
        raise ValueError(f"未知音色：{voice_key}")
    voice = VOICES[voice_key][0]
    rate = max(-50, min(rate, 50))
    rate_str = f"{rate:+d}%"

    import edge_tts

    async def _run() -> None:
        comm = edge_tts.Communicate(text, voice, rate=rate_str)
        await comm.save(str(dst))

    asyncio.run(_run())
    if not dst.is_file() or dst.stat().st_size < 1024:
        raise RuntimeError("合成结果异常（文件过小），检查网络后重试")
    return {"voice": voice, "chars": len(text)}
