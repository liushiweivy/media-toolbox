# -*- coding: utf-8 -*-
"""视频翻译字幕流水线测试：纯函数 + 批翻降级 + 路由守卫 +
monkeypatch 假模型/假翻译的全链路（无网络、无真实 whisper，秒级）。

运行：.venv/Scripts/python.exe -m pytest tests/test_subtitle.py -v
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server import media, subtitle, translate

client = TestClient(app_mod.app)
DATA = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def fixtures() -> None:
    """会话级造样：3s 含音轨 mp4（与 test_core 同款，存在即跳过）。"""
    DATA.mkdir(exist_ok=True)
    if not (DATA / "demo.mp4").is_file():
        media.run([media.ffmpeg(), "-y", "-loglevel", "error",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                   str(DATA / "tone.wav")])
        media.run([media.ffmpeg(), "-y", "-loglevel", "error",
                   "-f", "lavfi", "-i", "color=c=0x203040:s=320x180:d=3",
                   "-i", str(DATA / "tone.wav"),
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                   "-shortest", str(DATA / "demo.mp4")])


def _wait(job_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/job/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.3)
    raise TimeoutError(f"任务 {job_id} 超时未完成")


# ---------- 纯函数 ----------

def test_ts_millisecond_carry():
    assert subtitle.ts(0) == "00:00:00,000"
    assert subtitle.ts(1.5) == "00:00:01,500"
    assert subtitle.ts(3661.25) == "01:01:01,250"
    assert subtitle.ts(1.9996) == "00:00:02,000"  # round 进位不得出 ,1000


def test_write_srt_format(tmp_path):
    segs = [{"start": 0.0, "end": 1.5, "text": "a"},
            {"start": 3661.25, "end": 3663.0, "text": "b"}]
    p = tmp_path / "x.srt"
    subtitle.write_srt(p, segs, ["甲", "乙"])
    body = p.read_text(encoding="utf-8")
    assert body.startswith("1\n00:00:00,000 --> 00:00:01,500\n甲\n\n")
    assert "2\n01:01:01,250 --> 01:01:03,000\n乙\n\n" in body
    assert "\r" not in body  # LF 行尾


def test_to_bilingual_two_lines():
    segs = [{"text": "こんにちは", "start": 0, "end": 1}]
    assert subtitle.to_bilingual(segs, ["你好"]) == ["こんにちは\n你好"]


def test_ts_negative_guard():
    assert subtitle.ts(-1.5) == "00:00:00,000"  # 异常负秒钳到 0，不出垃圾时间戳


def test_write_srt_collapses_blank_lines(tmp_path):
    # 译文带内部空行（话痨模型）不得把 cue 截断
    segs = [{"start": 0.0, "end": 1.0, "text": "a"},
            {"start": 1.0, "end": 2.0, "text": "b"}]
    p = tmp_path / "x.srt"
    subtitle.write_srt(p, segs, ["第一行\n\n第二行", "乙"])
    body = p.read_text(encoding="utf-8")
    assert "第一行\n第二行\n\n2\n" in body
    assert body.count(" --> ") == 2  # 两个 cue 都完好


# ---------- 批翻降级链（假 chat） ----------

def _segs(n: int) -> list[dict]:
    return [{"start": float(i), "end": i + 0.5, "text": f"原文{i}"}
            for i in range(n)]


def test_translate_aligned(monkeypatch):
    monkeypatch.setattr(translate, "chat",
                        lambda prompt, timeout=0, retries=0:
                        "\n".join(f"{i + 1}. 译文{i}" for i in range(3)))
    out = subtitle.translate_segments(_segs(3), "ja", "zh")
    assert out == ["译文0", "译文1", "译文2"]


def test_translate_fallback_per_line(monkeypatch):
    calls = []

    def fake(prompt, timeout=0, retries=0):
        calls.append(prompt)
        if "逐行翻译" in prompt:
            return "1. 只有一行"  # 批量掉行 → 触发降级
        return "单行译文"

    monkeypatch.setattr(translate, "chat", fake)
    out = subtitle.translate_segments(_segs(3), "ja", "zh")
    assert out == ["单行译文"] * 3


def test_translate_line_failure_placeholder(monkeypatch):
    def fake(prompt, timeout=0, retries=0):
        if "逐行翻译" in prompt:
            return "掉行"
        raise RuntimeError("方舟挂了")

    monkeypatch.setattr(translate, "chat", fake)
    out = subtitle.translate_segments(_segs(2), "en", "zh")
    assert out == ["【未译】原文0", "【未译】原文1"]


def test_translate_keeps_leading_digits_when_unnumbered(monkeypatch):
    # 模型没按编号输出时，不得误剥译文自身的开头数字
    monkeypatch.setattr(translate, "chat",
                        lambda prompt, timeout=0, retries=0:
                        "3.14 是圆周率\n42 是答案\n7 月见")
    out = subtitle.translate_segments(_segs(3), "ja", "zh")
    assert out == ["3.14 是圆周率", "42 是答案", "7 月见"]


def test_translate_progress_cb(monkeypatch):
    monkeypatch.setattr(translate, "chat",
                        lambda prompt, timeout=0, retries=0:
                        "\n".join(f"{i + 1}. t{i}" for i in range(50)))
    ratios = []
    subtitle.translate_segments(_segs(50), "ja", "zh", ratios.append)
    assert ratios == [1.0]


# ---------- 路由守卫 ----------

def _post(data: dict, fname: str = "demo.mp4"):
    return client.post("/api/video/subtitle",
                       files={"file": (fname, (DATA / "demo.mp4").read_bytes())},
                       data=data)


def test_guard_bad_source():
    assert _post({"source": "xx", "target": "zh"}).status_code == 400


def test_guard_bad_target():
    assert _post({"source": "auto", "target": "xx"}).status_code == 400


def test_guard_bad_ext():
    r = client.post("/api/video/subtitle",
                    files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                    data={"source": "auto", "target": "zh"})
    assert r.status_code == 400


# ---------- 全链路（假模型 + 假翻译，无网络） ----------

class _FakeSeg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _FakeInfo:
    language = "ja"
    duration = 3.0


class _FakeModel:
    def transcribe(self, path, **kwargs):
        assert kwargs["vad_filter"] and kwargs["no_speech_threshold"] == 0.8
        return iter([_FakeSeg(0.0, 1.0, "おはよう"),
                     _FakeSeg(1.0, 2.0, "こんにちは"),
                     _FakeSeg(2.0, 3.0, "さようなら")]), _FakeInfo()


def test_full_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr("server.stt.get_model", lambda: _FakeModel())
    monkeypatch.setattr(translate, "chat",
                        lambda prompt, timeout=0, retries=0:
                        "1. 早上好\n2. 你好\n3. 再见")
    r = _post({"source": "auto", "target": "zh"})
    assert r.status_code == 200, r.text
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["meta"]["segments"] == 3 and job["meta"]["language"] == "ja"
    names = [o["name"] for o in job["outputs"]]
    assert any("双语" in n for n in names) and any("译文" in n for n in names) \
        and any("原文" in n for n in names)

    texts = {o["name"]: client.get(o["url"]).content.decode("utf-8")
             for o in job["outputs"]}
    bilingual = next(v for k, v in texts.items() if "双语" in k)
    assert "おはよう\n早上好" in bilingual  # 上原文下译文
    assert bilingual.count(" --> ") == 3
    pure = next(v for k, v in texts.items() if "译文" in k and "双语" not in k)
    assert "早上好" in pure and "おはよう" not in pure
    original = next(v for k, v in texts.items() if "原文" in k)
    assert "さようなら" in original and "再见" not in original


class _EmptyModel:
    def transcribe(self, path, **kwargs):
        return iter([]), _FakeInfo()


def test_no_speech_early_return(monkeypatch):
    monkeypatch.setattr("server.stt.get_model", lambda: _EmptyModel())
    called = []
    monkeypatch.setattr(translate, "chat",
                        lambda *a, **k: called.append(1) or "")
    r = _post({"source": "auto", "target": "zh"})
    assert r.status_code == 200, r.text
    job = _wait(r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert "未识别到语音" in job["message"]
    assert job["outputs"] == []
    assert not called  # 无语音时不应发起翻译请求
