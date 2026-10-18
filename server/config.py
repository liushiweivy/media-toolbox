# -*- coding: utf-8 -*-
"""全局配置：路径、限额、环境变量开关。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UP_DIR = DATA_DIR / "up"      # 上传原始文件
OUT_DIR = DATA_DIR / "out"    # 产出文件（按 jobid 前缀）
STATIC_DIR = BASE_DIR / "static"

for d in (UP_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = int(os.environ.get("MT_MAX_UPLOAD_MB", "512")) * 1024 * 1024
JOB_TTL_SEC = 6 * 3600                 # 任务记录与产出文件保留时长
MAX_WORKERS = 2                        # 重活并发上限（STT/OCR/转换），多余排队

# 方舟翻译（Coding Plan 端点）：key 只从环境变量或 data/ark.key 本地文件读，不入源码
ARK_BASE = os.environ.get("MT_ARK_BASE", "https://ark.cn-beijing.volces.com")
ARK_MODEL = os.environ.get("MT_ARK_MODEL", "ark-code-latest")


def _load_ark_key() -> str:
    key = os.environ.get("MT_ARK_KEY", "").strip()
    if key:
        return key
    key_file = DATA_DIR / "ark.key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    return ""  # 翻译类任务会在调用时报错提示


ARK_KEY = _load_ark_key()

# Edge headless（docx→pdf 打印通道）
EDGE_EXE = os.environ.get(
    "MT_EDGE_EXE",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

# whisper 模型（测试用 MT_STT_MODEL=small 提速；运行默认 turbo，质量接近 large-v3 但快 8 倍）
STT_MODEL = os.environ.get("MT_STT_MODEL", "large-v3-turbo")

HOST = os.environ.get("MT_HOST", "127.0.0.1")  # 要开局域网访问显式 MT_HOST=0.0.0.0
PORT = int(os.environ.get("MT_PORT", "8840"))
