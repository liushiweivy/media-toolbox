# -*- coding: utf-8 -*-
"""视频剪辑引擎：裁剪/压缩转码/合并/画面处理/封面帧/文字水印。

全部 argv 列表调用（无 shell）；重编码且已知时长的走 run_with_progress
报真进度，快操作（封面）沿用粗粒度。所有重编码链末尾统一偶数化
（x264 + yuv420p 要求偶数边长）。
"""
from __future__ import annotations

from pathlib import Path

from . import media

_FONT = r"C:\Windows\Fonts\msyh.ttc"

# 水印 9 宫格 → drawtext x/y 表达式（边距 24px）
_POS = {
    "tl": ("24", "24"), "tc": ("(w-tw)/2", "24"), "tr": ("w-tw-24", "24"),
    "ml": ("24", "(h-th)/2"), "mc": ("(w-tw)/2", "(h-th)/2"),
    "mr": ("w-tw-24", "(h-th)/2"),
    "bl": ("24", "h-th-24"), "bc": ("(w-tw)/2", "h-th-24"),
    "br": ("w-tw-24", "h-th-24"),
}

ROTATES = (0, 90, 180, 270)
FLIPS = ("none", "h", "v")
COMPRESS_HEIGHTS = (0, 1080, 720, 480, 360)
OUT_FMTS = ("mp4", "gif")


def _even(vf: str) -> str:
    """滤镜链末尾挂偶数化，防奇数分辨率炸 yuv420p。"""
    tail = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    return f"{vf},{tail}" if vf else tail


def _x264(crf: int = 23, audio: bool = True, abitrate: str = "160k") -> list[str]:
    out = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-pix_fmt", "yuv420p"]
    return out + (["-c:a", "aac", "-b:a", abitrate] if audio else ["-an"])


def _ff_path(p: str) -> str:
    """滤镜参数字符串里的 Windows 路径：正斜杠 + 盘符冒号转义。"""
    return p.replace("\\", "/").replace(":", r"\:")


def trim(src: Path, dst: Path, start: float, end: float, cb=None) -> None:
    """[start, end] 秒区间重编码裁剪。-ss 在 -i 前（输入定位快），
    ffmpeg 7 重编码时从最近关键帧解码并丢弃，帧精确；-t 用差值，
    避免输入 seek 重置时间戳后 -to 的歧义。"""
    dur = end - start
    media.run_with_progress(
        [media.ffmpeg(), "-y", "-ss", f"{start:.3f}", "-i", str(src),
         "-t", f"{dur:.3f}", "-vf", _even(""), *_x264(20), str(dst)],
        dur, cb)


def compress(src: Path, dst: Path, height: int, crf: int,
             out_fmt: str = "mp4", cb=None) -> None:
    """压缩转码：MP4 按目标高度+CRF；GIF 单命令调色板方案（不产临时文件）。"""
    total = media.duration(src)
    if out_fmt == "gif":
        fc = ("[0:v]fps=12,scale=480:-1:flags=lanczos,split[a][b];"
              "[a]palettegen[p];[b][p]paletteuse")
        media.run_with_progress(
            [media.ffmpeg(), "-y", "-i", str(src), "-filter_complex", fc,
             str(dst)], total, cb)
        return
    vf = f"scale=-2:{height}" if height else ""
    media.run_with_progress(
        [media.ffmpeg(), "-y", "-i", str(src), "-vf", _even(vf),
         *_x264(crf, abitrate="128k"), str(dst)], total, cb)


def transform(src: Path, dst: Path, rotate: int, flip: str,
              mute: bool, cb=None) -> None:
    """画面处理：旋转 90/180/270（顺时针）、水平/垂直翻转、可选去音轨。"""
    chain: list[str] = []
    if rotate == 90:
        chain.append("transpose=1")
    elif rotate == 180:
        chain += ["transpose=1", "transpose=1"]
    elif rotate == 270:
        chain.append("transpose=2")
    if flip == "h":
        chain.append("hflip")
    elif flip == "v":
        chain.append("vflip")
    info = media.probe(src)
    media.run_with_progress(
        [media.ffmpeg(), "-y", "-i", str(src), "-vf", _even(",".join(chain)),
         *_x264(20, audio=not mute and info["has_audio"]), str(dst)],
        info["duration"], cb)


def cover(src: Path, dst: Path, t: float) -> None:
    """提取 t 秒处封面帧为 jpg。t 钳到 duration-0.1 防越界空帧。"""
    dur = media.duration(src)
    if dur > 0:
        t = max(0.0, min(t, dur - 0.1))
    media.run([media.ffmpeg(), "-y", "-ss", f"{t:.3f}", "-i", str(src),
               "-frames:v", "1", "-q:v", "2", str(dst)])


def watermark(src: Path, dst: Path, text: str, pos: str, opacity: float,
              size: int, txtfile: Path, cb=None) -> None:
    """文字水印。中文安全的关键：用 textfile= 不用 text=——用户文本落盘
    UTF-8（无 BOM）临时文件，滤镜参数里只剩路径转义这一种固定模式，
    彻底绕开 drawtext 对 :'\\%,[]; 与中文的多层转义地狱。"""
    clean = " ".join(text.splitlines())[:100] or "水印"
    txtfile.write_text(clean, encoding="utf-8")
    x, y = _POS[pos]
    vf = _even(
        f"drawtext=fontfile='{_ff_path(_FONT)}'"
        f":textfile='{_ff_path(str(txtfile))}'"
        # expansion=none：用户文本里的 %{...} 不当滤镜变量展开（默认 normal 会
        # 把 "%{n}" 之类替换掉或直接解析失败毁掉整个任务）
        f":expansion=none:fontsize={size}:fontcolor=white@{opacity:.2f}"
        f":borderw=2:bordercolor=black@0.6:x={x}:y={y}")
    try:
        media.run_with_progress(
            [media.ffmpeg(), "-y", "-i", str(src), "-vf", vf,
             *_x264(20), str(dst)], media.duration(src), cb)
    finally:
        txtfile.unlink(missing_ok=True)


def merge(srcs: list[Path], dst: Path, cb=None) -> None:
    """多段合并：concat filter 重编码（demuxer 要求同编码同参数，混合输入
    必碎）。以第一段分辨率为画布统一 scale+pad；无声轨输入用 lavfi
    anullsrc 合成输入补齐。"""
    infos = [media.probe(p) for p in srcs]
    tw = max((infos[0]["width"] or 1280) // 2 * 2, 2)
    th = max((infos[0]["height"] or 720) // 2 * 2, 2)
    args: list[str] = [media.ffmpeg(), "-y"]
    parts: list[str] = []
    concat_in = ""
    # lavfi 合成输入也占 -i 序号，必须按参数流顺序自增计数
    input_idx = 0
    for i, (p, info) in enumerate(zip(srcs, infos)):
        args += ["-i", str(p)]
        v_idx = input_idx
        input_idx += 1
        parts.append(
            f"[{v_idx}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
        if info["has_audio"]:
            a_idx = v_idx
        else:
            args += ["-f", "lavfi", "-t", f"{max(info['duration'], 0.1):.3f}",
                     "-i", "anullsrc=r=44100:cl=stereo"]
            a_idx = input_idx
            input_idx += 1
        parts.append(f"[{a_idx}:a]aformat=sample_rates=44100:"
                     f"channel_layouts=stereo[a{i}]")
        concat_in += f"[v{i}][a{i}]"
    fc = (";".join(parts)
          + f";{concat_in}concat=n={len(srcs)}:v=1:a=1[v][a]")
    total = sum(i["duration"] for i in infos)
    media.run_with_progress(
        args + ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                *_x264(23), str(dst)], total, cb)
