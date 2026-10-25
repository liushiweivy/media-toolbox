# -*- coding: utf-8 -*-
"""日语视频 → 每 N 秒一个独立字幕 SRT（全局时间轴），边识别边产出。

用法：
  .venv/Scripts/python.exe tools/ja2zh_srt_parts.py <视频路径> [每段秒数，默认600] [--ja-only]

产出（源文件同目录）：
  --ja-only（默认段文件 .partNN.ja.srt）：只识别不翻译，适合先出日文字幕
  默认（.partNN.zh.srt）：识别后方舟翻译，段内按 50 句/子批
  所有分段时间戳均为整片全局时间，生成后立即可在整片播放时加载
  全部段完成后自动合并为整片 <原名>.ja.srt（及 <原名>.zh.srt）
段级 ckpt 断点续传：中断后重跑同命令，已完成段直接跳过（翻译模式不重复烧 token）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from server import stt  # noqa: E402
from ja2zh_srt import BATCH, translate_batch, write_srt  # noqa: E402

VAD = {"threshold": 0.15, "min_silence_duration_ms": 300, "speech_pad_ms": 400}


def duration_of(src: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def transcribe_part(model, src: Path, off: float, dur: float, wav: Path,
                    language: str = "ja") -> list[dict]:
    """从源视频直接切 [off, off+dur] 的 16k wav 并识别，返回带全局偏移的 segs。

    language="auto" 时不指定语种交 whisper 自动检测。
    """
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(off), "-t", str(dur), "-i", str(src),
                    "-ac", "1", "-ar", "16000", str(wav)], check=True)
    kwargs = dict(beam_size=5, vad_filter=True, vad_parameters=VAD,
                  no_speech_threshold=0.8, condition_on_previous_text=False)
    if language != "auto":
        kwargs["language"] = language
    segments, _ = model.transcribe(str(wav), **kwargs)
    return [{"start": round(s.start + off, 2),
             "end": round(s.end + off, 2),
             "text": s.text.strip()}
            for s in segments]


def _score_sample(model, sample: Path,
                  language: str | None) -> tuple[float, str, float, float]:
    """识别一个样本，返回 (时长加权 avg_logprob, 自动语种, 概率, 语音时长s)。

    强制 language 时中间两项无意义；语言检测惰性，必须消费 segment。
    语音时长 <2s 视为无效样本（采样点落在音乐/无声段），调用方应丢弃。
    """
    kwargs: dict = {"beam_size": 5, "vad_filter": True}
    if language:
        kwargs["language"] = language
    segments, info = model.transcribe(str(sample), **kwargs)
    dur = lp = 0.0
    auto_lang, auto_prob = "", 0.0
    for s in segments:
        d = max(0.01, s.end - s.start)
        dur += d
        lp += float(getattr(s, "avg_logprob", 0.0) or 0.0) * d
        if not language:
            auto_lang = info.language
            auto_prob = float(getattr(info, "language_probability", 0.0) or 0.0)
    return (lp / dur if dur else -1.0), auto_lang, auto_prob, dur


MIN_SPEECH_S = 2.0  # 样本有效语音下限


def _auto_samples(model, src: Path, offsets: list[float], sample_dur: float,
                  wav: Path) -> tuple[list[Path], dict[str, float]]:
    """对多个时间点采样并自动检测，丢弃无对白样本，返回 (样本文件, 语种概率加权)。"""
    samples: list[Path] = []
    votes: dict[str, float] = {}
    for k, off in enumerate(offsets):
        sample = wav.with_name(wav.stem + f".s{k}.wav")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", str(off), "-t", str(sample_dur), "-i", str(src),
                        "-ac", "1", "-ar", "16000", str(sample)], check=True)
        _lp, lang, prob, dur = _score_sample(model, sample, None)
        if dur < MIN_SPEECH_S or not lang:
            print(f"    样本 {k + 1}@{off:.0f}s 无对白（{dur:.1f}s），丢弃", flush=True)
            sample.unlink(missing_ok=True)
            continue
        samples.append(sample)
        votes[lang] = votes.get(lang, 0.0) + prob
    return samples, votes


def detect_language(model, src: Path, total_dur: float, wav: Path) -> str:
    """检测语种（针对“日语为主 + 少量中文偷拍、无英语正片”的库调过）。

    长片 6 点加密采样（8/25/42/60/78/92%，各 45s），对白稀疏也要尽量命中：
    - ja 特异性极高：中文片自动检测从不投 ja，而日语片哪怕音乐/气声多也会
      在某点投出 ja。任一有效样本 ja 概率 ≥0.4 即判 ja（实测 SSNI-866 0.82、
      SNIS-704 0.45、AUKS-081 0.73 均为日语）；
    - 完全无 ja 票时，zh 票主导（总概率 ≥en）判 zh（中文片跳过）；
    - 其余 en 杂票/低置信一律兜底 ja——库里 en 结果经证实全是日语片的
      英语幻觉，强制 ja 正式识别即出真日语。
    不用 zh/en 强制识别 logprob 仲裁：中文片强制 ja 也能用假名音译拿高分，
    日语片强制 zh 也能用汉字转写拿高分，不可靠。
    """
    try:
        if total_dur > 600:
            offsets = [total_dur * x for x in (0.08, 0.25, 0.42, 0.60, 0.78, 0.92)]
            sample_dur = 45.0
        else:
            offsets = [total_dur * x for x in (0.3, 0.7)]
            sample_dur = min(45.0, total_dur / 3)

        samples, votes = _auto_samples(model, src, offsets, sample_dur, wav)
        # 有效样本太少（多数采样点落在无声/音乐）时再补一轮中间点位
        if len(samples) < 2 and total_dur > 600:
            extra = [total_dur * x for x in (0.15, 0.35, 0.55, 0.7, 0.85)]
            more, mvotes = _auto_samples(model, src, extra, sample_dur, wav)
            for k, v in mvotes.items():
                votes[k] = votes.get(k, 0.0) + v
        print(f"    语种投票：{votes}", flush=True)

        ja = votes.get("ja", 0.0)
        if ja >= 0.4:
            return "ja"
        # 有 ja 票但低于阈值、或无 ja：中文片 zh 票应明显压过 en 杂票
        zh, en = votes.get("zh", 0.0), votes.get("en", 0.0)
        if ja == 0.0 and zh > 0.0 and zh >= en:
            return "zh"
        return "ja"  # 无有效对白 / en 杂票 / 库以日语为主，兜底 ja
    finally:
        for f in wav.parent.glob(wav.stem + ".s*.wav"):
            f.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)


def translate_in_batches(segs: list[dict], part_no: int, n_parts: int) -> list[str]:
    """段内仍按 BATCH 句/子批翻译——整段一次提交（如 100+ 句）会被审查截断只回一行。"""
    zh: list[str] = []
    for j in range(0, len(segs), BATCH):
        zh.extend(translate_batch(segs[j:j + BATCH]))
        print(f"段 {part_no}/{n_parts} 翻译 {len(zh)}/{len(segs)}", flush=True)
    return zh


def main() -> None:
    args = sys.argv[1:]
    ja_only = "--ja-only" in args
    args = [a for a in args if a != "--ja-only"]
    src = Path(args[0])
    chunk = float(args[1]) if len(args) > 1 else 600
    lang = "ja" if ja_only else "zh"
    base = src.with_suffix("")
    ckpt_path = Path(f"{base}.parts.{lang}.ckpt.json")

    total_dur = duration_of(src)
    n = max(1, int(total_dur // chunk) + (1 if total_dur % chunk else 0))
    print(f"总长 {total_dur:.0f}s，分 {n} 段（{chunk:.0f}s/段），模式："
          f"{'仅日文' if ja_only else '日译中'}", flush=True)

    parts: dict[int, dict] = {}
    if ckpt_path.is_file():
        try:
            saved = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if saved.get("src") == str(src) and abs(saved.get("chunk", chunk) - chunk) < 1:
                for p in saved["parts"]:
                    parts[p["idx"]] = p
                print(f"续传：已完成 {len(parts)} 段", flush=True)
        except (OSError, json.JSONDecodeError, KeyError):
            parts = {}

    model = stt.get_model()
    t0 = time.time()
    for i in range(n):
        if i in parts:
            continue
        off = i * chunk
        part_wav = Path(f"{base}.part{i + 1:02d}.tmp.wav")
        part_srt = Path(f"{base}.part{i + 1:02d}.{lang}.srt")
        try:
            segs = transcribe_part(model, src, off, chunk, part_wav)
            if ja_only:
                texts = [s["text"] for s in segs]
            else:
                print(f"段 {i + 1}/{n} 识别 {len(segs)} 句，翻译中…", flush=True)
                texts = translate_in_batches(segs, i + 1, n) if segs else []
            write_srt(part_srt, segs, texts)
            parts[i] = {"idx": i, "segs": segs, "texts": texts}
            ckpt_path.write_text(
                json.dumps({"src": str(src), "chunk": chunk,
                            "parts": list(parts.values())}, ensure_ascii=False),
                encoding="utf-8")
            print(f"  → 已产出 {part_srt.name}（{len(segs)} 句，累计 "
                  f"{len(parts)}/{n}，{time.time() - t0:.0f}s）", flush=True)
        finally:
            part_wav.unlink(missing_ok=True)

    ordered = [parts[i] for i in range(n) if i in parts]
    all_segs = [s for p in ordered for s in p["segs"]]
    ja_dst = Path(f"{base}.ja.srt")
    write_srt(ja_dst, all_segs, [s["text"] for s in all_segs])
    if not ja_only:
        all_texts = [t for p in ordered for t in p["texts"]]
        write_srt(Path(f"{base}.zh.srt"), all_segs, all_texts)
    ckpt_path.unlink(missing_ok=True)
    print(f"全部完成：{len(all_segs)} 句，耗时 {time.time() - t0:.0f}s\n"
          f"合并日文: {ja_dst}", flush=True)


if __name__ == "__main__":
    main()
