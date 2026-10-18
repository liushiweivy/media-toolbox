# -*- coding: utf-8 -*-
"""图片编辑引擎（Pillow）：一次任务 = 一个有序 ops 数组，按序应用。

坐标一律自然像素（与前端 canvas 的换算由前端负责，两边都以
EXIF 转正后的方向为基准——浏览器预览自动转正，后端 exif_transpose
第一个做，坐标系天然一致）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

_FONT = r"C:\Windows\Fonts\msyh.ttc"

OP_TYPES = {"crop", "resize", "rotate", "flip", "mosaic", "pen", "text", "format"}
MAX_OPS, MAX_POINTS, MAX_TEXT = 50, 5000, 200
FORMATS = ("jpg", "webp", "png")
_IMG_EXT_OUT = {"jpg": ("JPEG", ".jpg"), "webp": ("WEBP", ".webp"),
                "png": ("PNG", ".png")}
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

_ANCHORS = ("tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br")


def _fail(msg: str) -> None:
    raise ValueError(msg)


def _num(v, lo: float, hi: float, name: str) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        _fail(f"{name} 必须是数字")
    if not lo <= v <= hi:
        _fail(f"{name} 需在 {lo}–{hi} 之间")
    return float(v)


def _color(v, name: str) -> str:
    if not isinstance(v, str) or not _COLOR.match(v):
        _fail(f"{name} 须为 #rrggbb 形式")
    return v


def _validate_rect(op: dict, what: str) -> None:
    for k in ("x", "y"):
        _num(op.get(k), -1e6, 1e6, f"{what}.{k}")
    for k in ("w", "h"):
        _num(op.get(k), 1, 1e6, f"{what}.{k}")


def validate_ops(raw: str) -> list[dict]:
    """路由层调用：JSON → 类型白名单 + 数值钳制。坏输入抛 ValueError（→400）。"""
    try:
        # parse_constant：Python json 默认放行 NaN/Infinity，pen 坐标混进
        # 非有限值会拖到 Pillow 才炸成看不懂的 OverflowError
        ops = json.loads(raw, parse_constant=lambda c: _fail("ops 含非法数值"))
    except (json.JSONDecodeError, TypeError):
        _fail("ops 不是合法 JSON")
    if not isinstance(ops, list) or not ops:
        _fail("ops 需为非空数组")
    if len(ops) > MAX_OPS:
        _fail(f"操作数超过 {MAX_OPS} 上限")
    for op in ops:
        if not isinstance(op, dict):
            _fail("每个操作必须是对象")
        t = op.get("type")
        if t not in OP_TYPES:
            _fail(f"未知操作类型：{t}")
        if t == "crop" or t == "mosaic":
            _validate_rect(op, t)
            if t == "mosaic":
                _num(op.get("block", 12), 2, 100, "mosaic.block")
        elif t == "resize":
            if "scale" in op:
                _num(op["scale"], 0.01, 10, "resize.scale")
            else:
                _num(op.get("w", 0), 0, 20000, "resize.w")
                _num(op.get("h", 0), 0, 20000, "resize.h")
                if not op.get("w") and not op.get("h"):
                    _fail("resize 需给 scale 或 w/h 之一")
        elif t == "rotate":
            if op.get("deg") not in (90, 180, 270):
                _fail("rotate.deg 只支持 90/180/270")
        elif t == "flip":
            if op.get("mode") not in ("h", "v"):
                _fail("flip.mode 只支持 h/v")
        elif t == "pen":
            _color(op.get("color", "#ff3344"), "pen.color")
            _num(op.get("width", 6), 1, 100, "pen.width")
            pts = op.get("points")
            if (not isinstance(pts, list) or len(pts) < 2
                    or len(pts) > MAX_POINTS):
                _fail(f"pen.points 需 2–{MAX_POINTS} 个点")
            for p in pts:
                if (not isinstance(p, (list, tuple)) or len(p) != 2
                        or not all(isinstance(c, (int, float))
                                   and not isinstance(c, bool) for c in p)):
                    _fail("pen.points 每个点须为 [x, y] 数字对")
        elif t == "text":
            text = op.get("text")
            if not isinstance(text, str):
                _fail("text.text 必须是字符串")
            text = "".join(c for c in text if c >= " " or c == "\n").strip()
            if not 1 <= len(text) <= MAX_TEXT:
                _fail(f"文字需 1–{MAX_TEXT} 字")
            op["text"] = text
            _num(op.get("size", 32), 4, 500, "text.size")
            _color(op.get("color", "#ffffff"), "text.color")
            _color(op.get("outline_color", "#000000"), "text.outline_color")
            _num(op.get("outline", 2), 0, 20, "text.outline")
            if "anchor" in op:
                if op["anchor"] not in _ANCHORS:
                    _fail(f"text.anchor 只支持 {'/'.join(_ANCHORS)}")
            else:
                _num(op.get("x", 0), -1e6, 1e6, "text.x")
                _num(op.get("y", 0), -1e6, 1e6, "text.y")
        elif t == "format":
            if op.get("fmt") not in FORMATS:
                _fail(f"format.fmt 只支持 {'/'.join(FORMATS)}")
            _num(op.get("quality", 85), 1, 100, "format.quality")
    return ops


def _clamp_rect(op: dict, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """矩形钳制到图内（前端换算可能有 ±1px 误差，不报错）。"""
    w, h = size
    x1 = int(max(0, min(round(op["x"]), w - 1)))
    y1 = int(max(0, min(round(op["y"]), h - 1)))
    x2 = int(max(x1 + 1, min(round(op["x"] + op["w"]), w)))
    y2 = int(max(y1 + 1, min(round(op["y"] + op["h"]), h)))
    return x1, y1, x2, y2


def _mosaic(img: Image.Image, op: dict) -> None:
    """原地打码：区域 crop → BILINEAR 缩到 1/block → NEAREST 放大 → 贴回。"""
    x1, y1, x2, y2 = _clamp_rect(op, img.size)
    block = int(op.get("block", 12))
    region = img.crop((x1, y1, x2, y2))
    small = region.resize((max(1, (x2 - x1) // block),
                           max(1, (y2 - y1) // block)), Image.BILINEAR)
    img.paste(small.resize((x2 - x1, y2 - y1), Image.NEAREST), (x1, y1))


def _text_xy(op: dict, img: Image.Image, font) -> tuple[float, float]:
    """anchor 9 宫格或显式 x,y → 文字落点。"""
    if "anchor" not in op:
        return float(op.get("x", 0)), float(op.get("y", 0))
    probe = ImageDraw.Draw(img)
    box = probe.textbbox((0, 0), op["text"], font=font,
                         stroke_width=int(op.get("outline", 2)))
    tw, th = box[2] - box[0], box[3] - box[1]
    m = max(8, img.width // 50)
    col, row = op["anchor"][1], op["anchor"][0]
    x = {"l": m, "c": (img.width - tw) / 2, "r": img.width - tw - m}[col]
    y = {"t": m, "m": (img.height - th) / 2, "b": img.height - th - m}[row]
    return x, y


def apply(src: Path, dst: Path, ops: list[dict]) -> dict:
    """按序应用 ops，按 format 落盘；返回 {width, height, fmt}。"""
    try:
        img = Image.open(src)
        img = ImageOps.exif_transpose(img)  # 必须第一个做：对齐浏览器预览
        img = img.convert("RGB")
    except Image.DecompressionBombError:
        _fail("图片像素量过大")
    except Exception:  # noqa: BLE001 - 统一成用户可读 400 文案
        _fail("图片无法解析")
    fmt, quality = "jpg", 85
    for op in ops:
        t = op["type"]
        if t == "crop":
            img = img.crop(_clamp_rect(op, img.size))
        elif t == "resize":
            if "scale" in op:
                w = max(1, round(img.width * op["scale"]))
                h = max(1, round(img.height * op["scale"]))
            else:
                w, h = int(op.get("w") or 0), int(op.get("h") or 0)
                if not w:
                    w = max(1, round(img.width * h / img.height))
                if not h:
                    h = max(1, round(img.height * w / img.width))
            # MAX_IMAGE_PIXELS 只守 Image.open；放大攻击（如 scale=10 合法图）
            # 能申请数十 GB 内存，落盘前先挡
            if w * h > (Image.MAX_IMAGE_PIXELS or int(9e7)):
                _fail("缩放后像素量过大")
            img = img.resize((w, h), Image.LANCZOS)
        elif t == "rotate":
            # PIL 的 ROTATE_* 是逆时针角度，界面语义为顺时针
            img = img.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180,
                                 270: Image.ROTATE_90}[op["deg"]])
        elif t == "flip":
            img = img.transpose(Image.FLIP_LEFT_RIGHT if op["mode"] == "h"
                                else Image.FLIP_TOP_BOTTOM)
        elif t == "mosaic":
            _mosaic(img, op)
        elif t == "pen":
            draw = ImageDraw.Draw(img)
            pts = [(float(x), float(y)) for x, y in op["points"]]
            draw.line(pts, fill=op["color"], width=int(op["width"]),
                      joint="curve")
        elif t == "text":
            font = ImageFont.truetype(_FONT, int(op["size"]))
            x, y = _text_xy(op, img, font)
            ImageDraw.Draw(img).text(
                (x, y), op["text"], font=font, fill=op["color"],
                stroke_width=int(op.get("outline", 2)),
                stroke_fill=op.get("outline_color", "#000000"))
        elif t == "format":
            fmt, quality = op["fmt"], int(op.get("quality", 85))
    pil_fmt, _ = _IMG_EXT_OUT[fmt]
    save_kw = {"quality": quality}
    if fmt != "webp":
        save_kw["optimize"] = True
    img.save(dst, pil_fmt, **save_kw)
    return {"width": img.width, "height": img.height, "fmt": fmt}
