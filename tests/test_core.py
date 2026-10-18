# -*- coding: utf-8 -*-
"""媒体工具箱端到端测试：真实文件走通 9 条流水线 + 边界守卫。

运行：.venv/Scripts/python.exe -m pytest tests/ -v
STT 测试依赖已缓存的 whisper 模型（MT_STT_MODEL 默认 small，首次会下载）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("MT_STT_MODEL", "small")

from server import app as app_mod  # noqa: E402
from server import jobs, media  # noqa: E402

client = TestClient(app_mod.app)
DATA = Path(__file__).parent / "fixtures"


def _wait(job_id: str, timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/job/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"任务 {job_id} 超时未完成")


@pytest.fixture(scope="session", autouse=True)
def fixtures() -> None:
    """会话级造样：正弦 wav、含音轨 mp4、docx、中文图片。"""
    DATA.mkdir(exist_ok=True)
    media.run([media.ffmpeg(), "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
               str(DATA / "tone.wav")])
    media.run([media.ffmpeg(), "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=0x203040:s=320x180:d=3",
               "-i", str(DATA / "tone.wav"),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-shortest", str(DATA / "demo.mp4")])
    from docx import Document
    doc = Document()
    doc.add_heading("测试文档", level=1)
    doc.add_paragraph("正文段落：中文排版验证。")
    doc.save(str(DATA / "demo.docx"))
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (800, 160), (245, 242, 235))
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 40)
    ImageDraw.Draw(img).text((30, 50), "春眠不觉晓", font=font, fill=(20, 20, 20))
    img.save(str(DATA / "ocr.png"))


# ---------- 单元层 ----------

def test_health():
    h = client.get("/api/health").json()
    assert h["ok"] and len(h["voices"]) >= 8 and "en" in h["targets"]


def test_upload_rejects_bad_ext():
    r = client.post("/api/docx2pdf", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_job_404():
    assert client.get("/api/job/nonexistent12").status_code == 404


def test_download_path_traversal_blocked():
    assert jobs.output_path("abc123def456", "../secret.txt") is None
    assert jobs.output_path("abc123def456", "..\\secret.txt") is None
    assert jobs.output_path("abc123def456", "otherjob-file.txt") is None


def test_download_survives_restart(tmp_path):
    """登记表丢失后按命名约定兜底放行真实产出文件。"""
    p = app_mod.config.OUT_DIR / "deadbeefcafe-x.txt"
    p.write_text("hi", encoding="utf-8")
    try:
        assert jobs.output_path("deadbeefcafe", "deadbeefcafe-x.txt") == p
    finally:
        p.unlink(missing_ok=True)


def test_tts_empty_and_long_text():
    from server import tts
    with pytest.raises(ValueError):
        tts.synthesize("   ", DATA / "x.mp3")
    with pytest.raises(ValueError):
        tts.synthesize("字" * 8001, DATA / "x.mp3")


def test_translate_bad_target():
    from server import translate
    with pytest.raises(ValueError):
        translate.translate("hello", "xx")


def test_translate_chunk_split():
    chunks = translate_mod_chunks("段" * 7000)
    assert len(chunks) >= 3 and all(len(c) <= 3000 for c in chunks)


def translate_mod_chunks(text: str) -> list[str]:
    from server import translate
    return translate._split_chunks(text)


# ---------- 流水线层 ----------

def test_extract_audio():
    r = client.post("/api/extract-audio", files={"file": ("demo.mp4", (DATA / "demo.mp4").read_bytes())},
                    data={"fmt": "mp3"})
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done" and job["outputs"][0]["name"].endswith(".mp3")
    dl = client.get(job["outputs"][0]["url"])
    assert dl.status_code == 200 and len(dl.content) > 5000


def test_ocr():
    r = client.post("/api/ocr", files={"file": ("ocr.png", (DATA / "ocr.png").read_bytes())})
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done"
    assert "春眠不觉晓" in job["text"].replace(" ", "")


def test_docx2pdf_then_pdf2docx():
    r = client.post("/api/docx2pdf", files={"file": ("demo.docx", (DATA / "demo.docx").read_bytes())})
    job = _wait(r.json()["job_id"], timeout=150)
    assert job["status"] == "done"
    pdf = client.get(job["outputs"][0]["url"]).content
    assert pdf[:4] == b"%PDF"
    r2 = client.post("/api/pdf2docx", files={"file": ("demo.pdf", pdf)})
    job2 = _wait(r2.json()["job_id"], timeout=150)
    assert job2["status"] == "done"
    assert client.get(job2["outputs"][0]["url"]).content[:2] == b"PK"


@pytest.mark.network
def test_tts():
    r = client.post("/api/tts", json={"text": "测试合成语音。", "voice": "xiaoxiao"})
    job = _wait(r.json()["job_id"], timeout=90)
    assert job["status"] == "done"
    assert len(client.get(job["outputs"][0]["url"]).content) > 5000


@pytest.mark.network
def test_translate_text():
    r = client.post("/api/translate", json={"text": "人工智能改变世界。", "target": "en"})
    job = _wait(r.json()["job_id"], timeout=90)
    assert job["status"] == "done"
    assert "intelligence" in job["text"].lower() or "AI" in job["text"]


@pytest.mark.slow
def test_transcribe():
    r = client.post("/api/transcribe",
                    files={"file": ("tone.wav", (DATA / "tone.wav").read_bytes())},
                    data={"language": "zh", "docx": "1"})
    job = _wait(r.json()["job_id"], timeout=600)
    assert job["status"] == "done"
    names = [o["name"] for o in job["outputs"]]
    assert any(n.endswith(".txt") for n in names)
    assert any(n.endswith(".md") for n in names)
    assert any(n.endswith(".docx") for n in names)
