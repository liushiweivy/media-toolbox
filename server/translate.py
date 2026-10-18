# -*- coding: utf-8 -*-
"""文字翻译：方舟 Coding Plan 端点（ark-code-latest，OpenAI chat 协议）。"""
from __future__ import annotations

import requests

from . import config

TARGETS = {
    "en": "英语", "zh": "中文", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "ru": "俄语", "es": "西班牙语",
}

CHUNK_CHARS = 3000      # 单次翻译上限，超长文本按段落切块
CHUNK_MAX = 10          # 最多块数（约 3 万字），防止滥用
TIMEOUT = 60


def chat(prompt: str, timeout: int = 0, retries: int = 0) -> str:
    """公共入口：单次方舟对话（翻译、AI 总结等共用）。长任务可加大 timeout 并带重试。"""
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            return _chat(prompt, timeout or TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 重试边界兜住网络类异常
            last = exc
    raise last  # type: ignore[misc]


def _chat(prompt: str, timeout: int = TIMEOUT) -> str:
    if not config.ARK_KEY:
        raise RuntimeError("未配置方舟 key：设 MT_ARK_KEY 环境变量或写 data/ark.key")
    resp = requests.post(
        f"{config.ARK_BASE}/api/coding/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.ARK_KEY}",
                 "Content-Type": "application/json"},
        json={"model": config.ARK_MODEL,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 4096},
        timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"翻译接口 {resp.status_code}：{resp.text[:200]}")
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def _split_chunks(text: str) -> list[str]:
    """按段落切块，每块 ≤ CHUNK_CHARS，尽量不切句子。"""
    paras = text.split("\n")
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        while len(p) > CHUNK_CHARS:  # 单段超长的硬切
            chunks.append(cur + p[:CHUNK_CHARS - len(cur)] if cur else p[:CHUNK_CHARS])
            p = p[CHUNK_CHARS - len(cur):] if cur else p[CHUNK_CHARS:]
            cur = ""
        cur = cur + ("\n" if cur else "") + p
    if cur.strip():
        chunks.append(cur)
    return chunks


def translate(text: str, target: str, progress_cb=None) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("文本为空")
    if target not in TARGETS:
        raise ValueError(f"不支持的目标语言：{target}")
    chunks = _split_chunks(text)
    if len(chunks) > CHUNK_MAX:
        raise ValueError(f"文本过长（{len(text)} 字切成 {len(chunks)} 段，超 {CHUNK_MAX} 段上限），请分段翻译")
    out = []
    for i, chunk in enumerate(chunks):
        out.append(_chat(
            f"你是专业翻译。把下面的原文翻译成{TARGETS[target]}，"
            f"保持原有格式与换行，只输出译文，不要任何解释。\n\n原文：\n{chunk}"))
        if progress_cb:
            progress_cb((i + 1) / len(chunks))
    return {"text": "\n".join(out), "target": TARGETS[target], "chunks": len(chunks)}
