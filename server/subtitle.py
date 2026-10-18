# -*- coding: utf-8 -*-
"""音视频 → 翻译字幕 SRT 流水线（web 任务版）。

从 tools/ja2zh_srt.py（日译中 CLI，已实战验证）泛化而来：
本地 whisper 识别（零 token）→ 方舟 50 行/批编号翻译（掉行降级逐行，
单行失败【未译】占位）→ 双语对照 / 纯译文 / 原文三份 SRT。

与 CLI 的差异：不做 ckpt 断点续传（web 任务一次性）；识别进度由
惰性生成器逐条驱动（seg.end/dur），不是 stt.transcribe 的包装——
因为这里要保留 ja2zh 的小声/气声 VAD 调参与 condition_on_previous_text=False。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import stt, translate

BATCH = 50  # 每批翻译行数（越大请求越少、单位 token 越省；掉行自动降级逐行）

SRC_LANGS = ("auto", "ja", "en", "zh", "ko")
LANG_NAMES = {"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语"}


def ts(sec: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm。"""
    sec = max(sec, 0.0)
    ms = round((sec % 1) * 1000)
    s = int(sec)
    if ms == 1000:  # round 进位（如 1.9996 → 2,000ms）
        s += 1
        ms = 0
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},{ms:03d}"


def write_srt(path: Path, segs: list[dict], texts: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for i, (s, t) in enumerate(zip(segs, texts), 1):
            # cue 内空行会被严格解析器当 cue 终止（译文回车/原文换行都要压掉）
            t = re.sub(r"\s*\n\s*", "\n", t.strip())
            t = "\n".join(ln for ln in t.split("\n") if ln.strip())
            f.write(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{t}\n\n")


def to_bilingual(segs: list[dict], translated: list[str]) -> list[str]:
    """每句「原文↵译文」两行文本（SRT cue 内多行，播放器自动折行）。"""
    return [f"{s['text']}\n{t}" for s, t in zip(segs, translated)]


def transcribe_segments(src: Path, language: str, dur: float, cb=None):
    """whisper 识别 → (segs[{start,end,text}], detected_lang)。

    直接调模型底层（而非 stt.transcribe）以保留 ja2zh 的调参：
    小声/气声优化 VAD（阈值降低+语音前后留白）、no_speech 阈值提高防
    低音量对白被判静音丢弃、关上文条件防重复幻听。生成器惰性，逐条
    迭代即得真进度 cb(done/total)。
    """
    model = stt.get_model()
    kwargs = dict(beam_size=5, vad_filter=True,
                  vad_parameters={"threshold": 0.15,
                                  "min_silence_duration_ms": 300,
                                  "speech_pad_ms": 400},
                  no_speech_threshold=0.8,
                  condition_on_previous_text=False)
    if language and language != "auto":
        kwargs["language"] = language
    segments, info = model.transcribe(str(src), **kwargs)
    segs = []
    for seg in segments:
        segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                     "text": seg.text.strip()})
        if cb and dur > 0:
            cb(min(seg.end / dur, 1.0))
    return segs, info.language


def _translate_batch(batch: list[dict], src_name: str, tgt_name: str) -> list[str]:
    payload = "\n".join(f"{k + 1}. {s['text']}" for k, s in enumerate(batch))
    out = translate.chat(
        f"把下面的{src_name}视频字幕逐行翻译成自然、口语化的{tgt_name}。"
        "保持编号与行数完全一致，只输出译文，不要解释：\n" + payload,
        timeout=300, retries=3)
    lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
    # 仅当首行确为「1.」编号才剥编号——否则会误吃译文自身的开头数字（如"3.14 是…"）
    numbered = bool(lines) and re.match(r"^1\s*[.、)]", lines[0])
    texts = [re.sub(r"^\d+\s*[.、)]\s*", "", ln).strip() if numbered else ln
             for ln in lines]
    if len(texts) == len(batch):
        return texts
    # 掉行/多行 → 降级逐行，单行失败不拖垮整批
    result = []
    for s in batch:
        try:
            result.append(translate.chat(
                f"把这句{src_name}翻译成自然{tgt_name}，只输出译文：" + s["text"],
                timeout=120, retries=2).strip())
        except Exception:  # noqa: BLE001 - 保原文占位
            result.append("【未译】" + s["text"])
    return result


def translate_segments(segs: list[dict], src_lang: str, target: str,
                       cb=None) -> list[str]:
    """50 行/批编号翻译；src_lang 用识别结果回填（auto 场景）。"""
    src_name = LANG_NAMES.get(src_lang, src_lang)
    tgt_name = translate.TARGETS[target]
    out: list[str] = []
    total = len(segs)
    while len(out) < total:
        batch = segs[len(out):len(out) + BATCH]
        out.extend(_translate_batch(batch, src_name, tgt_name))
        if cb:
            cb(len(out) / total)
    return out
