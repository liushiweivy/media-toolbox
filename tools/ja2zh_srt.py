# -*- coding: utf-8 -*-
"""日语音视频 → 中文字幕 SRT 流水线：whisper 本地识别（零 token）→ 方舟批量翻译 → SRT。

用法：.venv/Scripts/python.exe tools/ja2zh_srt.py <视频路径> [输出.srt]
默认输出到源文件同目录 `<原名>.zh.srt`，并留 `<原名>.ja.srt` 原文对照。

省 token 设计：50 行/批减少请求数；识别本地完成；断点 ckpt 每批落盘，
中断后重跑同命令自动续传（不重复消耗已翻部分的 token）。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import media, stt, translate  # noqa: E402

BATCH = 50  # 每批翻译行数（越大请求数越少、单位 token 开销越低；掉行自动降级逐行）


def ts(sec: float) -> str:
    ms = round((sec % 1) * 1000)
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},{ms:03d}"


def write_srt(path: Path, segs: list[dict], texts: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for i, (s, t) in enumerate(zip(segs, texts), 1):
            f.write(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{t}\n\n")


CHUNK = 600  # 秒/段：长片分段独立识别，小声对白召回显著优于整片一次识别


def transcribe_chunked(wav: Path) -> list[dict]:
    """把 16k wav 切 CHUNK 秒/段独立识别，时间戳加段偏移后合并。"""
    import math
    import subprocess

    model = stt.get_model()
    duration = (wav.stat().st_size - 44) / 32000  # 16kHz 16bit mono
    n = max(1, math.ceil(duration / CHUNK))
    segs: list[dict] = []
    for i in range(n):
        off = i * CHUNK
        part = wav.with_name(f"{wav.stem}.part{i}.wav")
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", str(off), "-t", str(CHUNK), "-i", str(wav),
                            "-ac", "1", "-ar", "16000", str(part)], check=True)
            # 小声/气声优化：VAD 阈值降低 + 语音前后留白；no_speech 阈值提高
            # 避免低音量对白被判静音丢弃；关上文条件防重复幻听
            segments, _info = model.transcribe(
                str(part), language="ja", beam_size=5,
                vad_filter=True,
                vad_parameters={"threshold": 0.15,
                                "min_silence_duration_ms": 300,
                                "speech_pad_ms": 400},
                no_speech_threshold=0.8,
                condition_on_previous_text=False)
            for s in segments:
                segs.append({"start": round(s.start + off, 2),
                             "end": round(s.end + off, 2),
                             "text": s.text.strip()})
            print(f"  → 段 {i + 1}/{n}（累计 {len(segs)} 句）", flush=True)
        finally:
            part.unlink(missing_ok=True)
    return segs


def translate_batch(batch: list[dict]) -> list[str]:
    payload = "\n".join(f"{k + 1}. {s['text']}" for k, s in enumerate(batch))
    out = translate.chat(
        "把下面的日语视频字幕逐行翻译成自然、口语化的中文。"
        "保持编号与行数完全一致，只输出译文，不要解释：\n" + payload,
        timeout=300, retries=3)
    texts = [re.sub(r"^\d+\s*[.、)]\s*", "", ln).strip()
             for ln in out.split("\n") if ln.strip()]
    if len(texts) == len(batch):
        return texts
    print(f"  ! 批量对齐失败（{len(texts)}/{len(batch)}），降级逐行翻译", flush=True)
    out = []
    for s in batch:
        try:
            out.append(translate.chat("把这句日语翻译成自然中文，只输出译文：" + s["text"],
                                      timeout=120, retries=2).strip())
        except Exception:  # noqa: BLE001 - 单行失败不拖垮整批，保原文占位
            print(f"  ! 单行翻译失败，保留原文：{s['text'][:20]}…", flush=True)
            out.append("【未译】" + s["text"])
    return out


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + ".zh.srt")
    ja_dst = dst.with_name(dst.stem[:-3] + ".ja.srt") \
        if dst.stem.endswith(".zh") else dst.with_suffix(".ja.srt")
    ckpt = dst.with_name(dst.stem + ".ckpt.json")

    t0 = time.time()
    segs: list[dict] = []
    zh: list[str] = []
    if ckpt.is_file():  # 断点续传
        try:
            saved = json.loads(ckpt.read_text(encoding="utf-8"))
            if saved.get("src") == str(src):
                segs, zh = saved["segs"], saved["zh"]
                print(f"续传：已有识别 {len(segs)} 句、已翻 {len(zh)} 句", flush=True)
        except (OSError, json.JSONDecodeError, KeyError):
            segs, zh = [], []

    if not segs:
        wav = src.with_suffix(".16k.tmp.wav")
        print("1/3 转码 16kHz …", flush=True)
        media.to_wav16k(src, wav)
        try:
            print("2/3 whisper 识别（ja，分段+高灵敏度 VAD，本地零 token）…", flush=True)
            segs = transcribe_chunked(wav)
        finally:
            wav.unlink(missing_ok=True)
        print(f"  → {len(segs)} 句，耗时 {time.time() - t0:.0f}s", flush=True)
        write_srt(ja_dst, segs, [s["text"] for s in segs])
        ckpt.write_text(json.dumps({"src": str(src), "segs": segs, "zh": []},
                                   ensure_ascii=False), encoding="utf-8")

    total = len(segs)
    print(f"3/3 方舟翻译（{total} 句，{(total + BATCH - 1) // BATCH} 批）…", flush=True)
    while len(zh) < total:
        batch = segs[len(zh):len(zh) + BATCH]
        zh.extend(translate_batch(batch))
        ckpt.write_text(json.dumps({"src": str(src), "segs": segs, "zh": zh},
                                   ensure_ascii=False), encoding="utf-8")
        print(f"  → {len(zh)}/{total}（{time.time() - t0:.0f}s）", flush=True)

    write_srt(dst, segs, zh)
    write_srt(ja_dst, segs, [s["text"] for s in segs])
    ckpt.unlink(missing_ok=True)
    print(f"完成 {time.time() - t0:.0f}s\n中文: {dst}\n原文: {ja_dst}", flush=True)


if __name__ == "__main__":
    main()
