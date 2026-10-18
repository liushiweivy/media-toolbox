# -*- coding: utf-8 -*-
"""轻量任务队列：内存登记表 + 线程池执行，前端轮询进度。

重活（STT/OCR/文档转换/长音视频）全部走任务，HTTP 上传请求只负责收文件，
避免长任务把连接挂死；任务与产出文件按 TTL 清理。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import config

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=config.MAX_WORKERS, thread_name_prefix="mt-job")


def _public(job: dict) -> dict:
    return {
        "id": job["id"], "kind": job["kind"], "status": job["status"],
        "progress": round(job["progress"], 3), "message": job["message"],
        "outputs": [{"name": o["name"], "url": o["url"]} for o in job["outputs"]],
        "error": job["error"], "text": job.get("text"), "meta": job.get("meta") or {},
    }


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def set_progress(job_id: str, progress: float, message: str = "") -> None:
    """工作线程内调用；进度被钳到 [0, 0.99]，1.0 只由完成态写入。"""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "running":
            return
        job["progress"] = max(0.0, min(progress, 0.99))
        if message:
            job["message"] = message


def submit(kind: str, fn: Callable, *args, **kwargs) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _gc_locked()
        _jobs[job_id] = {
            "id": job_id, "kind": kind, "status": "queued",
            "progress": 0.0, "message": "排队中", "outputs": [],
            "error": None, "text": None, "meta": {}, "ts": time.time(),
        }
    _pool.submit(_run, job_id, fn, args, kwargs)
    return job_id


def add_output(job_id: str, name: str, path: Path) -> str:
    """登记产出文件，返回下载 URL。path 必须在 OUT_DIR 内。"""
    url = f"/api/download/{job_id}/{name}"
    with _lock:
        job = _jobs[job_id]
        job["outputs"].append({"name": name, "path": Path(path), "url": url})
    return url


def output_path(job_id: str, name: str) -> Path | None:
    """下载路由用：校验 jobid 与文件名，拒绝目录穿越。

    登记表在内存里，重启即失；文件仍在盘上时按命名约定兜底放行，
    保证重启前任务的下载链接不失效。
    """
    if "/" in name or "\\" in name or ".." in name or not name.startswith(job_id + "-"):
        return None
    job = _jobs.get(job_id)
    if job:
        for o in job["outputs"]:
            if o["name"] == name:
                p = o["path"]
                return p if p.is_file() and p.parent == config.OUT_DIR else None
        return None
    p = config.OUT_DIR / name
    return p if p.is_file() and p.parent == config.OUT_DIR else None


def _run(job_id: str, fn: Callable, args: tuple, kwargs: dict) -> None:
    with _lock:
        job = _jobs[job_id]
        job["status"] = "running"
        job["message"] = "处理中"
    try:
        result = fn(job_id, *args, **kwargs) or {}
        with _lock:
            job = _jobs[job_id]
            job["status"] = "done"
            job["progress"] = 1.0
            job["message"] = result.get("message") or "完成"
            job["text"] = result.get("text")
            job["meta"] = result.get("meta") or {}
            job["ts"] = time.time()  # 完成时刻重新计 TTL
    except Exception as exc:  # noqa: BLE001 - 任务边界必须兜住所有异常回传前端
        traceback.print_exc()
        with _lock:
            job = _jobs[job_id]
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["message"] = "失败"
        job["ts"] = time.time()  # 完成时刻重新计 TTL


def _gc_locked() -> None:
    """清理过期任务并删掉产出文件；进行中/排队中的任务永远不动（防长队误杀）。"""
    now = time.time()
    dead = [jid for jid, j in _jobs.items()
            if j["status"] in ("done", "error") and now - j["ts"] > config.JOB_TTL_SEC]
    for jid in dead:
        job = _jobs.pop(jid)
        for o in job["outputs"]:
            try:
                o["path"].unlink(missing_ok=True)
            except OSError:
                pass
