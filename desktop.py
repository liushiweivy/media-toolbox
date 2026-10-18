# -*- coding: utf-8 -*-
"""桌面软件版：内嵌 uvicorn 服务线程（127.0.0.1 随机空闲端口）+ pywebview 原生窗口。

与网页版共用 server/ 全部后端与 static/ 前端，零改动复用；
窗口内下载走原生「另存为」对话框（Bridge.save_output），不依赖浏览器下载行为。
启动：desktop.bat（pythonw 无控制台）；或直接 python desktop.py。
"""
from __future__ import annotations

import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn


def free_port() -> int:
    """向系统要一个空闲端口，避免与已运行的网页版 (:8840) 冲突。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> uvicorn.Server:
    from server.app import app
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True, name="mt-server").start()
    return srv


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def resolve_output(url: str) -> Path | None:
    """把 /api/download/{jobid}/{name} 解析成盘上的产出文件（含路径校验）。"""
    from server import jobs
    parts = url.split("?")[0].strip("/").split("/")
    if len(parts) < 2:
        return None
    return jobs.output_path(parts[-2], parts[-1])


class Bridge:
    """暴露给前端 window.pywebview.api 的原生能力。"""

    def __init__(self, window) -> None:
        self._window = window

    def save_output(self, url: str, suggested: str = "") -> str:
        """产出文件 → 原生另存为对话框 → 复制到用户选定位置。"""
        src = resolve_output(url)
        if not src:
            return "文件不存在或已过期"
        import webview
        filename = suggested or src.name.split("-", 1)[-1]
        dest = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=filename)
        if not dest:
            return "已取消"
        dest_path = Path(dest if isinstance(dest, str) else dest[0])
        try:
            shutil.copyfile(src, dest_path)
        except OSError as exc:
            return f"保存失败：{exc}"
        return f"已保存到 {dest_path.name}"


def main() -> None:
    port = free_port()
    server = start_server(port)
    if not wait_ready(port):
        raise SystemExit("内嵌服务未能就绪，请查看 data/server.log")
    import webview
    bridge = Bridge(None)
    window = webview.create_window(
        "媒体工具箱", f"http://127.0.0.1:{port}/",
        width=1180, height=860, min_size=(900, 640),
        js_api=bridge)
    bridge._window = window  # 文件对话框需要 window 引用
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
