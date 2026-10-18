# -*- coding: utf-8 -*-
"""YouTube 视频转文字：yt-dlp 抓字幕（人工优先，自动兜底），VTT 解析去重。

本机访问 YouTube 需走代理（默认 127.0.0.1:7897，MT_YT_PROXY 覆盖）；
yt-dlp 用独立 exe（MT_YTDLP 覆盖），不依赖系统 python。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import config

YTDLP = os.environ.get("MT_YTDLP", str(config.BASE_DIR / "bin" / "yt-dlp.exe"))
YT_PROXY = os.environ.get("MT_YT_PROXY", "http://127.0.0.1:7897")
TIMEOUT = 120

_ID_RE = re.compile(r"^[\w-]{6,20}$")
_LANG_PREF = ["zh-Hans", "zh-Hant", "zh-CN", "zh", "en"]


def video_id_of(url: str) -> str | None:
    """从 watch / youtu.be / shorts / live / embed 链接提取视频 id；不合法返回 None。"""
    from urllib.parse import urlparse, parse_qs
    try:
        u = urlparse(str(url).strip())
    except ValueError:
        return None
    host = u.netloc.lower()
    for pre in ("www.", "m."):
        if host.startswith(pre):
            host = host[len(pre):]
    vid = None
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(shorts|embed|live|v)/([\w-]+)", u.path)
            if m:
                vid = m.group(2)
    return vid if vid and _ID_RE.match(vid) else None


def _run(args: list[str]) -> str:
    """调 yt-dlp（数组参数无 shell），把常见错误翻成人话。"""
    cmd = [YTDLP, "--proxy", YT_PROXY, "--no-playlist", "--no-warnings", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"网络超时：请确认代理 {YT_PROXY} 可用") from None
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        if re.search(r"Sign in to confirm|bot", msg, re.I):
            raise RuntimeError("YouTube 触发人机验证，请稍后重试或更换代理节点")
        if re.search(r"Video unavailable|Private video", msg, re.I):
            raise RuntimeError("视频不可用（不存在、已删除或为私密视频）")
        raise RuntimeError(msg[-400:] or "yt-dlp 执行失败")
    return proc.stdout


def pick_track(subs: dict, autos: dict, want: str = "") -> dict | None:
    """选字幕轨：指定语言 > 简中 > 繁中 > 英文；人工优先于自动。"""
    pref = [want] if want else _LANG_PREF
    for table, auto in ((subs, False), (autos, True)):
        for lang in pref:
            if lang in table:
                return {"lang": lang, "auto": auto}
            hit = next((k for k in table if k == lang or k.startswith(lang + "-")), None)
            if hit:
                return {"lang": hit, "auto": auto}
    for table, auto in ((subs, False), (autos, True)):
        if table:
            return {"lang": next(iter(table)), "auto": auto}
    return None


def _to_sec(t: str) -> float:
    s = 0.0
    for p in t.split(":"):
        s = s * 60 + float(p)
    return round(s, 3)


def parse_vtt(raw: str) -> list[dict]:
    """WebVTT → [{start,end,text}]；自动字幕的滚动重复行去重合并。"""
    cues = []
    for block in raw.replace("\r", "").lstrip("﻿").split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if ti == -1:
            continue
        m = re.search(r"([\d:.]+)\s*-->\s*([\d:.]+)", lines[ti])
        if not m:
            continue
        text = re.sub(r"<[^>]+>", "", " ".join(lines[ti + 1:]))
        for a, b in (("&amp;", "&"), ("&#39;", "'"), ("&apos;", "'"),
                     ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
            text = text.replace(a, b)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append({"start": _to_sec(m.group(1)), "end": _to_sec(m.group(2)), "text": text})
    out: list[dict] = []
    for c in cues:
        p = out[-1] if out else None
        if p and (p["text"] == c["text"] or c["text"] in p["text"]):
            p["end"] = max(p["end"], c["end"])
            continue
        if p and c["text"].startswith(p["text"]):  # 滚动窗：本条包含上条 → 扩展上条
            p["text"], p["end"] = c["text"], c["end"]
            continue
        out.append(c)
    return out


def fetch_transcript(video_id: str, want_lang: str = "") -> dict:
    """拉元数据 → 选轨 → 下载 VTT → 解析，返回完整结果。"""
    meta = json.loads(_run(["-J", "--skip-download",
                            f"https://www.youtube.com/watch?v={video_id}"]))
    track = pick_track(meta.get("subtitles") or {},
                       meta.get("automatic_captions") or {}, want_lang)
    if not track:
        raise RuntimeError("该视频没有任何可用字幕（也未开启自动字幕）")
    with tempfile.TemporaryDirectory(prefix="mt-yt-") as tmp:
        _run(["--skip-download",
              "--write-auto-subs" if track["auto"] else "--write-subs",
              "--sub-langs", track["lang"], "--sub-format", "vtt",
              "-o", str(Path(tmp) / "%(id)s.%(ext)s"),
              f"https://www.youtube.com/watch?v={video_id}"])
        vtts = list(Path(tmp).glob(f"{video_id}.*.vtt"))
        if not vtts:
            raise RuntimeError("字幕下载失败")
        cues = parse_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))
    if not cues:
        raise RuntimeError("字幕文件为空或解析失败")
    return {
        "id": video_id,
        "title": meta.get("title") or video_id,
        "channel": meta.get("channel") or meta.get("uploader") or "",
        "duration": meta.get("duration") or 0,
        "lang": track["lang"], "auto": track["auto"], "cues": cues,
    }


# ---------- 文稿格式化 ----------

def fmt_mmss(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def to_plain(cues: list[dict]) -> str:
    return "\n".join(c["text"] for c in cues)


def to_timeline_md(cues: list[dict], title: str = "") -> str:
    lines = [f"# {title}", "", "| 时间 | 内容 |", "| --- | --- |"] if title else []
    lines += [f"| {fmt_mmss(c['start'])} | {c['text']} |" for c in cues]
    return "\n".join(lines)


def to_srt(cues: list[dict]) -> str:
    def ts(sec: float) -> str:
        ms = round((sec % 1) * 1000)
        s = int(sec)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},{ms:03d}"
    return "\n\n".join(
        f"{i}\n{ts(c['start'])} --> {ts(c['end'])}\n{c['text']}"
        for i, c in enumerate(cues, 1))
