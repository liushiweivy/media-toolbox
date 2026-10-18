# -*- coding: utf-8 -*-
"""视频剪辑 + 图片编辑流水线测试：真实文件走通各 op + 边界守卫。

运行：.venv/Scripts/python.exe -m pytest tests/test_edit.py -v
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import media

client = TestClient(app_mod.app)
DATA = Path(__file__).parent / "fixtures"


def _wait(job_id: str, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/job/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"任务 {job_id} 超时未完成")


@pytest.fixture(scope="session", autouse=True)
def fixtures() -> None:
    """会话级造样：无声轨 mp4（验 anullsrc 补音轨分支）。"""
    DATA.mkdir(exist_ok=True)
    media.run([media.ffmpeg(), "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=0x402030:s=320x180:d=2",
               "-c:v", "libx264", "-pix_fmt", "yuv420p",
               str(DATA / "demo2.mp4")])
    from PIL import Image
    img = Image.new("RGB", (640, 480))
    px = img.load()
    for y in range(480):
        for x in range(640):
            px[x, y] = (x * 255 // 640, y * 255 // 480, 128)
    img.save(DATA / "photo.jpg", "JPEG", quality=92)


def _post_video(endpoint: str, data: dict, fname: str = "demo.mp4") -> dict:
    r = client.post(f"/api/video/{endpoint}",
                    files={"file": (fname, (DATA / fname).read_bytes())},
                    data=data)
    assert r.status_code == 200, r.text
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    return job


def _download(job: dict) -> bytes:
    r = client.get(job["outputs"][0]["url"])
    assert r.status_code == 200
    return r.content


def _probe_download(job: dict, tmp_path: Path) -> dict:
    p = tmp_path / job["outputs"][0]["name"]
    p.write_bytes(_download(job))
    return media.probe(p)


# ---------- 视频流水线 ----------

def test_video_trim(tmp_path):
    job = _post_video("trim", {"start": "0.5", "end": "2.0"})
    assert job["outputs"][0]["name"].endswith("-剪辑.mp4")
    info = _probe_download(job, tmp_path)
    assert 1.3 <= info["duration"] <= 1.7
    assert info["width"] == 320 and info["has_audio"]


def test_video_compress_mp4(tmp_path):
    job = _post_video("compress", {"height": "360", "crf": "28", "out": "mp4"})
    info = _probe_download(job, tmp_path)
    # 源只有 180p，-2:360 会放大到 360；关键在走通链路且高度命中
    assert info["height"] == 360
    assert job["meta"]["in_bytes"] > 0 and job["meta"]["out_bytes"] > 0


def test_video_compress_gif(tmp_path):
    job = _post_video("compress", {"height": "0", "crf": "28", "out": "gif"})
    assert job["outputs"][0]["name"].endswith(".gif")
    assert _download(job)[:4] == b"GIF8"


def test_video_transform_rotate(tmp_path):
    job = _post_video("transform", {"rotate": "90", "flip": "none", "mute": "0"})
    info = _probe_download(job, tmp_path)
    assert (info["width"], info["height"]) == (180, 320)  # 宽高互换
    assert info["has_audio"]


def test_video_transform_mute(tmp_path):
    job = _post_video("transform", {"rotate": "0", "flip": "none", "mute": "1"})
    assert _probe_download(job, tmp_path)["has_audio"] is False


def test_video_cover(tmp_path):
    job = _post_video("cover", {"t": "1.0"})
    data = _download(job)
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    from PIL import Image
    assert Image.open(io.BytesIO(data)).size == (320, 180)


def test_video_watermark_chinese(tmp_path):
    job = _post_video("watermark", {"text": "春眠", "pos": "br",
                                    "opacity": "0.8", "size": "24"})
    info = _probe_download(job, tmp_path)
    assert 2.5 <= info["duration"] <= 3.5 and info["width"] == 320


def test_video_merge_mixed_audio(tmp_path):
    """demo.mp4（有音轨）+ demo2.mp4（无音轨，走 anullsrc 补轨）。"""
    r = client.post("/api/video/merge", files=[
        ("files", ("demo.mp4", (DATA / "demo.mp4").read_bytes())),
        ("files", ("demo2.mp4", (DATA / "demo2.mp4").read_bytes())),
    ])
    assert r.status_code == 200, r.text
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    info = _probe_download(job, tmp_path)
    assert 4.5 <= info["duration"] <= 5.5
    assert info["has_audio"]  # 无声段补音轨成功，concat 输出统一带音频


# ---------- 视频守卫 ----------

def test_trim_bad_range():
    r = client.post("/api/video/trim",
                    files={"file": ("demo.mp4", (DATA / "demo.mp4").read_bytes())},
                    data={"start": "2.0", "end": "1.0"})
    assert r.status_code == 400


def test_trim_bad_ext():
    r = client.post("/api/video/trim",
                    files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                    data={"start": "0", "end": "1"})
    assert r.status_code == 400


def test_merge_single_file_rejected():
    r = client.post("/api/video/merge", files=[
        ("files", ("demo.mp4", (DATA / "demo.mp4").read_bytes()))])
    assert r.status_code == 400


def test_watermark_bad_pos():
    r = client.post("/api/video/watermark",
                    files={"file": ("demo.mp4", (DATA / "demo.mp4").read_bytes())},
                    data={"text": "水印", "pos": "xx"})
    assert r.status_code == 400


def test_compress_bad_crf():
    r = client.post("/api/video/compress",
                    files={"file": ("demo.mp4", (DATA / "demo.mp4").read_bytes())},
                    data={"crf": "99"})
    assert r.status_code == 400


# ---------- 图片流水线 ----------

import json as _json

from PIL import Image as _Image


def _post_image(ops: list[dict], fname: str = "ocr.png") -> "_Image.Image":
    r = client.post("/api/image/edit",
                    files={"file": (fname, (DATA / fname).read_bytes())},
                    data={"ops": _json.dumps(ops, ensure_ascii=False)})
    assert r.status_code == 200, r.text
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    return _Image.open(io.BytesIO(_download(job)))


def test_image_crop_and_format():
    im = _post_image([{"type": "crop", "x": 100, "y": 20, "w": 300, "h": 100},
                      {"type": "format", "fmt": "jpg", "quality": 80}])
    assert im.size == (300, 100) and im.format == "JPEG"


def test_image_resize_scale():
    im = _post_image([{"type": "resize", "scale": 0.5},
                      {"type": "format", "fmt": "png"}])
    assert im.size == (400, 80) and im.format == "PNG"


def test_image_rotate_90():
    im = _post_image([{"type": "rotate", "deg": 90},
                      {"type": "format", "fmt": "png"}])
    assert im.size == (160, 800)  # 800x160 → 宽高互换


def test_image_compress_only():
    """空变换仅 format（纯压缩瘦身路径）。"""
    im = _post_image([{"type": "format", "fmt": "webp", "quality": 60}],
                     fname="photo.jpg")
    assert im.size == (640, 480) and im.format == "WEBP"


def test_image_mosaic_blocks():
    im = _post_image([{"type": "mosaic", "x": 100, "y": 100, "w": 120,
                       "h": 120, "block": 12},
                      {"type": "format", "fmt": "png"}], fname="photo.jpg")
    im = im.convert("RGB")
    # 同一块内相邻像素相等（块大小 12，取块内偏移 1 与 8）
    assert im.getpixel((101, 101)) == im.getpixel((108, 101))


def test_image_pen_red_line():
    im = _post_image([{"type": "pen", "color": "#ff2222", "width": 8,
                       "points": [[0, 0], [160, 80], [320, 160]]},
                      {"type": "format", "fmt": "png"}])
    im = im.convert("RGB")
    r, g, b = im.getpixel((160, 80))
    assert r > 200 and g < 80 and b < 80


def test_image_text_bright_pixels():
    im = _post_image([{"type": "text", "text": "水印测试", "x": 10, "y": 10,
                       "size": 40, "color": "#ffffff", "outline": 0},
                      {"type": "format", "fmt": "png"}], fname="photo.jpg")
    im = im.convert("RGB")
    bright = sum(1 for y in range(10, 60) for x in range(10, 300)
                 if sum(im.getpixel((x, y))) > 600)
    assert bright > 50


# ---------- 图片守卫 ----------

def test_image_bad_ext():
    r = client.post("/api/image/edit",
                    files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                    data={"ops": '[{"type":"format","fmt":"jpg"}]'})
    assert r.status_code == 400


def test_image_ops_not_json():
    r = client.post("/api/image/edit",
                    files={"file": ("ocr.png", (DATA / "ocr.png").read_bytes())},
                    data={"ops": "not-json"})
    assert r.status_code == 400


def test_image_ops_unknown_type():
    r = client.post("/api/image/edit",
                    files={"file": ("ocr.png", (DATA / "ocr.png").read_bytes())},
                    data={"ops": '[{"type":"hack"}]'})
    assert r.status_code == 400


def test_image_text_too_long():
    ops = _json.dumps([{"type": "text", "text": "字" * 201, "x": 0, "y": 0},
                       {"type": "format", "fmt": "jpg"}])
    r = client.post("/api/image/edit",
                    files={"file": ("ocr.png", (DATA / "ocr.png").read_bytes())},
                    data={"ops": ops})
    assert r.status_code == 400
