# HANDOFF — 媒体工具箱

## 现状（2026-10-18 三期更新）

- 页面 13 卡：原 9 功能 + **YouTube 转文字** + 视频剪辑 + 图片编辑 + **视频翻译字幕**
- pytest 86 绿 1 跳过（含 youtube 27 项纯函数 + subtitle 15 项；MT_YT_LIVE=1 可跑联网端到端）
- 视频翻译字幕卡：本地 whisper 识别（零 token）→ 方舟 50 行/批编号翻译 →
  **双语对照 + 纯译文 + 原文三份 SRT**；`server/subtitle.py` 泛化自 `tools/ja2zh_srt.py`
  （日译中 CLI，保留原样可用）；源语言 auto/ja/en/zh/ko，VAD 小声调参 +
  condition_on_previous_text=False；「译为」select 支持 data-default 指定默认值（此卡默认 zh）
- 字幕卡复审 APPROVE 后整改：to_wav16k 入 try（finally 清转码半截 wav）+ SRT 写盘走
  _run_engine（失败删残件）；ts() 负秒钳 0；write_srt 压掉 cue 内空行（防严格解析器截断）；
  剥编号仅限首行确为「1.」（防误吃译文开头数字如"3.14"）
- 服务：start.bat → **127.0.0.1:8840**（要局域网访问显式 MT_HOST=0.0.0.0）
- YouTube 转文字：`server/youtube.py` + `bin/yt-dlp.exe`（免 python）；
  **必须走代理**（MT_YT_PROXY 默认 127.0.0.1:7897，直连 YouTube 超时）；
  产出 txt + 时间轴.md + srt + AI总结.md（方舟，180s 超时 + 1 重试——60s 默认超时实测不够）
- 浏览器实测：8840 页面粘贴 E6EGSBXC3qg → 346 条 zh-Hans 人工字幕 + AI 总结（结构化要点）全链路通过

## 历史（2026-10-17 收官，双审整改后复测全绿）

- 原 9 功能全部真机验证通过，pytest 14 绿（20s），CDP 页面冒烟 14/14
- 铜金深色 bento 九宫，拖拽上传 + 进度轮询 + 下载/复制/试听

## 双审整改（code-reviewer + security-reviewer，已全部复测）

1. 方舟 key 移出源码 → 环境变量 MT_ARK_KEY 或 `data/ark.key`（本地文件，勿提交）
2. 上传原件任务结束即删（_submit_upload 包装器 finally 删 src + .stem 边车）
3. Edge 打印 profile 目录 finally rmtree（原每次转换漏一个浏览器 profile）
4. 默认绑 127.0.0.1（原 0.0.0.0 裸暴露局域网，翻译会烧付费额度）
5. 上传路由 async→sync（512MB 同步读写不再堵事件循环）
6. 上传文件名加 uuid 前缀（同毫秒同 tag 冲突）
7. 翻译切块超上限报错（原 chunks[:10] 静默截断丢文本）
8. GC 不动 queued/running 任务 + 完成时刻重计 TTL（原长队可被误杀致 KeyError）
9. docx→HTML 加 CSP（default-src 'none'; img-src data:，防恶意 docx 外联）
10. 前端选项填充 innerHTML→DOM API；TTS 音色/翻译语种路由层校验

## 结构

```
server/  config(常量) jobs(队列) media(ffmpeg+probe+run_with_progress) stt(whisper)
         tts(edge-tts) ocr(rapidocr) translate(方舟) convert(mammoth+Edge/pdf2docx)
         youtube(yt-dlp 字幕+总结) videoedit(剪辑六操作) imageedit(Pillow ops 引擎)
         subtitle(识别+批翻+SRT) app(路由)
static/  index.html(十三卡) app.js(轮询渲染+视频卡+图片画布编辑器) styles.css(铜金)
desktop.py 桌面软件版（uvicorn 线程+pywebview 原生窗口，Bridge.save_output 原生另存为）
desktop.bat 双击起桌面版（pythonw 无控制台）
tests/   test_core.py 14 项 + test_desktop.py 6 项 + test_edit.py 24 项（剪辑/P图流水线）
         + test_youtube.py 27 项纯函数 + test_subtitle.py 15 项（fixtures 会话级自造）
tools/   cdp-check.mjs 22 项页面冒烟 · mk-launchpad*.py 启动台快捷方式生成
         ja2zh_srt.py 日译中字幕 CLI（subtitle.py 的实战原型，独立可用）
data/    up/ 上传 · out/ 产出 · server.log · ark.key（方舟 key，勿提交）
```

## 视频剪辑 / 图片编辑（2026-10-18 追加）

- **视频剪辑卡**（`data-kind="video-edit"`）：六操作 select 切换参数组——
  裁剪片段（-ss 前置 + -t 差值帧级准确）/ 压缩转码（高度+CRF→MP4 或单命令 palette GIF）/
  合并多段（2–10，concat filter 重编码，无声轨段 lavfi anullsrc 补齐）/
  旋转翻转静音 / 提取封面帧 / 中文文字水印（九宫格位置）。
- **图片编辑卡**（`data-kind="image-edit"`，宽卡）：内嵌 canvas 编辑器，
  拖框裁剪/马赛克/涂鸦/点落文字 + 旋转翻转缩放；一次任务提交有序 ops 数组，
  后端 `imageedit.apply` 按序应用（exif_transpose 第一个做，与浏览器坐标系一致）；
  **前端几何 op 入栈时把既有叠加层坐标同步变换，保证两端语义一致**。
- 进度：重编码走 `media.run_with_progress`（-progress pipe:1 解析 out_time_us，
  映射到 0.1~0.95）；`validate_ops` 路由层先验，坏输入 400。
- 测试：test_edit.py 24 项（真实 ffmpeg/Pillow 流水线 + 守卫）；CDP 21/21。
- code-reviewer 复审 APPROVE（0 CRITICAL/HIGH），6 项 MEDIUM 已整改：
  drawtext 补 `expansion=none`（用户文本含 `%{` 不再炸任务）；
  `_run_engine` 包装——引擎失败/超时删半截产出（原 GC 只清已注册输出，残件会堆积）；
  run_with_progress 看门狗覆盖流式阶段（原 timeout 只守 stdout EOF 后的 wait，
  ffmpeg 卡死会永久占住 worker 线程）；merge 路由中途校验失败清已落盘上传件；
  imageedit 放大后像素量 >MAX_IMAGE_PIXELS 拒（防 scale=10 内存耗尽）+ ops JSON
  拒 NaN/Infinity；前端图片 >40MP 拒编辑（两张全尺寸画布内存保护）。

## 桌面软件版（2026-10-17 追加）

- pywebview 6.2.1（WebView2，本机已装运行时），desktop.py 起 uvicorn 守护线程
  （127.0.0.1 + free_port() 随机空闲端口，不与网页版 :8840 抢）→ wait_ready → 开窗口
- 下载：前端检测 `window.pywebview.api` → Bridge.save_output 弹原生另存为复制产出
- Bridge 持 window 引用用「先建 bridge(None) 再回填」模式，勿碰 window._js_api 内部
- 验证：6 项 pytest（free_port/服务线程/resolve_output/Bridge 保存+取消）+ 真机窗口
  PrintWindow 截图确认渲染（SetForegroundWindow 被系统焦点锁拒时可用 PrintWindow 抓遮挡窗口）

## 踩坑记录（重要）

1. **av 19 砍了 `metadata_errors` 参数**：faster-whisper 1.2.1 解码音频必炸
   `TypeError: open() got an unexpected keyword argument 'metadata_errors'`。
   锁定 **av==18.1.0**（14.x 无 cp312 win 轮，源码编译要 ffmpeg dev 库）。
2. **hf-mirror 不代理 Xet CAS**：whisper 模型下载 401。
   必须 `HF_HUB_DISABLE_XET=1`（stt.py 已 setdefault）；hf-xet 包已卸载。
3. **无 Office/LibreOffice**：docx→pdf 走 mammoth→HTML→Edge `--headless=new --print-to-pdf`，
   独立 user-data-dir 避免和日常浏览器抢锁。
4. **任务登记表在内存**：重启丢记录导致下载 404 → jobs.output_path 加了
   `{jobid}-{name}` 盘查兜底，重启前链接仍有效。
5. 前端取任务文本是 **job.text**（扁平），不是 job.result.text——CDP 冒烟抓出来的。
6. 改 static/ 后必须 bump index.html 里的 `?v=`，否则浏览器吃缓存。
7. **drawtext 中文/Windows 转义地狱**：用 `textfile=`（文本落盘 UTF-8 无 BOM 临时文件），
   不用 `text=`；fontfile 写 `'C\:/Windows/Fonts/msyh.ttc'`（正斜杠 + 冒号 `\:` + 单引号）。
8. **Popen 进度管道死锁**：`-progress pipe:1` 时 stderr=PIPE 必须起守护线程持续抽干，
   否则 ffmpeg 塞满 stderr 管道后整体挂死。
9. **ffprobe 必须显式 utf-8**：subprocess text 模式默认 GBK，输出含中文文件名即
   UnicodeDecodeError（线程内炸，pytest 只报 warning 极易漏）。encoding="utf-8", errors="replace"。
10. **CSS display 类盖 hidden 属性**：`.vop-group{display:flex}` 会赢过 UA 的
    `[hidden]{display:none}`——必须全局 `[hidden]{display:none!important}`；
    冒烟断言要用 getComputedStyle，不能只看 .hidden 属性。
11. **Git Bash 吃 taskkill 的 /PID**（转成 D:/... 路径）：`MSYS_NO_PATHCONV=1 taskkill /PID n /F`。
12. **Git Bash 起 start.bat 静默失败**：走 PowerShell
    `Start-Process cmd '/c start.bat' -WorkingDirectory ... -WindowStyle Hidden`。
13. **lavfi 补音轨也占 -i 序号**：merge 里 anullsrc 输入混插在视频之间时，
    滤镜标签必须用自增 input_idx，不能用循环下标。

## 续做入口

- 换 large-v3-turbo 正式跑：去掉 MT_STT_MODEL 环境变量重启即可（模型 ~1.6GB 首次下载）
- 重启服务：`netstat -ano | findstr :8840` 查 PID → 按 PID taskkill → start.bat
  （**禁止 taskkill /IM python.exe**，本机多 python 项目并发）
