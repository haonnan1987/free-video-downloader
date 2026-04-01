const $ = (id) => document.getElementById(id);

const hint = $("hint-msg");
const form = $("form-resolve");
const input = $("input-url");
const grid = $("result-grid");
const cardThumb = $("card-thumb");
const cardTitle = $("card-title");
const cardMeta = $("card-meta");
const cardTags = $("card-tags");
const cardDuration = $("card-duration");
const selectFmt = $("select-format");
const selectEnc = $("select-encoding");
const btnDl = $("btn-download");
const btnDlLabel = $("btn-download-label");
const sizeFull = $("size-full");
const segBtns = () => document.querySelectorAll(".format-seg .seg-btn");
const dlStatus = $("download-status");
const drawer = $("drawer");
const btnResolve = $("btn-resolve");

let lastUrl = "";
let resolvedFormats = [];
let lastResolveData = null;
let mediaMode = "original";
let thumbBlobUrl = null;

/** 从整段分享文案中提取首个 http(s) 链接（去掉末尾标点、括号等） */
function extractHttpUrlFromText(text) {
  if (!text || typeof text !== "string") return "";
  const raw = text.trim();
  if (!raw) return "";
  const re = /https?:\/\/[^\s\u3000]+/gi;
  const matches = raw.match(re);
  if (!matches || !matches.length) return raw;
  let url = matches.reduce((a, b) => (b.length > a.length ? b : a));
  const punct = "，。！？、；;．";
  const brackets = "】」』）)\"'";
  for (;;) {
    let next = url.replace(new RegExp(`[${punct}]+$`, "u"), "");
    while (next.length) {
      const ch = next[next.length - 1];
      if (brackets.includes(ch) || ch === "]" || ch === ")") next = next.slice(0, -1);
      else break;
    }
    if (next === url) break;
    url = next;
  }
  return url.trim();
}

function humanFilesizeMB(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "—";
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function allowedKindsForMode(mode) {
  if (mode === "audio_only") return ["audio", "muxed"];
  if (mode === "video_only") return ["video", "muxed"];
  return null;
}

function syncDownloadButtonLabel() {
  if (!btnDlLabel) return;
  if (mediaMode === "audio_only") btnDlLabel.textContent = "下载音频";
  else if (mediaMode === "video_only") btnDlLabel.textContent = "下载视频";
  else btnDlLabel.textContent = "下载完整视频";
}

function updateSizeDisplays() {
  if (!sizeFull) return;
  const opt = selectFmt?.selectedOptions?.[0];
  const fromSel = opt?.dataset?.filesize != null ? Number(opt.dataset.filesize) : null;
  const fallback = lastResolveData?.filesize_approx;
  const bytes =
    fromSel != null && Number.isFinite(fromSel) && fromSel > 0 ? fromSel : fallback;
  sizeFull.textContent = humanFilesizeMB(bytes);
}

function revokeThumbBlob() {
  if (thumbBlobUrl) {
    try {
      URL.revokeObjectURL(thumbBlobUrl);
    } catch {
      /* ignore */
    }
    thumbBlobUrl = null;
  }
}

/** 与后端 _is_xhs_thumbnail_cdn_host 对齐：picasso-static / qimg / xhscdn.net 等需走 /api/thumb */
function _isXhsThumbCdnHost(hostname) {
  const h = (hostname || "").toLowerCase();
  if (!h || !h.includes(".")) return false;
  if (
    h.endsWith(".xhscdn.com") ||
    h === "xhscdn.com" ||
    h.endsWith(".xhscdn.net") ||
    h === "xhscdn.net"
  )
    return true;
  if (!h.endsWith(".xiaohongshu.com")) return false;
  if (h === "www.xiaohongshu.com" || h === "xiaohongshu.com") return false;
  const sub0 = h.split(".")[0];
  const known = new Set([
    "picasso-static",
    "qimg",
    "fe-static",
    "ci",
    "edith",
    "sns-avatar-qc",
  ]);
  if (known.has(sub0)) return true;
  return /^(picasso|sns-|fe-|lf-|apm-)/.test(sub0);
}

function _needsHotlinkThumbProxy(url) {
  if (typeof url !== "string") return false;
  try {
    const h = new URL(url.startsWith("//") ? `https:${url}` : url).hostname.toLowerCase();
    const igFbcdn = h.endsWith(".fbcdn.net") && h.includes("instagram");
    return (
      h.endsWith(".hdslb.com") ||
      h.endsWith(".biliimg.com") ||
      h.includes("douyinpic.com") ||
      h.endsWith(".twimg.com") ||
      h === "twimg.com" ||
      h.includes("cdninstagram.com") ||
      igFbcdn ||
      (h.endsWith(".byteimg.com") && url.toLowerCase().includes("douyin")) ||
      _isXhsThumbCdnHost(h)
    );
  } catch {
    return /hdslb\.com|biliimg\.com|douyinpic\.com|twimg\.com|cdninstagram\.com|instagram.*fbcdn|xhscdn\.(com|net)|picasso-static\.|\.qimg\.|sns-/i.test(
      url
    );
  }
}

/** TikTok 等：POST /api/thumb；B 站/小红书 CDN：浏览器直连常被 Referer 拦截，走代拉 */
async function applyResultThumbnail(data) {
  revokeThumbBlob();
  const proxyTarget = data.thumbnail_proxy_url;
  let direct = data.thumbnail;

  const urlToProxy = proxyTarget || (_needsHotlinkThumbProxy(direct) ? direct : null);

  const setFromBlobRes = async (res) => {
    if (!res.ok) throw new Error("thumb proxy failed");
    const blob = await res.blob();
    thumbBlobUrl = URL.createObjectURL(blob);
    cardThumb.src = thumbBlobUrl;
    cardThumb.hidden = false;
  };

  if (urlToProxy) {
    try {
      const res = await fetch("/api/thumb", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: urlToProxy }),
      });
      await setFromBlobRes(res);
    } catch {
      if (urlToProxy.length < 8000) {
        try {
          const g = await fetch(
            "/api/thumb?url=" + encodeURIComponent(urlToProxy),
            { method: "GET" }
          );
          await setFromBlobRes(g);
          return;
        } catch {
          /* fall through */
        }
      }
      if (direct && !proxyTarget) {
        cardThumb.src = direct.startsWith("//") ? `https:${direct}` : direct;
        cardThumb.hidden = false;
      } else if (proxyTarget && !direct?.startsWith?.("data:")) {
        cardThumb.src = proxyTarget.startsWith("//") ? `https:${proxyTarget}` : proxyTarget;
        cardThumb.hidden = false;
      } else {
        cardThumb.removeAttribute("src");
        cardThumb.hidden = true;
      }
    }
    return;
  }

  if (direct) {
    cardThumb.src = direct.startsWith("//") ? `https:${direct}` : direct;
    cardThumb.hidden = false;
    return;
  }

  cardThumb.removeAttribute("src");
  cardThumb.hidden = true;
}

function apiErrorDetail(data) {
  const d = data?.detail;
  if (d == null) return "请求失败";
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((x) => (typeof x === "object" && x?.msg ? x.msg : String(x)))
      .join("；");
  }
  return String(d);
}

function setHint(text, isError = false) {
  hint.textContent = text;
  hint.classList.toggle("error", isError);
}

function fmtDuration(sec) {
  if (sec == null || Number.isNaN(sec)) return "";
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  const parts = [
    h > 0 ? String(h).padStart(2, "0") : null,
    String(m).padStart(2, "0"),
    String(s).padStart(2, "0"),
  ].filter(Boolean);
  return parts.join(":");
}

function renderTags(data) {
  cardTags.replaceChildren();
  const tags = [];
  if (data.extractor) tags.push(data.extractor);
  for (const t of tags) {
    const span = document.createElement("span");
    span.className = "tag";
    span.textContent = t;
    cardTags.appendChild(span);
  }

  cardDuration.textContent = data.duration ? fmtDuration(data.duration) : "";
}

function fillFormats(formats) {
  selectFmt.replaceChildren();
  resolvedFormats = formats || [];
  const kinds = allowedKindsForMode(mediaMode);
  let added = 0;
  for (const f of resolvedFormats) {
    if (kinds && f.kind && !kinds.includes(f.kind)) continue;
    const opt = document.createElement("option");
    opt.value = f.format_id;
    opt.textContent = f.label || f.format_id;
    if (f.filesize != null) opt.dataset.filesize = String(f.filesize);
    selectFmt.appendChild(opt);
    added += 1;
  }
  if (!added && resolvedFormats.length) {
    const opt = document.createElement("option");
    opt.value = "best";
    opt.textContent = "默认";
    selectFmt.appendChild(opt);
  }
  updateSizeDisplays();
}

if (input) {
  input.addEventListener("focus", () => {
    requestAnimationFrame(() => {
      if (input.value) input.select();
    });
  });

  input.addEventListener("paste", (e) => {
    const text = e.clipboardData?.getData("text/plain") || "";
    if (!text) return;
    const extracted = extractHttpUrlFromText(text);
    if (!/^https?:\/\//i.test(extracted)) return;
    const clip = text.trim();
    if (extracted === clip) return;
    e.preventDefault();
    const s = input.selectionStart ?? 0;
    const end = input.selectionEnd ?? 0;
    input.value = `${input.value.slice(0, s)}${extracted}${input.value.slice(end)}`;
    const pos = s + extracted.length;
    requestAnimationFrame(() => input.setSelectionRange(pos, pos));
  });

  input.addEventListener("blur", () => {
    const v = input.value.trim();
    if (!v) return;
    const x = extractHttpUrlFromText(v);
    if (x && x !== v && /^https?:\/\//i.test(x)) input.value = x;
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const cleaned = extractHttpUrlFromText(input.value.trim());
  if (cleaned) input.value = cleaned;
  const url = input.value.trim();
  if (!url) return;
  setHint("正在解析，请稍候…");
  btnResolve.disabled = true;
  btnResolve.classList.add("loading");
  try {
    const res = await fetch("/api/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setHint(apiErrorDetail(data), true);
      grid.hidden = true;
      return;
    }
    lastUrl = url;
    lastResolveData = data;
    mediaMode = "original";
    segBtns().forEach((b) => b.classList.toggle("active", b.dataset.mode === "original"));
    syncDownloadButtonLabel();

    cardTitle.textContent = data.title || "未命名";
    cardMeta.textContent = data.webpage_url || url;
    await applyResultThumbnail(data);
    renderTags(data);
    fillFormats(data.formats);
    grid.hidden = false;
    dlStatus.textContent = "";
    setHint("");
  } catch {
    setHint("网络错误，请稍后重试。", true);
    grid.hidden = true;
  } finally {
    btnResolve.disabled = false;
    btnResolve.classList.remove("loading");
  }
});

document.querySelector(".format-seg")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn || !btn.dataset.mode) return;
  mediaMode = btn.dataset.mode;
  segBtns().forEach((b) => b.classList.toggle("active", b === btn));
  syncDownloadButtonLabel();
  fillFormats(lastResolveData?.formats || []);
});

selectFmt.addEventListener("change", () => updateSizeDisplays());

async function pollJob(jobId) {
  const started = Date.now();
  const maxMs = 60 * 60 * 1000;
  while (Date.now() - started < maxMs) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(apiErrorDetail(data));
    if (data.status === "completed") return data;
    if (data.status === "failed") throw new Error(data.error || "下载失败");
    dlStatus.textContent = "正在下载，请稍候…";
    await new Promise((r) => setTimeout(r, 1200));
  }
  throw new Error("等待超时");
}

btnDl.addEventListener("click", async () => {
  if (!lastUrl) {
    setHint("请先解析链接。", true);
    return;
  }
  btnDl.disabled = true;
  dlStatus.classList.remove("bad");
  dlStatus.classList.add("busy");
  dlStatus.textContent = "正在创建任务…";
  try {
    const format_id = selectFmt.value || "best";
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: lastUrl,
        format_id,
        media_mode: mediaMode,
        encoding: selectEnc.value || "auto",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(apiErrorDetail(data));
    const jobId = data.job_id;
    await pollJob(jobId);
    dlStatus.classList.remove("busy");
    dlStatus.textContent = "完成，正在保存文件…";
    window.location.assign(`/api/jobs/${jobId}/file`);
  } catch (e) {
    dlStatus.classList.remove("busy");
    dlStatus.classList.add("bad");
    dlStatus.textContent = e.message || "下载失败";
  } finally {
    btnDl.disabled = false;
  }
});

$("btn-about").addEventListener("click", () => {
  drawer.hidden = false;
});

$("drawer-close").addEventListener("click", () => {
  drawer.hidden = true;
});

drawer.addEventListener("click", (e) => {
  if (e.target.classList.contains("drawer-backdrop")) drawer.hidden = true;
});

input.addEventListener("paste", () => {
  setTimeout(() => {
    if (input.value.trim()) form.requestSubmit();
  }, 50);
});
