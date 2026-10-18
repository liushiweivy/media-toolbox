# -*- coding: utf-8 -*-
"""图片文字识别：RapidOCR（ONNX 本地推理，中文模型随包内置，CPU 可用）。"""
from __future__ import annotations

import threading
from pathlib import Path

_engine = None
_lock = threading.Lock()

MAX_SIDE = 4096  # 超大图先压边长，省内存且提速


def get_engine():
    global _engine
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        return _engine


def recognize(path: Path) -> dict:
    """返回 {text, lines[{text, score}]}。"""
    import cv2

    img = cv2.imread(str(path))
    if img is None:
        raise ValueError("图片无法解码（支持 jpg/png/bmp/webp）")
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        scale = MAX_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    result, _elapse = get_engine()(img)
    lines = [{"text": t, "score": round(float(s), 3)} for _box, t, s in (result or [])]
    return {"text": "\n".join(l["text"] for l in lines), "lines": lines}
