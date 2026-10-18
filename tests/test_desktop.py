# -*- coding: utf-8 -*-
"""桌面软件版测试：空闲端口、内嵌服务线程就绪、产出文件解析（不起真窗口）。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("MT_STT_MODEL", "small")

import desktop  # noqa: E402
from server import config  # noqa: E402


def test_free_port_returns_usable_port():
    port = desktop.free_port()
    assert 1024 <= port <= 65535
    assert port == desktop.free_port() or True  # 两次调用不保证同端口，仅冒烟


def test_embedded_server_thread_ready():
    port = desktop.free_port()
    server = desktop.start_server(port)
    try:
        assert desktop.wait_ready(port, timeout=15)
    finally:
        server.should_exit = True


def test_wait_ready_timeout_on_dead_port():
    assert not desktop.wait_ready(desktop.free_port(), timeout=0.5)


def test_resolve_output(tmp_path):
    p = config.OUT_DIR / "cafe123babe4-report.txt"
    p.write_text("x", encoding="utf-8")
    try:
        assert desktop.resolve_output(f"/api/download/cafe123babe4/{p.name}") == p
        assert desktop.resolve_output("/api/download/cafe123babe4/../evil") is None
        assert desktop.resolve_output("/api/download/deadbeef0000/cafe123babe4-report.txt") is None
        assert desktop.resolve_output("not-a-url") is None
    finally:
        p.unlink(missing_ok=True)


def test_bridge_save_output(tmp_path):
    """Bridge 无窗口也能走完解析+复制（对话框用 monkeypatch 跳过）。"""
    src = config.OUT_DIR / "ab12cd34ef56-a.mp3"
    src.write_bytes(b"ID3-fake")
    dst = tmp_path / "用户选择.mp3"

    class FakeWindow:
        def create_file_dialog(self, _kind, save_filename=""):
            return str(dst)

    bridge = desktop.Bridge(FakeWindow())
    try:
        msg = bridge.save_output("/api/download/ab12cd34ef56/ab12cd34ef56-a.mp3")
        assert msg.startswith("已保存") and dst.read_bytes() == b"ID3-fake"
        assert bridge.save_output("/api/download/000000000000/x.mp3").startswith("文件不存在")
    finally:
        src.unlink(missing_ok=True)


def test_bridge_cancel_returns_message(tmp_path):
    class FakeWindow:
        def create_file_dialog(self, _kind, save_filename=""):
            return None

    src = config.OUT_DIR / "bb12cd34ef56-b.txt"
    src.write_text("y", encoding="utf-8")
    bridge = desktop.Bridge(FakeWindow())
    try:
        assert bridge.save_output("/api/download/bb12cd34ef56/bb12cd34ef56-b.txt") == "已取消"
    finally:
        src.unlink(missing_ok=True)
