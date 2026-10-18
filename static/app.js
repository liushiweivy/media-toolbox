/* 媒体工具箱前端：卡片 · 拖拽上传 · 任务轮询 · 结果下载
 * 视频剪辑卡：操作选择器切换参数组，merge 多文件
 * 图片编辑卡：canvas 编辑器，ops 数组一次提交 */
(() => {
  "use strict";

  const VEP = {
    trim: "/api/video/trim", compress: "/api/video/compress",
    merge: "/api/video/merge", transform: "/api/video/transform",
    cover: "/api/video/cover", watermark: "/api/video/watermark",
  };

  // 各卡片的提交方式：upload=FormData 带文件，json=纯文本参数
  // custom=true 的卡由专用 init 接管拖拽区；build 可追加/校验表单，返回错误串则中止
  const KIND = {
    "extract-audio":    { mode: "upload", endpoint: "/api/extract-audio" },
    "transcribe":       { mode: "upload", endpoint: "/api/transcribe", extra: { docx: "1" } },
    "stt":              { mode: "upload", endpoint: "/api/transcribe", extra: { docx: "0" } },
    "tts":              { mode: "json",   endpoint: "/api/tts",
                          prepare: b => { b.rate = parseInt(b.rate || "0", 10); return b; } },
    "docx2pdf":         { mode: "upload", endpoint: "/api/docx2pdf" },
    "pdf2docx":         { mode: "upload", endpoint: "/api/pdf2docx" },
    "ocr":              { mode: "upload", endpoint: "/api/ocr" },
    "translate":        { mode: "json",   endpoint: "/api/translate" },
    "yt-transcript":    { mode: "json",   endpoint: "/api/yt-transcript" },
    "speech-translate": { mode: "upload", endpoint: "/api/speech-translate" },
    "video-edit":       { mode: "upload", endpoint: "", custom: true, build: buildVideo },
    "image-edit":       { mode: "upload", endpoint: "/api/image/edit", custom: true, build: buildImage },
    "video-subtitle":   { mode: "upload", endpoint: "/api/video/subtitle" },
  };
  const POLL_MS = 900;

  const $ = (sel, root) => (root || document).querySelector(sel);

  /* ---------- 健康检查 + 选项填充 ---------- */
  fetch("/api/health").then(r => r.json()).then(h => {
    $("#engine").textContent =
      `STT ${h.stt_model}（${h.stt_engine}）· OCR 本地 · 翻译 云端`;
    const fill = (sel, entries, selected) => {
      entries.forEach(([value, label]) => {
        const o = document.createElement("option");
        o.value = value;
        o.textContent = label;
        if (value === selected) o.selected = true;
        sel.appendChild(o);
      });
    };
    document.querySelectorAll('select[data-opt="voice"]').forEach(sel => {
      fill(sel, h.voices.map(v => [v.key, v.label]), "xiaoxiao");
    });
    document.querySelectorAll('select[data-opt="target"]').forEach(sel => {
      fill(sel, Object.entries(h.targets), sel.dataset.default || "en");
    });
  }).catch(() => { $("#engine").textContent = "后端未就绪"; });

  /* ---------- 卡片初始化 ---------- */
  document.querySelectorAll(".card").forEach(card => {
    const kind = card.dataset.kind;
    const cfg = KIND[kind];
    if (!cfg) return;
    const runBtn = $(".btn.run", card);

    if (kind === "video-edit") initVideoCard(card, runBtn);
    else if (kind === "image-edit") initImageCard(card, runBtn);
    else initDropCard(card, runBtn);

    runBtn.addEventListener("click", () => submit(card, kind, cfg, runBtn));
  });

  function initDropCard(card, runBtn) {
    const drop = $(".drop", card);
    if (!drop) return;
    const input = document.createElement("input");
    input.type = "file";
    input.accept = drop.dataset.accept || "*/*";
    input.hidden = true;
    card.appendChild(input);

    const pick = file => {
      if (!file) return;
      card._file = file;
      $(".fname", drop).textContent = `${file.name}（${fmtSize(file.size)}）`;
      runBtn.disabled = false;
    };
    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", () => pick(input.files[0]));
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", e => {
      e.preventDefault(); drop.classList.remove("over");
      pick(e.dataTransfer.files[0]);
    });
  }

  function fmtSize(n) {
    return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  }

  // 全量收集卡内 [data-opt]：隐藏参数组也会带上，后端忽略多余表单字段，无害
  function collectOpts(card) {
    const o = {};
    card.querySelectorAll("[data-opt]").forEach(el => {
      o[el.dataset.opt] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
    });
    return o;
  }

  /* ---------- 提交任务 ---------- */
  async function submit(card, kind, cfg, runBtn) {
    const result = $(".result", card);
    const pre = $("pre", result);
    result.classList.remove("on");
    $(".dls", result).innerHTML = "";
    pre.hidden = true; pre.textContent = "";
    const oldAudio = $("audio", result);
    if (oldAudio) oldAudio.remove();

    let resp;
    try {
      if (cfg.mode === "upload") {
        const hasFile = !!card._file || !!(card._files && card._files.length);
        if (!hasFile) { fail(card, "请先选择文件"); return; }
        const fd = new FormData();
        if (card._file) fd.append("file", card._file);
        Object.entries(collectOpts(card)).forEach(([k, v]) => fd.append(k, v));
        Object.entries(cfg.extra || {}).forEach(([k, v]) => fd.append(k, v));
        if (cfg.build) {
          const err = cfg.build(card, fd);
          if (err) { fail(card, err); return; }
        }
        resp = await fetch(card._endpoint || cfg.endpoint, { method: "POST", body: fd });
      } else {
        let body = collectOpts(card);
        if (cfg.prepare) body = cfg.prepare(body) || body;
        if (body.text !== undefined && !body.text.trim()) { fail(card, "请输入文字"); return; }
        resp = await fetch(cfg.endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
    } catch (e) {
      fail(card, "网络错误：" + e.message);
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      fail(card, err.detail || `请求失败（${resp.status}）`);
      return;
    }
    const { job_id } = await resp.json();
    runBtn.disabled = true;
    $(".prog", card).classList.add("on");
    poll(card, kind, job_id, () => {
      runBtn.disabled = cfg.mode === "upload" && !card._file && !(card._files || []).length;
    });
  }

  /* ---------- 轮询 ---------- */
  function poll(card, kind, jobId, onEnd) {
    const bar = $(".prog .bar i", card);
    const pmsg = $(".prog .pmsg", card);
    const timer = setInterval(async () => {
      let job;
      try {
        const r = await fetch(`/api/job/${jobId}`);
        if (!r.ok) throw new Error(`状态查询 ${r.status}`);
        job = await r.json();
      } catch (e) {
        clearInterval(timer); finish(); fail(card, e.message); return;
      }
      bar.style.width = Math.round((job.progress || 0) * 100) + "%";
      pmsg.textContent = job.message || "排队中…";
      if (job.status === "done") {
        clearInterval(timer); finish(); showResult(card, kind, job);
      } else if (job.status === "error") {
        clearInterval(timer); finish(); fail(card, job.message || "处理失败");
      }
    }, POLL_MS);
    function finish() {
      bar.style.width = "100%";
      $(".prog", card).classList.remove("on");
      onEnd();
    }
  }

  /* ---------- 结果渲染 ---------- */
  function showResult(card, kind, job) {
    const result = $(".result", card);
    $(".msg", result).textContent = job.message || "完成";
    $(".msg", result).classList.remove("err");
    const dls = $(".dls", result);
    (job.outputs || []).forEach(o => {
      const a = document.createElement("a");
      a.className = "btn ghost";
      a.href = o.url;
      a.download = "";
      a.textContent = "⬇ " + o.name.split("-").slice(1).join("-");
      a.addEventListener("click", e => {
        // 桌面软件版（pywebview）：走原生另存为对话框
        if (window.pywebview && window.pywebview.api) {
          e.preventDefault();
          window.pywebview.api.save_output(o.url, "").then(msg => {
            const m = $(".msg", result);
            m.textContent = msg;
            m.classList.toggle("err", msg.startsWith("保存失败") || msg.includes("不存在"));
          });
        }
      });
      dls.appendChild(a);
    });
    if (kind === "tts" && job.outputs && job.outputs[0]) {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.src = job.outputs[0].url;
      result.appendChild(audio);
    }
    const text = job.text;
    if (text) {
      const pre = $("pre", result);
      pre.hidden = false;
      pre.textContent = text;
      const copy = document.createElement("button");
      copy.className = "btn ghost";
      copy.textContent = "复制文字";
      copy.addEventListener("click", () => {
        navigator.clipboard.writeText(text).then(
          () => { copy.textContent = "已复制 ✓"; },
          () => { pre.focus(); });
      });
      dls.appendChild(copy);
    }
    result.classList.add("on");
  }

  function fail(card, msg) {
    const result = $(".result", card);
    const m = $(".msg", result);
    m.textContent = "✕ " + msg;
    m.classList.add("err");
    result.classList.add("on");
  }

  /* ==================== 视频剪辑卡 ==================== */

  function buildVideo(card, fd) {
    const vop = $('[data-opt="vop"]', card).value;
    card._endpoint = VEP[vop];
    if (vop === "merge") {
      if ((card._files || []).length < 2) return "合并需至少 2 个视频";
      card._files.forEach(f => fd.append("files", f));
      return null;
    }
    if (!card._file) return "请先选择视频";
    if (vop === "trim") {
      const s = parseFloat(fd.get("start"));
      const e = parseFloat(fd.get("end"));
      if (!(s >= 0 && e > s)) return "时间区间无效（需 0 ≤ 起点 < 终点）";
    }
    if (vop === "watermark" && !String(fd.get("text") || "").trim()) {
      return "请输入水印文字";
    }
    return null;
  }

  function initVideoCard(card, runBtn) {
    const drop = $(".drop", card);
    const dhint = $(".dhint", drop);
    const filelist = $(".filelist", card);
    const vopSel = $('[data-opt="vop"]', card);
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "video/*";
    input.hidden = true;
    card.appendChild(input);
    card._files = [];

    const isMerge = () => vopSel.value === "merge";

    function renderFilelist() {
      filelist.hidden = !isMerge() || !card._files.length;
      filelist.textContent = "";
      card._files.forEach((f, i) => {
        const li = document.createElement("li");
        const span = document.createElement("span");
        span.textContent = `${i + 1}. ${f.name}（${fmtSize(f.size)}）`;
        const del = document.createElement("button");
        del.type = "button";
        del.className = "chip-x";
        del.textContent = "×";
        del.title = "移除";
        del.addEventListener("click", () => { card._files.splice(i, 1); refresh(); });
        li.appendChild(span);
        li.appendChild(del);
        filelist.appendChild(li);
      });
    }

    function refresh() {
      if (isMerge()) {
        runBtn.disabled = card._files.length < 2;
        $(".fname", drop).textContent =
          card._files.length ? `已选 ${card._files.length} 段（点列表 × 可移除）` : "";
      } else {
        runBtn.disabled = !card._file;
        $(".fname", drop).textContent =
          card._file ? `${card._file.name}（${fmtSize(card._file.size)}）` : "";
      }
      renderFilelist();
    }

    const pick = files => {
      if (!files || !files.length) return;
      if (isMerge()) {
        [...files].forEach(f => { if (card._files.length < 10) card._files.push(f); });
      } else {
        card._file = files[0];
      }
      refresh();
    };

    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", () => { pick(input.files); input.value = ""; });
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", e => {
      e.preventDefault(); drop.classList.remove("over");
      pick(e.dataTransfer.files);
    });

    vopSel.addEventListener("change", () => {
      card.querySelectorAll(".vop-group").forEach(g => {
        g.hidden = g.dataset.group !== vopSel.value;
      });
      input.multiple = isMerge();
      dhint.textContent = isMerge() ? "点击或拖入多个视频（2–10 段）" : "点击或拖入视频";
      card._file = null;
      card._files = [];
      refresh();
    });

    // 初始：trim 组可见
    card.querySelectorAll(".vop-group").forEach(g => {
      g.hidden = g.dataset.group !== "trim";
    });
    refresh();
  }

  /* ==================== 图片编辑卡 ==================== */

  const GEOM = new Set(["crop", "resize", "rotate", "flip"]);
  const MAX_STORE = 2048; // 预览 backing store 长边上限（仅显示缩放，原图照样上传）
  // orig/base 两张全尺寸画布 = 像素量×4B×2；超 40MP 光画布就吃 320MB+，直接拒
  const MAX_EDIT_PIXELS = 40e6;

  function buildImage(card, fd) {
    const ed = card._ed;
    if (!ed || !ed.img) return "请先选择图片";
    const ops = [...ed.ops, {
      type: "format",
      fmt: $(".ie-fmt", card).value,
      quality: parseInt($(".ie-quality", card).value, 10),
    }];
    fd.append("ops", JSON.stringify(ops));
    return null;
  }

  function newSize(op, w, h) {
    if (op.type === "crop") return [op.w, op.h];
    if (op.type === "resize") {
      if (op.scale) return [Math.max(1, Math.round(w * op.scale)),
                            Math.max(1, Math.round(h * op.scale))];
      const nw = op.w || Math.round(w * op.h / h);
      const nh = op.h || Math.round(h * op.w / w);
      return [nw, nh];
    }
    if (op.type === "rotate") return op.deg === 180 ? [w, h] : [h, w];
    return [w, h]; // flip
  }

  // 点 (x,y) 过几何 op（op 施加前图像为 w×h），返回 [x', y']
  function transformPt(x, y, op, w, h) {
    if (op.type === "crop") return [x - op.x, y - op.y];
    if (op.type === "resize") {
      const [nw, nh] = newSize(op, w, h);
      return [x * nw / w, y * nh / h];
    }
    if (op.type === "rotate") {
      if (op.deg === 90) return [h - 1 - y, x];
      if (op.deg === 180) return [w - 1 - x, h - 1 - y];
      return [y, w - 1 - x]; // 270
    }
    return op.mode === "h" ? [w - 1 - x, y] : [x, h - 1 - y];
  }

  // 几何 op 入列时，已存在的 overlay 坐标同步过变换——
  // 与后端"ops 按序应用"语义严格一致（关键不变式）
  function transformOverlay(o, gop, w, h) {
    if (o.type === "mosaic") {
      const [ax, ay] = transformPt(o.x, o.y, gop, w, h);
      const [bx, by] = transformPt(o.x + o.w, o.y + o.h, gop, w, h);
      o.x = Math.min(ax, bx); o.y = Math.min(ay, by);
      o.w = Math.abs(bx - ax); o.h = Math.abs(by - ay);
    } else if (o.type === "pen") {
      o.points = o.points.map(([px, py]) => transformPt(px, py, gop, w, h));
    } else if (o.type === "text") {
      [o.x, o.y] = transformPt(o.x, o.y, gop, w, h);
    }
  }

  function applyGeomCanvas(src, op) {
    const c = document.createElement("canvas");
    if (op.type === "crop") {
      c.width = op.w; c.height = op.h;
      c.getContext("2d").drawImage(src, op.x, op.y, op.w, op.h, 0, 0, op.w, op.h);
    } else if (op.type === "resize") {
      const [w, h] = newSize(op, src.width, src.height);
      c.width = w; c.height = h;
      c.getContext("2d").drawImage(src, 0, 0, w, h);
    } else if (op.type === "rotate") {
      const swap = op.deg !== 180;
      c.width = swap ? src.height : src.width;
      c.height = swap ? src.width : src.height;
      const ctx = c.getContext("2d");
      ctx.save();
      if (op.deg === 90) { ctx.translate(c.width, 0); ctx.rotate(Math.PI / 2); }
      else if (op.deg === 180) { ctx.translate(c.width, c.height); ctx.rotate(Math.PI); }
      else { ctx.translate(0, c.height); ctx.rotate(-Math.PI / 2); }
      ctx.drawImage(src, 0, 0);
      ctx.restore();
    } else { // flip
      c.width = src.width; c.height = src.height;
      const ctx = c.getContext("2d");
      ctx.save();
      ctx.translate(op.mode === "h" ? c.width : 0, op.mode === "v" ? c.height : 0);
      ctx.scale(op.mode === "h" ? -1 : 1, op.mode === "v" ? -1 : 1);
      ctx.drawImage(src, 0, 0);
      ctx.restore();
    }
    return c;
  }

  function initImageCard(card, runBtn) {
    const drop = $(".drop", card);
    const iedit = $(".iedit", card);
    const canvas = $(".preview", card);
    const opslist = $(".opslist", card);
    const colorEl = $(".ie-color", card);
    const widthEl = $(".ie-width", card);
    const textEl = $(".ie-text", card);
    const tsizeEl = $(".ie-tsize", card);
    const outlineEl = $(".ie-outline", card);
    const qv = $(".ie-qv", card);
    const qualityEl = $(".ie-quality", card);

    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.hidden = true;
    card.appendChild(input);

    const ed = card._ed = {
      img: null, orig: null, base: null,
      natW: 0, natH: 0, curW: 0, curH: 0, zoom: 1,
      ops: [], tool: "crop", draft: null, stroke: null,
    };

    /* ---------- 画布尺寸与绘制 ---------- */
    function sizeCanvas() {
      ed.zoom = Math.min(1, MAX_STORE / Math.max(ed.curW, ed.curH));
      canvas.width = Math.max(1, Math.round(ed.curW * ed.zoom));
      canvas.height = Math.max(1, Math.round(ed.curH * ed.zoom));
    }

    function recomputeBase() {
      let src = ed.orig;
      ed.ops.forEach(op => {
        if (GEOM.has(op.type)) src = applyGeomCanvas(src, op);
      });
      ed.base = src;
    }

    function drawPenPath(ctx, pts, z, color, width) {
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, width * z);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      pts.forEach(([px, py], i) => {
        if (i === 0) ctx.moveTo(px * z, py * z);
        else ctx.lineTo(px * z, py * z);
      });
      ctx.stroke();
    }

    function dashedRect(ctx, x, y, w, h, fill) {
      ctx.save();
      if (fill) { ctx.fillStyle = fill; ctx.fillRect(x, y, w, h); }
      ctx.strokeStyle = "#e8c987";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    }

    function drawOverlay(ctx, o, z) {
      if (o.type === "mosaic") {
        // 真马赛克预览：区域缩到 1/block 再最近邻放大
        const tw = Math.max(1, Math.round(o.w / o.block));
        const th = Math.max(1, Math.round(o.h / o.block));
        const tiny = document.createElement("canvas");
        tiny.width = tw; tiny.height = th;
        tiny.getContext("2d").drawImage(ed.base, o.x, o.y, o.w, o.h, 0, 0, tw, th);
        ctx.save();
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(tiny, 0, 0, tw, th, o.x * z, o.y * z, o.w * z, o.h * z);
        ctx.restore();
        dashedRect(ctx, o.x * z, o.y * z, o.w * z, o.h * z, null);
      } else if (o.type === "pen") {
        drawPenPath(ctx, o.points, z, o.color, o.width);
      } else if (o.type === "text") {
        ctx.save();
        ctx.font = `${Math.max(6, o.size * z)}px "Microsoft YaHei", sans-serif`;
        ctx.textBaseline = "top";
        if (o.outline > 0) {
          ctx.strokeStyle = o.outline_color;
          ctx.lineWidth = Math.max(1, o.outline * z * 2);
          ctx.strokeText(o.text, o.x * z, o.y * z);
        }
        ctx.fillStyle = o.color;
        ctx.fillText(o.text, o.x * z, o.y * z);
        ctx.restore();
      }
    }

    function redraw() {
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(ed.base, 0, 0, canvas.width, canvas.height);
      ed.ops.forEach(o => { if (!GEOM.has(o.type)) drawOverlay(ctx, o, ed.zoom); });
      if (ed.stroke) {
        drawPenPath(ctx, ed.stroke.points, ed.zoom,
                    ed.stroke.color, ed.stroke.width);
      }
      if (ed.draft) {
        dashedRect(ctx, ed.draft.x * ed.zoom, ed.draft.y * ed.zoom,
                   ed.draft.w * ed.zoom, ed.draft.h * ed.zoom,
                   "rgba(201,160,90,.12)");
      }
    }

    const DESCR = {
      crop: o => `裁剪 ${o.w}×${o.h}`,
      resize: o => `缩放 ${Math.round((o.scale || 1) * 100)}%`,
      rotate: o => `旋转 ${o.deg}°`,
      flip: o => (o.mode === "h" ? "水平翻转" : "垂直翻转"),
      mosaic: o => `马赛克 ${Math.round(o.w)}×${Math.round(o.h)}`,
      pen: o => `涂鸦（${o.points.length} 点）`,
      text: o => `文字「${o.text.slice(0, 12)}」`,
    };

    function renderOps() {
      opslist.textContent = "";
      ed.ops.forEach((o, i) => {
        const li = document.createElement("li");
        const span = document.createElement("span");
        span.textContent = (DESCR[o.type] || (() => o.type))(o);
        li.appendChild(span);
        if (!GEOM.has(o.type)) {
          const del = document.createElement("button");
          del.type = "button";
          del.className = "chip-x";
          del.textContent = "×";
          del.title = "删除（几何操作请用「清空操作」重来）";
          del.addEventListener("click", () => {
            ed.ops.splice(i, 1);
            redraw(); renderOps();
          });
          li.appendChild(del);
        }
        opslist.appendChild(li);
      });
      opslist.hidden = ed.ops.length === 0;
    }

    function pushGeometric(op) {
      const w = ed.curW, h = ed.curH;
      ed.ops.forEach(o => { if (!GEOM.has(o.type)) transformOverlay(o, op, w, h); });
      ed.ops.push(op);
      [ed.curW, ed.curH] = newSize(op, w, h);
      recomputeBase(); sizeCanvas(); redraw(); renderOps();
    }

    /* ---------- 指针交互 ---------- */
    function toNat(e) {
      const r = canvas.getBoundingClientRect();
      return {
        x: (e.clientX - r.left) * ed.curW / r.width,
        y: (e.clientY - r.top) * ed.curH / r.height,
      };
    }

    function clampNum(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

    function placeText(p) {
      const text = textEl.value.trim();
      if (!text) { fail(card, "先在工具条输入水印文字，再点画布落字"); return; }
      ed.ops.push({
        type: "text", text, x: Math.round(p.x), y: Math.round(p.y),
        size: clampNum(parseInt(tsizeEl.value, 10) || 32, 8, 200),
        color: colorEl.value,
        outline: clampNum(parseInt(outlineEl.value, 10) || 0, 0, 10),
        outline_color: "#000000",
      });
      redraw(); renderOps();
    }

    canvas.addEventListener("pointerdown", e => {
      if (!ed.img) return;
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      const p = toNat(e);
      if (ed.tool === "crop" || ed.tool === "mosaic") {
        ed.draft = { x0: p.x, y0: p.y, x: p.x, y: p.y, w: 0, h: 0 };
      } else if (ed.tool === "pen") {
        ed.stroke = {
          color: colorEl.value,
          width: clampNum(parseInt(widthEl.value, 10) || 6, 1, 60),
          points: [[p.x, p.y]],
        };
      } else if (ed.tool === "text") {
        placeText(p);
      }
    });

    canvas.addEventListener("pointermove", e => {
      if (!ed.draft && !ed.stroke) return;
      const p = toNat(e);
      if (ed.draft) {
        ed.draft.x = Math.min(ed.draft.x0, p.x);
        ed.draft.y = Math.min(ed.draft.y0, p.y);
        ed.draft.w = Math.abs(p.x - ed.draft.x0);
        ed.draft.h = Math.abs(p.y - ed.draft.y0);
        redraw();
      } else if (ed.stroke) {
        const last = ed.stroke.points[ed.stroke.points.length - 1];
        const minDist = 2 / ed.zoom; // 最小间距 2 CSS px
        if (Math.hypot(p.x - last[0], p.y - last[1]) >= minDist
            && ed.stroke.points.length < 5000) {
          ed.stroke.points.push([p.x, p.y]);
          redraw();
        }
      }
    });

    canvas.addEventListener("pointerup", e => {
      if (ed.draft) {
        const d = ed.draft;
        ed.draft = null;
        const p = toNat(e);
        const x1 = clampNum(Math.round(Math.min(d.x0, p.x)), 0, ed.curW);
        const y1 = clampNum(Math.round(Math.min(d.y0, p.y)), 0, ed.curH);
        const x2 = clampNum(Math.round(Math.max(d.x0, p.x)), 0, ed.curW);
        const y2 = clampNum(Math.round(Math.max(d.y0, p.y)), 0, ed.curH);
        const w = x2 - x1, h = y2 - y1;
        if (w >= 4 && h >= 4) {
          if (ed.tool === "crop") {
            pushGeometric({ type: "crop", x: x1, y: y1, w, h });
          } else {
            ed.ops.push({ type: "mosaic", x: x1, y: y1, w, h, block: 12 });
            redraw(); renderOps();
          }
        } else {
          redraw();
        }
      } else if (ed.stroke) {
        const s = ed.stroke;
        ed.stroke = null;
        if (s.points.length >= 2) {
          ed.ops.push({
            type: "pen", color: s.color, width: s.width,
            points: s.points.map(([px, py]) => [Math.round(px), Math.round(py)]),
          });
        }
        redraw(); renderOps();
      }
    });

    /* ---------- 工具条 ---------- */
    card.querySelectorAll("[data-tool]").forEach(btn => {
      btn.addEventListener("click", () => {
        ed.tool = btn.dataset.tool;
        card.querySelectorAll("[data-tool]").forEach(b => b.classList.toggle("on", b === btn));
      });
    });
    card.querySelectorAll("[data-act]").forEach(btn => {
      btn.addEventListener("click", () => {
        if (!ed.img) return;
        const act = btn.dataset.act;
        if (act === "rotate") pushGeometric({ type: "rotate", deg: 90 });
        else if (act === "fliph") pushGeometric({ type: "flip", mode: "h" });
        else if (act === "flipv") pushGeometric({ type: "flip", mode: "v" });
        else if (act === "resize") {
          const pct = clampNum(parseFloat($(".ie-scale", card).value) || 100, 1, 1000);
          pushGeometric({ type: "resize", scale: pct / 100 });
        } else if (act === "clear") {
          ed.ops = [];
          ed.curW = ed.natW; ed.curH = ed.natH;
          recomputeBase(); sizeCanvas(); redraw(); renderOps();
        }
      });
    });
    qualityEl.addEventListener("input", () => { qv.textContent = qualityEl.value; });

    /* ---------- 图片载入 ---------- */
    const pick = file => {
      if (!file) return;
      card._file = file;
      $(".fname", drop).textContent = `${file.name}（${fmtSize(file.size)}）`;
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        ed.img = img;
        // naturalWidth 是浏览器按 EXIF 转正后的尺寸，与后端 exif_transpose 对齐
        ed.natW = img.naturalWidth;
        ed.natH = img.naturalHeight;
        if (ed.natW * ed.natH > MAX_EDIT_PIXELS) {
          card._file = null;
          ed.img = null;
          fail(card, `图片过大（${Math.round(ed.natW * ed.natH / 1e6)}MP > 40MP），请先缩小再编辑`);
          return;
        }
        ed.curW = ed.natW;
        ed.curH = ed.natH;
        const oc = document.createElement("canvas");
        oc.width = ed.natW;
        oc.height = ed.natH;
        oc.getContext("2d").drawImage(img, 0, 0);
        ed.orig = oc;
        ed.ops = [];
        ed.draft = null;
        ed.stroke = null;
        recomputeBase(); sizeCanvas(); redraw(); renderOps();
        iedit.hidden = false;
        runBtn.disabled = false;
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        fail(card, "图片无法解析");
      };
      img.src = url;
    };

    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", () => { pick(input.files[0]); input.value = ""; });
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", e => {
      e.preventDefault(); drop.classList.remove("over");
      pick(e.dataTransfer.files[0]);
    });
  }
})();
