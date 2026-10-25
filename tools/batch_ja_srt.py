# -*- coding: utf-8 -*-
"""批量字幕：扫描目录下所有正片视频，逐个只识别（不翻译）产出完整 SRT。

用法：.venv/Scripts/python.exe tools/batch_ja_srt.py <目录> [每段秒数=600]
          [--min-mb 500] [--lang ja|auto]

- --lang ja（默认）：强制日语识别，产出 <名>.ja.srt；
- --lang auto：每片取样检测语种并全片统一，产出 <名>.<ja|zh|en|…>.srt，
  适合混有中文/英语内容的影视库；
- 模型只加载一次，多片复用；已有非空同名字幕的视频跳过；
- 每片即时产出 <名>.partNN.<lang>.srt（全局时间轴），完成后合并整片 SRT；
- 单片异常只记录不中断，最后汇总。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from server import stt  # noqa: E402
from ja2zh_srt import write_srt  # noqa: E402
from ja2zh_srt_parts import (  # noqa: E402
    VAD, detect_language, duration_of, transcribe_part)

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".webm"}
KNOWN_LANG = ("ja", "zh", "en")


def existing_subtitle(base: Path) -> Path | None:
    for lang in KNOWN_LANG:
        p = Path(f"{base}.{lang}.srt")
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def process_video(model, src: Path, chunk: float, lang_mode: str) -> tuple[str, int, str]:
    """返回 (状态, 句数, 语言)。状态：skip / done / resume-done。"""
    base = src.with_suffix("")
    total_dur = duration_of(src)

    pre = existing_subtitle(base)
    if pre:
        return "skip", pre.stat().st_size, pre.suffixes[-2].lstrip(".")

    if lang_mode == "auto":
        sample = Path(f"{base}.langdetect.tmp.wav")
        lang = detect_language(model, src, total_dur, sample)
        print(f"  检测语种：{lang}（时长 {total_dur:.0f}s）", flush=True)
        if lang == "zh":
            return "skip-zh", 0, lang
    else:
        lang = "ja"

    # 清理异语种的中断残留（语言改判或旧任务遗留），避免错误字幕混入
    for old in list(base.parent.glob(base.name + ".part*.*.srt")) + \
               list(base.parent.glob(base.name + ".parts.*.ckpt.json")):
        if f".{lang}." not in old.name:
            old.unlink(missing_ok=True)

    ja_dst = Path(f"{base}.{lang}.srt")
    ckpt_path = Path(f"{base}.parts.{lang}.ckpt.json")
    n = max(1, int(total_dur // chunk) + (1 if total_dur % chunk else 0))
    parts: dict[int, dict] = {}
    resumed = False
    if ckpt_path.is_file():
        try:
            saved = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if saved.get("src") == str(src) and abs(saved.get("chunk", chunk) - chunk) < 1:
                for p in saved["parts"]:
                    parts[p["idx"]] = p
                resumed = bool(parts)
        except (OSError, json.JSONDecodeError, KeyError):
            parts = {}
    print(f"  {n} 段" + (f"，续传 {len(parts)} 段" if resumed else ""), flush=True)

    t0 = time.time()
    for i in range(n):
        if i in parts:
            continue
        off = i * chunk
        part_wav = Path(f"{base}.part{i + 1:02d}.tmp.wav")
        part_srt = Path(f"{base}.part{i + 1:02d}.{lang}.srt")
        try:
            segs = transcribe_part(model, src, off, chunk, part_wav, language=lang)
            write_srt(part_srt, segs, [s["text"] for s in segs])
            parts[i] = {"idx": i, "segs": segs}
            ckpt_path.write_text(
                json.dumps({"src": str(src), "chunk": chunk, "lang": lang,
                            "parts": list(parts.values())}, ensure_ascii=False),
                encoding="utf-8")
            print(f"    part{i + 1:02d}/{n}: {len(segs)} 句（累计 {len(parts)}/"
                  f"{n}，{time.time() - t0:.0f}s）", flush=True)
        finally:
            part_wav.unlink(missing_ok=True)

    ordered = [parts[i] for i in range(n) if i in parts]
    all_segs = [s for p in ordered for s in p["segs"]]
    write_srt(ja_dst, all_segs, [s["text"] for s in all_segs])
    ckpt_path.unlink(missing_ok=True)
    return ("resume-done" if resumed else "done"), len(all_segs), lang


def main() -> None:
    args = sys.argv[1:]
    min_mb = 500
    if "--min-mb" in args:
        j = args.index("--min-mb")
        min_mb = int(args[j + 1])
        del args[j:j + 2]
    lang_mode = "ja"
    if "--lang" in args:
        j = args.index("--lang")
        lang_mode = args[j + 1]
        del args[j:j + 2]
    root = Path(args[0])
    chunk = float(args[1]) if len(args) > 1 else 600

    videos = sorted(
        (p for p in root.rglob("*")
         if p.is_file() and p.suffix.lower() in VIDEO_EXT
         and p.stat().st_size >= min_mb * 1024 * 1024),
        key=lambda p: str(p))
    print(f"发现 {len(videos)} 个 ≥{min_mb}MB 的视频，语种模式 {lang_mode}，"
          f"模型加载中…", flush=True)
    model = stt.get_model()
    print(f"模型就绪：{stt.device_info()}", flush=True)

    results = []
    for k, v in enumerate(videos, 1):
        print(f"\n[{k}/{len(videos)}] {v}", flush=True)
        try:
            status, n_seg, lang = process_video(model, v, chunk, lang_mode)
            print(f"  => {status} [{lang}]（{n_seg} 句）", flush=True)
            results.append((str(v.relative_to(root)), status, n_seg, lang, ""))
        except Exception as e:  # noqa: BLE001 - 单片失败不拖垮整批
            traceback.print_exc()
            results.append((str(v.relative_to(root)), "FAIL", 0, "?", str(e)))

    print("\n===== 汇总 =====", flush=True)
    for name, status, n_seg, lang, err in results:
        line = f"{status:11s} [{lang}] {n_seg:4d} 句  {name}"
        if err:
            line += f"  ERROR: {err[:80]}"
        print(line, flush=True)


if __name__ == "__main__":
    main()
