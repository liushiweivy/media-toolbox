# -*- coding: utf-8 -*-
"""YouTube 转文字单元测试：纯函数全覆盖（无网络）；联网端到端需 MT_YT_LIVE=1 显式开启。"""
from __future__ import annotations

import os

import pytest

from server import youtube

# ---------- video_id_of ----------

@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=E6EGSBXC3qg", "E6EGSBXC3qg"),
    ("https://www.youtube.com/watch?v=E6EGSBXC3qg&t=120s", "E6EGSBXC3qg"),
    ("https://youtu.be/E6EGSBXC3qg", "E6EGSBXC3qg"),
    ("https://www.youtube.com/shorts/E6EGSBXC3qg", "E6EGSBXC3qg"),
    ("https://www.youtube.com/live/E6EGSBXC3qg", "E6EGSBXC3qg"),
    ("https://www.youtube.com/embed/E6EGSBXC3qg", "E6EGSBXC3qg"),
    ("https://m.youtube.com/watch?v=E6EGSBXC3qg", "E6EGSBXC3qg"),
    ("  https://www.youtube.com/watch?v=E6EGSBXC3qg  ", "E6EGSBXC3qg"),
])
def test_video_id_ok(url, vid):
    assert youtube.video_id_of(url) == vid


@pytest.mark.parametrize("url", [
    "", "not-a-url", "https://www.bilibili.com/video/BV1xx",
    "https://www.youtube.com/watch", "https://www.youtube.com/watch?v=ab",  # 太短
    "https://www.youtube.com/feed/subscriptions", "https://youtu.be/",
])
def test_video_id_bad(url):
    assert youtube.video_id_of(url) is None


# ---------- parse_vtt ----------

MANUAL_VTT = """WEBVTT
Kind: captions
Language: zh-Hans

00:00:00.266 --> 00:00:02.266
大家好最近出去旅行刚回来

00:00:02.266 --> 00:00:03.500
走了之前录了两集
"""

AUTO_VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:02.000
Hi everyone

00:00:02.000 --> 00:00:03.000
Hi everyone and welcome

00:00:03.000 --> 00:00:04.000
and welcome back

00:00:04.000 --> 00:00:05.000
and welcome back
to the show
"""


def test_parse_vtt_manual():
    cues = youtube.parse_vtt(MANUAL_VTT)
    assert cues == [
        {"start": 0.266, "end": 2.266, "text": "大家好最近出去旅行刚回来"},
        {"start": 2.266, "end": 3.5, "text": "走了之前录了两集"},
    ]


def test_parse_vtt_auto_dedupe():
    """自动字幕滚动窗：包含上条的合并扩展，重复的跳过。"""
    cues = youtube.parse_vtt(AUTO_VTT)
    texts = [c["text"] for c in cues]
    assert texts == ["Hi everyone and welcome", "and welcome back to the show"]


def test_parse_vtt_html_entities():
    cues = youtube.parse_vtt(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nTom &amp; Jerry&#39;s &lt;show&gt;\n")
    assert cues[0]["text"] == "Tom & Jerry's <show>"


def test_parse_vtt_empty():
    assert youtube.parse_vtt("WEBVTT\n\nNOTE nothing here\n") == []


# ---------- pick_track ----------

SUBS = {"en": [], "zh-Hans": [], "ja": []}
AUTOS = {"en": [], "ko": []}


def test_pick_manual_zh_first():
    t = youtube.pick_track(SUBS, AUTOS)
    assert t == {"lang": "zh-Hans", "auto": False}


def test_pick_want_lang():
    t = youtube.pick_track(SUBS, AUTOS, "ja")
    assert t == {"lang": "ja", "auto": False}


def test_pick_auto_fallback():
    t = youtube.pick_track({}, AUTOS)
    assert t == {"lang": "en", "auto": True}


def test_pick_region_variant():
    t = youtube.pick_track({"en-US": []}, {})
    assert t == {"lang": "en-US", "auto": False}


def test_pick_none():
    assert youtube.pick_track({}, {}) is None


# ---------- 文稿格式化 ----------

CUES = [
    {"start": 0.0, "end": 1.5, "text": "第一句"},
    {"start": 65.2, "end": 67.0, "text": "第二句"},
]


def test_to_plain():
    assert youtube.to_plain(CUES) == "第一句\n第二句"


def test_to_srt():
    srt = youtube.to_srt(CUES)
    assert "1\n00:00:00,000 --> 00:00:01,500\n第一句" in srt
    assert "2\n00:01:05,200 --> 00:01:07,000\n第二句" in srt


def test_to_timeline_md():
    md = youtube.to_timeline_md(CUES, "标题")
    assert md.startswith("# 标题")
    assert "| 1:05 | 第二句 |" in md


# ---------- 联网端到端（默认跳过） ----------

@pytest.mark.skipif(os.environ.get("MT_YT_LIVE") != "1",
                    reason="需代理与 YouTube 网络，MT_YT_LIVE=1 时运行")
def test_fetch_live():
    r = youtube.fetch_transcript("E6EGSBXC3qg")
    assert r["title"] and r["cues"] and not r["auto"]
    assert r["lang"].startswith("zh")
