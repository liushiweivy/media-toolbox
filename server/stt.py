# -*- coding: utf-8 -*-
"""语音转文字：faster-whisper 懒加载单例。

- 模型默认 large-v3-turbo（接近 large-v3 质量、约 8 倍速），MT_STT_MODEL 可覆盖；
- 有 CUDA 用 GPU（pip 装的 nvidia-cublas/cudnn 需要先挂上 PATH），失败回落 CPU int8；
- HF 走 hf-mirror.com 镜像，国内网络可下。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# hf-mirror 不代理 Xet CAS（401），强制走普通 HTTP 分片下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from . import config  # noqa: E402

_model = None
_device_info = ""
_lock = threading.Lock()


def _prepend_nvidia_lib_path() -> None:
    """pip 版 cuBLAS/cuDNN 不进系统 PATH，Windows 下 ctranslate2 加载 CUDA 需要。"""
    nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not nvidia.is_dir():
        return
    add = [str(p) for p in (nvidia / "cublas" / "bin", nvidia / "cudnn" / "bin") if p.is_dir()]
    os.environ["PATH"] = os.pathsep.join(add + [os.environ.get("PATH", "")])


def device_info() -> str:
    return _device_info or "未加载"


def get_model():
    global _model, _device_info
    with _lock:
        if _model is not None:
            return _model
        _prepend_nvidia_lib_path()
        from faster_whisper import WhisperModel
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                try:
                    _model = WhisperModel(config.STT_MODEL, device="cuda",
                                          compute_type="float16")
                    _device_info = f"cuda/float16 · {config.STT_MODEL}"
                    return _model
                except Exception:  # noqa: BLE001 - GPU 初始化失败回落 CPU
                    _model = None
        except Exception:  # noqa: BLE001
            pass
        _model = WhisperModel(config.STT_MODEL, device="cpu", compute_type="int8")
        _device_info = f"cpu/int8 · {config.STT_MODEL}"
        return _model


def transcribe(path: Path, language: str | None = None, progress_cb=None) -> dict:
    """识别音频。返回 {text, segments[{start,end,text}], language, duration}。

    progress_cb(done_seconds) 由消费 segment 生成器的循环驱动。
    """
    model = get_model()
    kwargs = {"beam_size": 5, "vad_filter": True}
    if language and language != "auto":
        kwargs["language"] = language
    segments, info = model.transcribe(str(path), **kwargs)
    out = []
    for seg in segments:
        out.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                    "text": seg.text.strip()})
        if progress_cb:
            progress_cb(seg.end)
    text = "".join(s["text"] for s in out) if info.language == "zh" else \
        "\n".join(s["text"] for s in out)
    return {"text": text.strip(), "segments": out,
            "language": info.language, "duration": round(info.duration, 1)}


def fmt_ts(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_markdown(segments: list[dict]) -> str:
    return "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments)
