# -*- coding: utf-8 -*-
"""ffmpeg 封装：定位可执行文件、探测时长、视频提取音频。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

_DEFAULT_FFMPEG = r"F:\java\ffmpeg-7.1-essentials_build\bin\ffmpeg.exe"

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".opus"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".ts", ".m4v"}


def ffmpeg() -> str:
    exe = os.environ.get("MT_FFMPEG") or shutil.which("ffmpeg") or _DEFAULT_FFMPEG
    if not Path(exe).is_file():
        raise RuntimeError(f"找不到 ffmpeg：{exe}（可用 MT_FFMPEG 指定）")
    return exe


def ffprobe() -> str:
    exe = os.environ.get("MT_FFPROBE") or shutil.which("ffprobe")
    if exe and Path(exe).is_file():
        return exe
    # 与 ffmpeg 同目录的 ffprobe
    cand = Path(ffmpeg()).with_name("ffprobe.exe")
    if cand.is_file():
        return str(cand)
    raise RuntimeError("找不到 ffprobe")


def run(cmd: list[str], timeout: int = 7200) -> None:
    """跑 ffmpeg 家族命令，失败带 stderr 尾部抛错。"""
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError("命令失败：" + " | ".join(tail))


def duration(path: Path) -> float:
    """媒体总时长（秒），探测失败返回 0。"""
    try:
        out = subprocess.run(
            [ffprobe(), "-v", "error", "-print_format", "json",
             "-show_entries", "format=duration", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001 - 时长只用于进度估算，失败不致命
        return 0.0


def _parse_rate(rate: str) -> float:
    """ffprobe 的 "30000/1001" 帧率 → 浮点；非法返回 0。"""
    try:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    except (ValueError, AttributeError):
        return 0.0


def probe(path: Path) -> dict:
    """流信息 {width, height, duration, has_audio, fps}；探测失败字段给 0/False。"""
    info = {"width": 0, "height": 0, "duration": 0.0, "has_audio": False, "fps": 0.0}
    try:
        out = subprocess.run(
            [ffprobe(), "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        j = json.loads(out.stdout)
    except Exception:  # noqa: BLE001 - 探测失败不致命，字段留默认
        return info
    streams = j.get("streams") or []
    vs = next((s for s in streams if s.get("codec_type") == "video"), {})
    info["width"] = int(vs.get("width") or 0)
    info["height"] = int(vs.get("height") or 0)
    info["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
    info["fps"] = _parse_rate(str(vs.get("avg_frame_rate") or "0/0"))
    try:
        info["duration"] = float((j.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    return info


def run_with_progress(cmd: list[str], total_sec: float, cb=None,
                      timeout: int = 7200) -> None:
    """Popen 版 run：-progress pipe:1 按 out_time_us 回调真进度（0..1）。

    stderr 必须起守护线程持续抽干——ffmpeg 警告输出量大时 PIPE 会塞满
    导致父子互等死锁；-loglevel error 同时把 stderr 量降到最小。
    total_sec<=0 时退化为只等结束（调用方自己给粗进度）。
    """
    full = [cmd[0], "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            *cmd[1:]]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    errbuf: list[str] = []

    def _drain() -> None:
        try:
            errbuf.extend(proc.stderr.read().splitlines())  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

    threading.Thread(target=_drain, daemon=True).start()
    # 看门狗覆盖整个生命周期：stdout 迭代本身可能因 ffmpeg 卡死（坏输入、
    # 滤镜图活锁）永久阻塞，只在 wait 上加 timeout 守不住流式阶段。
    finished = threading.Event()
    killed = threading.Event()

    def _watchdog() -> None:
        if not finished.wait(timeout) and proc.poll() is None:
            killed.set()
            proc.kill()

    threading.Thread(target=_watchdog, daemon=True).start()
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us=") and cb and total_sec > 0:
            try:
                done = int(line.split("=", 1)[1].strip() or 0) / 1e6
            except ValueError:
                continue
            cb(min(done / total_sec, 1.0))
    rc = proc.wait()
    finished.set()
    if killed.is_set():
        raise RuntimeError(f"命令超时（{timeout}s）")
    if rc != 0:
        raise RuntimeError("命令失败：" + " | ".join(errbuf[-4:]))


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXT


def extract_audio(src: Path, dst: Path, fmt: str = "mp3") -> None:
    """视频/音频 → 音频文件。mp3 用 libmp3lame，m4a 用 aac。"""
    if fmt == "m4a":
        codec = ["-vn", "-acodec", "aac", "-b:a", "192k"]
    else:
        codec = ["-vn", "-acodec", "libmp3lame", "-q:a", "3"]
    run([ffmpeg(), "-y", "-i", str(src), *codec, str(dst)])


def to_wav16k(src: Path, dst: Path) -> None:
    """转 STT 友好格式：单声道 16kHz wav（whisper 对长文件更稳）。"""
    run([ffmpeg(), "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)])
