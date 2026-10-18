# -*- coding: utf-8 -*-
"""媒体工具箱 FastAPI 入口：上传 → 任务 → 轮询 → 下载。"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, convert, imageedit, jobs, media, ocr, stt, subtitle, translate, tts, youtube, videoedit

app = FastAPI(title="媒体工具箱")

_IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
_SAFE_NAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _save_upload(upload: UploadFile, allowed: set[str], tag: str) -> Path:
    """流式落盘上传文件：后缀白名单 + 大小上限 + 文件名净化。"""
    name = upload.filename or "upload"
    stem = _SAFE_NAME.sub("_", Path(name).stem)[:80] or "file"
    ext = Path(name).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"不支持的格式 {ext or '(无后缀)'}，允许：{' '.join(sorted(allowed))}")
    dst = config.UP_DIR / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}-{tag}{ext}"
    size = 0
    with dst.open("wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                f.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(413, f"文件超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限")
            f.write(chunk)
    dst.with_suffix(dst.suffix + ".stem").write_text(stem, encoding="utf-8")  # 原名留档
    return dst


def _original_stem(src: Path) -> str:
    side = src.with_suffix(src.suffix + ".stem")
    try:
        return side.read_text(encoding="utf-8")[:80]
    except OSError:
        return src.stem


def _out(job_id: str, filename: str) -> Path:
    return config.OUT_DIR / f"{job_id}-{filename}"


def _submit_upload(kind: str, fn, src: Path, *args) -> str:
    """登记任务；结束后删掉上传原件与 .stem 边车（产出已另存 OUT_DIR）。"""
    def wrapped(job_id: str, *a) -> dict:
        try:
            return fn(job_id, src, *a)
        finally:
            src.unlink(missing_ok=True)
            src.with_suffix(src.suffix + ".stem").unlink(missing_ok=True)
    return jobs.submit(kind, wrapped, *args)


def _submit_uploads(kind: str, fn, srcs: list[Path], *args) -> str:
    """多文件变体（视频合并）：结束后逐个删上传原件与边车。"""
    def wrapped(job_id: str, *a) -> dict:
        try:
            return fn(job_id, srcs, *a)
        finally:
            for s in srcs:
                s.unlink(missing_ok=True)
                s.with_suffix(s.suffix + ".stem").unlink(missing_ok=True)
    return jobs.submit(kind, wrapped, *args)


# ---------- 任务工作函数（线程池内执行，首参 job_id） ----------

def work_extract_audio(job_id: str, src: Path, fmt: str) -> dict:
    jobs.set_progress(job_id, 0.2, "提取音频中")
    dst = _out(job_id, f"{_original_stem(src)}.{fmt}")
    media.extract_audio(src, dst, fmt)
    jobs.add_output(job_id, dst.name, dst)
    return {"message": "音频已提取"}


def work_transcribe(job_id: str, src: Path, language: str, want_docx: bool) -> dict:
    dur = media.duration(src)
    wav = _out(job_id, "stt-16k.wav")
    jobs.set_progress(job_id, 0.05, "转码 16kHz")
    media.to_wav16k(src, wav)

    def cb(done_sec: float) -> None:
        if dur > 0:
            jobs.set_progress(job_id, 0.1 + 0.8 * min(done_sec / dur, 1.0), "识别中")

    try:
        result = stt.transcribe(wav, language, cb)
    finally:
        wav.unlink(missing_ok=True)
    stem = _original_stem(src)
    txt = _out(job_id, f"{stem}.txt")
    txt.write_text(result["text"], encoding="utf-8")
    jobs.add_output(job_id, txt.name, txt)
    md = _out(job_id, f"{stem}-时间轴.md")
    md.write_text(stt.to_markdown(result["segments"]), encoding="utf-8")
    jobs.add_output(job_id, md.name, md)
    if want_docx:
        docx = _out(job_id, f"{stem}.docx")
        convert.text_to_docx(result["text"], docx, title=stem)
        jobs.add_output(job_id, docx.name, docx)
    jobs.set_progress(job_id, 0.95, "整理文档")
    return {"text": result["text"], "message": f"识别完成（{result['language']}）",
            "meta": {"language": result["language"], "duration": result["duration"],
                     "engine": stt.device_info()}}


def work_tts(job_id: str, text: str, voice: str, rate: int) -> dict:
    jobs.set_progress(job_id, 0.3, "合成中")
    dst = _out(job_id, f"tts-{voice}.mp3")
    meta = tts.synthesize(text, dst, voice, rate)
    jobs.add_output(job_id, dst.name, dst)
    return {"message": f"合成完成（{meta['chars']} 字）", "meta": meta}


def work_docx2pdf(job_id: str, src: Path) -> dict:
    jobs.set_progress(job_id, 0.2, "解析 docx")
    dst = _out(job_id, f"{_original_stem(src)}.pdf")
    convert.docx_to_pdf(src, dst)
    jobs.add_output(job_id, dst.name, dst)
    return {"message": "已转 PDF"}


def work_pdf2docx(job_id: str, src: Path) -> dict:
    dst = _out(job_id, f"{_original_stem(src)}.docx")
    convert.pdf_to_docx(src, dst, lambda p: jobs.set_progress(job_id, 0.05 + 0.9 * p, "转换中"))
    jobs.add_output(job_id, dst.name, dst)
    return {"message": "已转 Word"}


def work_ocr(job_id: str, src: Path) -> dict:
    jobs.set_progress(job_id, 0.3, "识别中")
    result = ocr.recognize(src)
    if not result["text"].strip():
        return {"text": "", "message": "未识别到文字"}
    return {"text": result["text"], "message": f"识别到 {len(result['lines'])} 行"}


def work_translate(job_id: str, text: str, target: str) -> dict:
    result = translate.translate(
        text, target, lambda p: jobs.set_progress(job_id, p, "翻译中"))
    return {"text": result["text"],
            "message": f"已译为{result['target']}" + (f"（{result['chunks']} 段）" if result["chunks"] > 1 else "")}


def _progress_cb(job_id: str, stage: str, base: float = 0.1, span: float = 0.85):
    """把引擎回调的 0..1 映射到任务进度区间。"""
    def cb(ratio: float) -> None:
        jobs.set_progress(job_id, base + span * ratio, stage)
    return cb


def _run_engine(dst: Path, fn, *args) -> None:
    """引擎失败时删掉半截产出——GC 只清已注册输出，失败/超时留下的
    未注册残件会永久堆积在 data/out。"""
    try:
        fn(*args)
    except Exception:
        dst.unlink(missing_ok=True)
        raise


def work_video_trim(job_id: str, src: Path, start: float, end: float) -> dict:
    jobs.set_progress(job_id, 0.05, "裁剪片段中")
    dst = _out(job_id, f"{_original_stem(src)}-剪辑.mp4")
    _run_engine(dst, videoedit.trim, src, dst, start, end,
                _progress_cb(job_id, "裁剪片段中"))
    jobs.add_output(job_id, dst.name, dst)
    return {"message": f"已裁剪 {start:.1f}s–{end:.1f}s"}


def work_video_compress(job_id: str, src: Path, height: int, crf: int,
                        out_fmt: str) -> dict:
    in_bytes = src.stat().st_size
    jobs.set_progress(job_id, 0.05, "压缩转码中")
    stem = _original_stem(src)
    dst = _out(job_id, f"{stem}-压缩.{out_fmt}")
    _run_engine(dst, videoedit.compress, src, dst, height, crf, out_fmt,
                _progress_cb(job_id, "压缩转码中"))
    jobs.add_output(job_id, dst.name, dst)
    out_bytes = dst.stat().st_size
    ratio = (1 - out_bytes / in_bytes) * 100 if in_bytes else 0
    return {"message": f"转码完成（{in_bytes // 1024}KB → {out_bytes // 1024}KB）",
            "meta": {"in_bytes": in_bytes, "out_bytes": out_bytes,
                     "saved_percent": round(ratio, 1)}}


def work_video_merge(job_id: str, srcs: list[Path]) -> dict:
    jobs.set_progress(job_id, 0.05, "合并多段中")
    dst = _out(job_id, "合并.mp4")
    _run_engine(dst, videoedit.merge, srcs, dst,
                _progress_cb(job_id, "合并多段中"))
    jobs.add_output(job_id, dst.name, dst)
    return {"message": f"已合并 {len(srcs)} 段"}


def work_video_transform(job_id: str, src: Path, rotate: int, flip: str,
                         mute: bool) -> dict:
    jobs.set_progress(job_id, 0.05, "画面处理中")
    dst = _out(job_id, f"{_original_stem(src)}-画面.mp4")
    _run_engine(dst, videoedit.transform, src, dst, rotate, flip, mute,
                _progress_cb(job_id, "画面处理中"))
    jobs.add_output(job_id, dst.name, dst)
    done = [s for s, on in ((f"旋转{rotate}°", rotate),
                            ({"h": "水平翻转", "v": "垂直翻转"}.get(flip, ""), flip != "none"),
                            ("去音轨", mute)) if on]
    return {"message": "画面处理完成" + (f"（{'、'.join(done)}）" if done else "")}


def work_video_cover(job_id: str, src: Path, t: float) -> dict:
    jobs.set_progress(job_id, 0.3, "提取封面帧")
    dst = _out(job_id, f"{_original_stem(src)}-封面.jpg")
    _run_engine(dst, videoedit.cover, src, dst, t)
    jobs.add_output(job_id, dst.name, dst)
    return {"message": f"已提取 {t:.1f}s 处封面帧"}


def work_video_watermark(job_id: str, src: Path, text: str, pos: str,
                         opacity: float, size: int) -> dict:
    jobs.set_progress(job_id, 0.05, "加文字水印中")
    dst = _out(job_id, f"{_original_stem(src)}-水印.mp4")
    txtfile = _out(job_id, "wm.txt")
    _run_engine(dst, videoedit.watermark, src, dst, text, pos, opacity, size,
                txtfile, _progress_cb(job_id, "加文字水印中"))
    jobs.add_output(job_id, dst.name, dst)
    return {"message": "水印已添加"}


def work_video_subtitle(job_id: str, src: Path, source: str, target: str) -> dict:
    """音视频 → 双语/译文/原文三份 SRT。识别本地零 token，翻译走方舟。"""
    dur = media.duration(src)
    wav = _out(job_id, "sub-16k.wav")
    jobs.set_progress(job_id, 0.02, "转码音频")
    try:
        media.to_wav16k(src, wav)
        segs, detected = subtitle.transcribe_segments(
            wav, source, dur, _progress_cb(job_id, "识别语音", 0.05, 0.55))
    finally:
        wav.unlink(missing_ok=True)  # 转码失败的半截 wav 也一并清掉
    if not segs:
        return {"text": "", "message": "未识别到语音内容"}
    translated = subtitle.translate_segments(
        segs, detected, target, _progress_cb(job_id, "翻译字幕", 0.62, 0.33))
    stem = _original_stem(src)
    outs = ((f"{stem}-双语.srt", subtitle.to_bilingual(segs, translated)),
            (f"{stem}-译文.srt", translated),
            (f"{stem}-原文.srt", [s["text"] for s in segs]))
    for name, texts in outs:
        p = _out(job_id, name)
        _run_engine(p, subtitle.write_srt, p, segs, texts)
        jobs.add_output(job_id, p.name, p)
    tgt_name = translate.TARGETS[target]
    return {"message": f"{len(segs)} 句字幕（{detected}→{tgt_name}），三份 SRT 已出",
            "meta": {"segments": len(segs), "language": detected,
                     "target": target,
                     "batches": (len(segs) + subtitle.BATCH - 1) // subtitle.BATCH}}


def work_speech_translate(job_id: str, src: Path, target: str) -> dict:
    dur = media.duration(src)
    wav = _out(job_id, "st-16k.wav")
    media.to_wav16k(src, wav)

    def cb(done_sec: float) -> None:
        if dur > 0:
            jobs.set_progress(job_id, 0.7 * min(done_sec / dur, 1.0), "识别原文")

    try:
        result = stt.transcribe(wav, "auto", cb)
    finally:
        wav.unlink(missing_ok=True)
    if not result["text"].strip():
        return {"text": "", "message": "未识别到语音内容"}
    jobs.set_progress(job_id, 0.75, "翻译中")
    tr = translate.translate(result["text"], target,
                             lambda p: jobs.set_progress(job_id, 0.75 + 0.25 * p, "翻译中"))
    return {"text": tr["text"], "message": f"已译为{tr['target']}",
            "meta": {"source_text": result["text"], "language": result["language"]}}


def work_yt_transcript(job_id: str, url: str, lang: str, want_summary: bool) -> dict:
    vid = youtube.video_id_of(url)
    if not vid:
        raise ValueError("无法识别的 YouTube 链接（支持 watch / youtu.be / shorts / live）")
    jobs.set_progress(job_id, 0.1, "读取视频信息")
    r = youtube.fetch_transcript(vid, lang)
    jobs.set_progress(job_id, 0.6, "整理文档")
    stem = _SAFE_NAME.sub("_", r["title"])[:60] or vid
    txt = _out(job_id, f"{stem}.txt")
    txt.write_text(youtube.to_plain(r["cues"]), encoding="utf-8")
    jobs.add_output(job_id, txt.name, txt)
    md = _out(job_id, f"{stem}-时间轴.md")
    md.write_text(youtube.to_timeline_md(r["cues"], r["title"]), encoding="utf-8")
    jobs.add_output(job_id, md.name, md)
    srt = _out(job_id, f"{stem}.srt")
    srt.write_text(youtube.to_srt(r["cues"]), encoding="utf-8")
    jobs.add_output(job_id, srt.name, srt)

    summary_text = ""
    if want_summary:
        jobs.set_progress(job_id, 0.75, "生成 AI 总结")
        summary_text = translate.chat(
            "你是内容分析助手。下面是视频「" + r["title"] + "」的字幕文稿，"
            "请用中文输出结构化总结（Markdown），包含：## 一句话概览、## 核心要点（3-7 条）、"
            "## 关键细节与数据、## 结论与启发。只输出总结本身。\n\n文稿：\n"
            + youtube.to_plain(r["cues"])[:12000],
            timeout=180, retries=1)
        smd = _out(job_id, f"{stem}-AI总结.md")
        smd.write_text(
            f"# {r['title']} · AI 总结\n\n"
            f"> 频道：{r['channel']} · 字幕：{r['lang']}"
            f"（{'自动' if r['auto'] else '人工'}）\n\n" + summary_text,
            encoding="utf-8")
        jobs.add_output(job_id, smd.name, smd)

    n = len(r["cues"])
    kind = "自动" if r["auto"] else "人工"
    return {"text": summary_text or youtube.to_plain(r["cues"])[:6000],
            "message": f"{n} 条 {r['lang']}（{kind}字幕）" + ("，已生成 AI 总结" if summary_text else ""),
            "meta": {"video_id": vid, "title": r["title"], "channel": r["channel"],
                     "duration": r["duration"], "lang": r["lang"], "auto": r["auto"],
                     "cues": n}}


# ---------- 路由 ----------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "stt_engine": stt.device_info(),
            "stt_model": config.STT_MODEL,
            "voices": [{"key": k, "label": v[1]} for k, v in tts.VOICES.items()],
            "targets": translate.TARGETS}


@app.post("/api/extract-audio")
def api_extract_audio(file: UploadFile = File(...), fmt: str = Form("mp3")) -> dict:
    if fmt not in ("mp3", "m4a"):
        raise HTTPException(400, "fmt 只支持 mp3/m4a")
    src = _save_upload(file, media.VIDEO_EXT | media.AUDIO_EXT, "va")
    return {"job_id": _submit_upload("extract-audio", work_extract_audio, src, fmt)}


@app.post("/api/transcribe")
def api_transcribe(file: UploadFile = File(...), language: str = Form("auto"),
                   docx: str = Form("1")) -> dict:
    src = _save_upload(file, media.VIDEO_EXT | media.AUDIO_EXT, "tr")
    return {"job_id": _submit_upload("transcribe", work_transcribe, src, language, docx == "1")}


class TtsBody(BaseModel):
    text: str
    voice: str = "xiaoxiao"
    rate: int = 0


@app.post("/api/tts")
def api_tts(body: TtsBody) -> dict:
    if body.voice not in tts.VOICES:
        raise HTTPException(400, f"未知音色：{body.voice}")
    return {"job_id": jobs.submit("tts", work_tts, body.text, body.voice, body.rate)}


@app.post("/api/docx2pdf")
def api_docx2pdf(file: UploadFile = File(...)) -> dict:
    src = _save_upload(file, {".docx"}, "d2p")
    return {"job_id": _submit_upload("docx2pdf", work_docx2pdf, src)}


@app.post("/api/pdf2docx")
def api_pdf2docx(file: UploadFile = File(...)) -> dict:
    src = _save_upload(file, {".pdf"}, "p2d")
    return {"job_id": _submit_upload("pdf2docx", work_pdf2docx, src)}


@app.post("/api/ocr")
def api_ocr(file: UploadFile = File(...)) -> dict:
    src = _save_upload(file, _IMG_EXT, "ocr")
    return {"job_id": _submit_upload("ocr", work_ocr, src)}


class TranslateBody(BaseModel):
    text: str
    target: str = "en"


@app.post("/api/translate")
def api_translate(body: TranslateBody) -> dict:
    if body.target not in translate.TARGETS:
        raise HTTPException(400, f"不支持的目标语言：{body.target}")
    return {"job_id": jobs.submit("translate", work_translate, body.text, body.target)}


class YtTranscriptBody(BaseModel):
    url: str
    lang: str = ""
    summary: bool = True


@app.post("/api/yt-transcript")
def api_yt_transcript(body: YtTranscriptBody) -> dict:
    if not youtube.video_id_of(body.url):
        raise HTTPException(400, "无法识别的 YouTube 链接（支持 watch / youtu.be / shorts / live）")
    if body.lang and not re.fullmatch(r"[\w-]{1,12}", body.lang):
        raise HTTPException(400, "语言参数不合法")
    return {"job_id": jobs.submit("yt-transcript", work_yt_transcript,
                                  body.url, body.lang, body.summary)}


@app.post("/api/speech-translate")
def api_speech_translate(file: UploadFile = File(...), target: str = Form("en")) -> dict:
    if target not in translate.TARGETS:
        raise HTTPException(400, f"不支持的目标语言：{target}")
    src = _save_upload(file, media.VIDEO_EXT | media.AUDIO_EXT, "st")
    return {"job_id": _submit_upload("speech-translate", work_speech_translate, src, target)}


@app.post("/api/video/trim")
def api_video_trim(file: UploadFile = File(...), start: float = Form(0),
                   end: float = Form(0)) -> dict:
    if not (0 <= start < end):
        raise HTTPException(400, "时间区间无效（需 0 ≤ 起点 < 终点）")
    src = _save_upload(file, media.VIDEO_EXT, "vt")
    return {"job_id": _submit_upload("video-trim", work_video_trim, src, start, end)}


@app.post("/api/video/compress")
def api_video_compress(file: UploadFile = File(...), height: int = Form(0),
                       crf: int = Form(28), out: str = Form("mp4")) -> dict:
    if height not in videoedit.COMPRESS_HEIGHTS:
        raise HTTPException(400, f"目标高度只支持 {videoedit.COMPRESS_HEIGHTS}")
    if not 18 <= crf <= 40:
        raise HTTPException(400, "CRF 需在 18–40 之间")
    if out not in videoedit.OUT_FMTS:
        raise HTTPException(400, "输出格式只支持 mp4/gif")
    src = _save_upload(file, media.VIDEO_EXT, "vc")
    return {"job_id": _submit_upload("video-compress", work_video_compress,
                                     src, height, crf, out)}


@app.post("/api/video/merge")
def api_video_merge(files: list[UploadFile] = File(...)) -> dict:
    if not 2 <= len(files) <= 10:
        raise HTTPException(400, "合并需 2–10 个视频")
    srcs: list[Path] = []
    try:
        for i, f in enumerate(files):
            srcs.append(_save_upload(f, media.VIDEO_EXT, f"mg{i}"))
    except HTTPException:
        # 中途校验失败时已落盘的前几个上传件要清掉（_submit_uploads 的
        # finally 只在任务登记后才会跑）
        for p in srcs:
            p.unlink(missing_ok=True)
            p.with_suffix(p.suffix + ".stem").unlink(missing_ok=True)
        raise
    return {"job_id": _submit_uploads("video-merge", work_video_merge, srcs)}


@app.post("/api/video/subtitle")
def api_video_subtitle(file: UploadFile = File(...), source: str = Form("auto"),
                       target: str = Form("zh")) -> dict:
    if source not in subtitle.SRC_LANGS:
        raise HTTPException(400, f"源语言只支持 {'/'.join(subtitle.SRC_LANGS)}")
    if target not in translate.TARGETS:
        raise HTTPException(400, f"目标语言只支持 {'/'.join(translate.TARGETS)}")
    src = _save_upload(file, media.VIDEO_EXT | media.AUDIO_EXT, "vs")
    return {"job_id": _submit_upload("video-subtitle", work_video_subtitle,
                                     src, source, target)}


@app.post("/api/video/transform")
def api_video_transform(file: UploadFile = File(...), rotate: int = Form(0),
                        flip: str = Form("none"), mute: str = Form("0")) -> dict:
    if rotate not in videoedit.ROTATES:
        raise HTTPException(400, "旋转角度只支持 0/90/180/270")
    if flip not in videoedit.FLIPS:
        raise HTTPException(400, "翻转只支持 none/h/v")
    src = _save_upload(file, media.VIDEO_EXT, "vf")
    return {"job_id": _submit_upload("video-transform", work_video_transform,
                                     src, rotate, flip, mute == "1")}


@app.post("/api/video/cover")
def api_video_cover(file: UploadFile = File(...), t: float = Form(0)) -> dict:
    if t < 0:
        raise HTTPException(400, "时间点不能为负")
    src = _save_upload(file, media.VIDEO_EXT, "vk")
    return {"job_id": _submit_upload("video-cover", work_video_cover, src, t)}


@app.post("/api/video/watermark")
def api_video_watermark(file: UploadFile = File(...), text: str = Form(""),
                        pos: str = Form("br"), opacity: float = Form(0.8),
                        size: int = Form(36)) -> dict:
    text = text.strip()
    if not 1 <= len(text) <= 100:
        raise HTTPException(400, "水印文字需 1–100 字")
    if pos not in videoedit._POS:
        raise HTTPException(400, "位置只支持九宫格 tl/tc/tr/ml/mc/mr/bl/bc/br")
    if not 0.1 <= opacity <= 1.0:
        raise HTTPException(400, "不透明度需在 0.1–1.0 之间")
    if not 12 <= size <= 200:
        raise HTTPException(400, "字号需在 12–200 之间")
    src = _save_upload(file, media.VIDEO_EXT, "vw")
    return {"job_id": _submit_upload("video-watermark", work_video_watermark,
                                     src, text, pos, opacity, size)}


def work_image_edit(job_id: str, src: Path, ops: list[dict]) -> dict:
    jobs.set_progress(job_id, 0.3, "图片处理中")
    fmt = next((op["fmt"] for op in reversed(ops) if op["type"] == "format"),
               "jpg")
    dst = _out(job_id, f"{_original_stem(src)}-编辑.{fmt}")
    try:
        meta = imageedit.apply(src, dst, ops)
    except ValueError as e:
        dst.unlink(missing_ok=True)
        raise RuntimeError(str(e)) from None
    except Exception:
        dst.unlink(missing_ok=True)
        raise
    jobs.add_output(job_id, dst.name, dst)
    return {"message": f"图片已导出（{meta['width']}×{meta['height']} {fmt}）",
            "meta": meta}


@app.post("/api/image/edit")
def api_image_edit(file: UploadFile = File(...), ops: str = Form(...)) -> dict:
    try:
        parsed = imageedit.validate_ops(ops)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    src = _save_upload(file, _IMG_EXT, "ie")
    return {"job_id": _submit_upload("image-edit", work_image_edit, src, parsed)}


@app.get("/api/job/{job_id}")
def api_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    return job


@app.get("/api/download/{job_id}/{name}")
def api_download(job_id: str, name: str) -> FileResponse:
    path = jobs.output_path(job_id, name)
    if not path:
        raise HTTPException(404, "文件不存在或已过期")
    return FileResponse(path, filename=path.name.split("-", 1)[-1])


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


# 供测试/脚本复用上传目录清理
def cleanup_uploads(older_than: float = config.JOB_TTL_SEC) -> None:
    now = time.time()
    for p in config.UP_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > older_than:
                p.unlink(missing_ok=True)
        except OSError:
            pass
