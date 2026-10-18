# -*- coding: utf-8 -*-
"""文档转换：docx→pdf（mammoth 转 HTML + Edge 无头打印）、pdf→docx（pdf2docx）、文本→docx。

本机无 Office/LibreOffice，docx→pdf 走 mammoth 保真还原正文/标题/表格/图片，
Edge --print-to-pdf 负责排版输出；适合常规图文文档，复杂版式可能有出入。
"""
from __future__ import annotations

import html as html_mod
import shutil
import subprocess
import time
from pathlib import Path

from . import config

# CSP：mammoth 图片默认内联 base64，禁掉一切外部资源请求（防恶意 docx 外联）
HTML_WRAPPER = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<style>
  @page {{ size: A4; margin: 2.2cm 2cm; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
         font-size: 11pt; line-height: 1.75; color: #1a1a1a; }}
  img {{ max-width: 100%; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ border: 1px solid #999; padding: 4px 8px; }}
</style></head><body>
{body}
</body></html>"""


def docx_to_pdf(src: Path, dst: Path) -> dict:
    import mammoth

    with src.open("rb") as f:
        result = mammoth.convert_to_html(f)
    body = result.value
    if not body.strip():
        raise ValueError("docx 内容为空或无法解析")
    html_path = dst.with_suffix(".html")
    html_path.write_text(HTML_WRAPPER.format(body=body), encoding="utf-8")
    try:
        _edge_print(html_path, dst)
    finally:
        html_path.unlink(missing_ok=True)
    if not dst.is_file() or dst.stat().st_size < 200:
        raise RuntimeError("Edge 打印未产出 PDF")
    return {"bytes": dst.stat().st_size}


def _edge_print(html_path: Path, pdf_path: Path, timeout: int = 120) -> None:
    """Edge 无头打印；用独立 user-data-dir 避免和日常浏览器抢锁。"""
    edge = Path(config.EDGE_EXE)
    if not edge.is_file():
        raise RuntimeError(f"找不到 Edge：{edge}")
    profile = config.DATA_DIR / f"edge-prof-{int(time.time() * 1000)}"
    cmd = [
        str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
        f"--user-data-dir={profile}", f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError("Edge 打印失败：" + (proc.stderr or "")[-200:])


def pdf_to_docx(src: Path, dst: Path, progress_cb=None) -> dict:
    from pdf2docx import Converter

    cv = Converter(str(src))
    try:
        kwargs = {}
        if progress_cb:
            # pdf2docx 按页回调进度（0-1）
            kwargs["progress"] = lambda p: progress_cb(float(p))
        cv.convert(str(dst), **kwargs)
    finally:
        cv.close()
    if not dst.is_file() or dst.stat().st_size < 1024:
        raise RuntimeError("转换未产出 docx")
    return {"bytes": dst.stat().st_size}


def text_to_docx(text: str, dst: Path, title: str = "") -> None:
    from docx import Document

    doc = Document()
    if title:
        doc.add_heading(title, level=1)
    for para in text.split("\n"):
        doc.add_paragraph(para)
    doc.save(str(dst))


def text_to_html_page(text: str, title: str = "文档") -> str:
    return HTML_WRAPPER.format(
        body=f"<h2>{html_mod.escape(title)}</h2><p>" +
        html_mod.escape(text).replace("\n", "<br>") + "</p>")
