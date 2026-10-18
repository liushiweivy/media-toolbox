/* 媒体工具箱页面冒烟：13 卡渲染、引擎徽标、选项填充、真走翻译流程、
 * TTS 流程出播放器、视频剪辑卡参数组切换、图片编辑卡画布+裁剪流程、
 * 移动端无横溢、零 console 错误。
 * 用法：node tools/cdp-check.mjs（需 9333 无头 Edge + 8840 服务）
 * 产出 docs/page-*.png。 */
import { writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const CDP = "http://127.0.0.1:9333";
const URL = "http://127.0.0.1:8840/";
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, "docs");
await mkdir(OUT, { recursive: true });

const target = await (await fetch(CDP + "/json/new?about:blank", { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));

let mid = 0;
const pending = new Map();
const pageErrors = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") {
    pageErrors.push(m.params.exceptionDetails.exception?.description || JSON.stringify(m.params.exceptionDetails));
  }
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
    pageErrors.push("console: " + m.params.entry.text);
  }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++mid;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
async function evalJs(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, label, timeout = 30000, interval = 400) {
  const deadline = Date.now() + timeout;
  let value = false;
  while (Date.now() < deadline) {
    value = await fn();
    if (value) return value;
    await sleep(interval);
  }
  console.log("TIMEOUT waiting:", label, "last:", value);
  return false;
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  await writeFile(join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("shot", name);
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  console.log((ok ? "PASS" : "FAIL"), name, detail);
}

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: URL });

/* 1. 十三卡渲染 */
await waitFor(() => evalJs(`document.querySelectorAll(".card").length === 13`), "13 cards");
check("cards-13", await evalJs(`document.querySelectorAll(".card").length`) === 13);
const titles = await evalJs(`[...document.querySelectorAll(".card h2")].map(h => h.textContent)`);
check("titles", JSON.stringify(titles).includes("视频提取音频") && JSON.stringify(titles).includes("语音翻译")
  && JSON.stringify(titles).includes("YouTube 转文字"));

/* 2. 引擎徽标 + 选项填充 */
await waitFor(() => evalJs(`document.getElementById("engine").textContent.includes("STT")`), "engine badge");
check("engine-badge", await evalJs(`document.getElementById("engine").textContent.includes("STT")`),
  await evalJs(`document.getElementById("engine").textContent`));
check("voice-options", await evalJs(`document.querySelector('[data-kind="tts"] select[data-opt="voice"]').options.length`) >= 8);
check("target-options", await evalJs(`document.querySelectorAll('select[data-opt="target"]').length`) === 3
  && await evalJs(`document.querySelector('select[data-opt="target"]').options.length`) >= 8);
check("subtitle-source-options", await evalJs(
  `document.querySelector('[data-kind="video-subtitle"] select[data-opt="source"]').options.length`) === 5);

/* 3. 上传卡初始按钮禁用，文本卡可用 */
check("upload-btn-disabled", await evalJs(`document.querySelector('[data-kind="ocr"] .btn.run').disabled`));
check("text-btn-enabled", !(await evalJs(`document.querySelector('[data-kind="translate"] .btn.run').disabled`)));

/* 4. 空文本提交要报错提示 */
await evalJs(`document.querySelector('[data-kind="translate"] .btn.run').click()`);
await sleep(400);
check("empty-text-guard", await evalJs(`document.querySelector('[data-kind="translate"] .msg.err').textContent.includes("请输入文字")`));

/* 5. 真走翻译流程 */
await evalJs(`(() => {
  const c = document.querySelector('[data-kind="translate"]');
  c.querySelector('textarea').value = "人工智能让媒体处理更简单。";
  c.querySelector('select[data-opt="target"]').value = "en";
  c.querySelector(".btn.run").click();
})()`);
const trOk = await waitFor(() => evalJs(`(() => {
  const c = document.querySelector('[data-kind="translate"]');
  const pre = c.querySelector("pre");
  return c.querySelector(".result").classList.contains("on") && !pre.hidden && pre.textContent.length > 5;
})()`), "translate result", 60000, 1000);
check("translate-flow", trOk,
  await evalJs(`document.querySelector('[data-kind="translate"] pre').textContent.slice(0, 60)`));
check("translate-copy-btn", await evalJs(`[...document.querySelectorAll('[data-kind="translate"] .dls .btn')].some(b => b.textContent.includes("复制"))`));

/* 6. 真走 TTS 流程出播放器 */
await evalJs(`(() => {
  const c = document.querySelector('[data-kind="tts"]');
  c.querySelector("textarea").value = "页面冒烟测试语音。";
  c.querySelector(".btn.run").click();
})()`);
const ttsOk = await waitFor(() => evalJs(`!!document.querySelector('[data-kind="tts"] audio')`), "tts audio", 60000, 1000);
check("tts-flow-audio", ttsOk);
check("tts-download", await evalJs(`document.querySelectorAll('[data-kind="tts"] .dls a').length`) >= 1);

/* 7. 视频剪辑卡：六操作 + 参数组切换 */
check("video-vop-options", await evalJs(
  `document.querySelector('[data-kind="video-edit"] select[data-opt="vop"]').options.length`) === 6);
check("video-trim-visible", await evalJs(`(() => {
  const c = document.querySelector('[data-kind="video-edit"]');
  const vis = el => getComputedStyle(el).display !== "none";
  return vis(c.querySelector('[data-group="trim"]'))
    && !vis(c.querySelector('[data-group="merge"]'));
})()`));
await evalJs(`(() => {
  const c = document.querySelector('[data-kind="video-edit"]');
  const s = c.querySelector('select[data-opt="vop"]');
  s.value = "merge";
  s.dispatchEvent(new Event("change"));
})()`);
await sleep(300);
check("video-merge-group", await evalJs(`(() => {
  const c = document.querySelector('[data-kind="video-edit"]');
  const vis = el => getComputedStyle(el).display !== "none";
  return !vis(c.querySelector('[data-group="trim"]'))
    && vis(c.querySelector('[data-group="merge"]'))
    && c.querySelector('input[type="file"]').multiple
    && c.querySelector(".dhint").textContent.includes("多个视频");
})()`));
// 切回 trim 供截图观感
await evalJs(`(() => {
  const c = document.querySelector('[data-kind="video-edit"]');
  const s = c.querySelector('select[data-opt="vop"]');
  s.value = "trim";
  s.dispatchEvent(new Event("change"));
})()`);

/* 8. 图片编辑卡：喂真图 → 画布出现 → 拖框裁剪 → 导出下载 */
const FIX_PNG = join(ROOT, "tests", "fixtures", "ocr.png");
if (existsSync(FIX_PNG)) {
  check("image-iedit-hidden-initial", await evalJs(
    `getComputedStyle(document.querySelector('[data-kind="image-edit"] .iedit')).display === "none"`));
  const doc = await send("DOM.getDocument", { depth: -1 });
  const node = await send("DOM.querySelector", {
    nodeId: doc.result.root.nodeId,
    selector: '[data-kind="image-edit"] input[type="file"]',
  });
  await send("DOM.setFileInputFiles", { files: [FIX_PNG], nodeId: node.result.nodeId });
  await evalJs(`(() => {
    const i = document.querySelector('[data-kind="image-edit"] input[type="file"]');
    i.dispatchEvent(new Event("change"));
  })()`);
  const canvasUp = await waitFor(() => evalJs(`(() => {
    const cv = document.querySelector('[data-kind="image-edit"] canvas.preview');
    return cv && cv.width > 0 && !document.querySelector('[data-kind="image-edit"] .iedit').hidden;
  })()`), "image canvas", 15000);
  check("image-canvas-ready", canvasUp);
  // 模拟拖框裁剪（画布左上 1/4 区域）
  await evalJs(`(() => {
    const cv = document.querySelector('[data-kind="image-edit"] canvas.preview');
    const r = cv.getBoundingClientRect();
    const opts = (x, y) => ({ bubbles: true, pointerId: 1, clientX: r.left + x, clientY: r.top + y });
    cv.dispatchEvent(new PointerEvent("pointerdown", opts(r.width * 0.1, r.height * 0.1)));
    cv.dispatchEvent(new PointerEvent("pointermove", opts(r.width * 0.6, r.height * 0.6)));
    cv.dispatchEvent(new PointerEvent("pointerup", opts(r.width * 0.6, r.height * 0.6)));
  })()`);
  await sleep(300);
  check("image-crop-op", await evalJs(`(() => {
    const c = document.querySelector('[data-kind="image-edit"]');
    const ops = c._ed ? c._ed.ops : [];
    return ops.length === 1 && ops[0].type === "crop"
      && !c.querySelector(".opslist").hidden;
  })()`));
  // 真走导出流程（后端 Pillow job）
  await evalJs(`document.querySelector('[data-kind="image-edit"] .btn.run').click()`);
  const imgDone = await waitFor(() => evalJs(`(() => {
    const c = document.querySelector('[data-kind="image-edit"]');
    return c.querySelector(".result").classList.contains("on")
      && c.querySelectorAll(".dls a").length >= 1;
  })()`), "image export", 60000, 1000);
  check("image-export-flow", imgDone,
    await evalJs(`document.querySelector('[data-kind="image-edit"] .msg').textContent`));
} else {
  console.log("SKIP image card flow (tests/fixtures/ocr.png 不存在，先跑 pytest 造样)");
}

await shot("page-desktop.png");

/* 9. 移动端无横向溢出 */
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await sleep(500);
check("mobile-no-overflow", await evalJs(`document.documentElement.scrollWidth <= 390`));
await shot("page-mobile.png");

/* 10. 零 console 错误 */
check("zero-console-errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 200));

const passed = results.filter(r => r.ok).length;
console.log(`\n${passed}/${results.length} checks passed`);
await fetch(CDP + "/json/close/" + target.id);
ws.close();
process.exitCode = passed === results.length ? 0 : 1;
