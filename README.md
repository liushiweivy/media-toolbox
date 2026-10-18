# 媒体工具箱（media-toolbox）

音视频 / 文档 / 文字 一站式本地处理网页，端口 **8840**。

## 十二项功能

| 功能 | 端点 | 引擎 |
|------|------|------|
| 视频提取音频 | POST /api/extract-audio | ffmpeg（mp3/m4a） |
| 音视频转文档 | POST /api/transcribe (docx=1) | faster-whisper → txt + 时间轴 md + docx |
| 语音转文字 | POST /api/transcribe (docx=0) | faster-whisper（CUDA float16，无卡回落 CPU int8） |
| 文字转语音 | POST /api/tts | edge-tts，8 音色，语速 ±50% |
| Word 转 PDF | POST /api/docx2pdf | mammoth → HTML → Edge 无头打印（无需 Office） |
| PDF 转 Word | POST /api/pdf2docx | pdf2docx |
| 图片文字识别 | POST /api/ocr | RapidOCR（本地 ONNX，模型随包） |
| 文字翻译 | POST /api/translate | 方舟 ark-code-latest，8 语种，>3000 字自动切块 |
| **YouTube 转文字** | POST /api/yt-transcript | yt-dlp 抓字幕（人工优先）→ **完整文稿（txt + 时间轴 md + srt）+ AI 总结版（方舟，可选）** |
| 语音翻译 | POST /api/speech-translate | whisper 识别 → 方舟翻译 流水线 |
| 视频剪辑 | POST /api/video/{trim,compress,merge,transform,cover,watermark} | ffmpeg：裁剪 / 压缩转码 / 合并 / 旋转翻转 / 封面帧 / 文字水印 |
| 图片编辑 | 前端画布 + POST /api/image/* | 拖框裁剪 / 马赛克 / 画笔 / 落文字 / 旋转翻转 |

辅助：GET /api/health、GET /api/job/{id}（轮询）、GET /api/download/{jobid}/{name}。

### YouTube 转文字（2026-10-18 新增）

- 输入：watch / youtu.be / shorts / live 链接 + 字幕语言（默认自动：简中 → 繁中 → 英文，人工轨优先于自动轨）+ 是否生成 AI 总结
- 产出（均含视频标题命名）：
  1. `{标题}.txt` — 完整纯文本文稿
  2. `{标题}-时间轴.md` — 带时间戳的 Markdown 表格
  3. `{标题}.srt` — 标准字幕文件
  4. `{标题}-AI总结.md` — 方舟生成的结构化总结（一句话概览 / 核心要点 / 关键细节 / 结论启发），勾选「AI 总结」时产出；结果区同时直接展示总结全文
- 自动字幕的滚动重复行已去重合并
- **需要代理**访问 YouTube（默认 `http://127.0.0.1:7897`）；yt-dlp 用自带 `bin/yt-dlp.exe`，不依赖系统 python

## 启动

```
start.bat            # 网页版：激活 .venv + 起 uvicorn（127.0.0.1:8840）
desktop.bat          # 桌面软件版：原生窗口（pywebview/WebView2），无浏览器无控制台
```

网页版浏览器打开 http://127.0.0.1:8840/；桌面版双击 `desktop.bat` 直接出窗口
（内嵌服务走 127.0.0.1 随机空闲端口，与网页版互不冲突；窗口内下载走原生另存为对话框）。

## 测试

```
.venv/Scripts/python.exe -m pytest tests/ -v     # 86 项（真实文件全流程 + youtube 模块纯函数）
node tools/cdp-check.mjs                          # 页面冒烟（需 9333 无头 Edge）
MT_YT_LIVE=1 .venv/Scripts/python.exe -m pytest tests/test_youtube.py -v   # YouTube 联网端到端（需代理）
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| MT_STT_MODEL | large-v3-turbo | whisper 模型（测试用 small 提速） |
| MT_ARK_KEY | 无（或写 `data/ark.key` 文件） | 方舟翻译/AI 总结 key（OpenAI 兼容端点可换） |
| MT_FFMPEG | 自动探测 | ffmpeg 路径 |
| MT_MAX_UPLOAD_MB | 512 | 上传上限 |
| MT_YTDLP | bin/yt-dlp.exe | yt-dlp 路径（YouTube 转文字） |
| MT_YT_PROXY | http://127.0.0.1:7897 | YouTube 访问代理 |

## 关键实现

- 任务队列：ThreadPoolExecutor(2) + 内存登记表，前端 900ms 轮询；任务/产出 6h TTL
- 下载链接重启不失效：登记表丢失时按 `{jobid}-{name}` 命名约定盘查兜底
- STT 首次下载模型走 hf-mirror（HF_ENDPOINT + HF_HUB_DISABLE_XET=1）
- 上传流式落盘、后缀白名单、文件名净化、下载防目录穿越
- 产出文件名含原始文件名（.stem 边车文件留档）
- yt-dlp 全程数组参数调用（无 shell 注入面）；URL 严格校验视频 id；AI 总结 180s 超时 + 1 次重试
