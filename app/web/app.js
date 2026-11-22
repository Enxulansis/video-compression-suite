/* Video Compression Suite — front-end logic (vanilla ES modules, no build step) */

const $ = (id) => document.getElementById(id);

/** Last path component -- used wherever a message must name one file, not a path. */
function baseName(p) {
  return String(p || "").split(/[\\/]/).pop() || String(p || "");
}

const state = {
  caps: null,
  profiles: [],
  defaultProfile: "P3",
  profileId: "P3",
  selectionMode: "simple",
  goal: "balanced",
  manualSeed: null,
  backendSupportsSimple: false,
  backendSupportsHandoff: false,
  initializing: false,
  files: [],            // { path, name, media|null, error, seek|null }
  activeFile: null,     // path used for the command preview
  plan: null,
  stats: null,
  jobs: [],             // last known queue, used for immediate local updates
  mode: "compress",     // compress | repair
  estimate: null,
  settings: {},         // persisted backend settings (default_out_dir, location)
  needsPathFallback: false,  // browser refused to expose local paths
  history: { entries: [], stats: null, limit: 50, query: "", onlyExisting: false,
             selected: new Set() },
  targetResult: null,   // last solved target-size result (measured, not estimated)
};

const VIDEO_EXT = new Set([
  "mp4", "m4v", "mkv", "mov", "avi", "wmv", "flv", "f4v", "ts", "m2ts", "mts",
  "webm", "mpg", "mpeg", "m2v", "vob", "rm", "rmvb", "3gp", "3g2", "ogv",
  "asf", "mxf", "dv", "divx", "dat", "iso", "m2p", "tp", "trp", "amv", "y4m",
]);

/* ------------------------------------------------------------------ helpers */

function toast(text, ms = 2600) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), ms);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok || (data && data.ok === false)) {
    const error = new Error((data && data.error) || `请求失败 (${res.status})`);
    error.status = res.status;
    error.retryable = res.status === 429 || res.status === 504;
    throw error;
  }
  return data;
}

function fmtBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

// Telemetry is independent of plan invalidation and never enqueues or decodes files.
const MEMORY_REFRESH_MS = 5000;
const MEMORY_TIMEOUT_MS = 4000;
let memoryRead = null;
async function refreshMemory({ retry = true } = {}) {
  if (memoryRead) return memoryRead;
  memoryRead = (async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), MEMORY_TIMEOUT_MS);
    try {
      const data = await api('/api/memory', { signal: controller.signal });
      if (!data?.memory || !data?.policy) throw new Error('后端尚未提供实时内存状态');
      const m = data.memory, el = $('memoryStatus');
      const stamp = Number(data.sampled_at);
      const when = Number.isFinite(stamp) && stamp > 0 ? new Date(stamp * 1000).toLocaleTimeString() : '时间未知';
      const memoryAmount = mb => mb === 0 ? '0 MB' : fmtBytes(mb * 1048576);
      const commit = m.commit_limit_mb > 0
        ? ` · 提交余量 ${memoryAmount(Math.max(0, m.commit_limit_mb - m.commit_used_mb))}` : '';
      el.textContent = m.available
        ? `可用内存 ${memoryAmount(m.free_mb)}${commit} · 安全余量 ${memoryAmount(data.policy.margin_mb)} · ${when}`
        : `无法读取当前内存 · ${when}`;
      el.parentElement.classList.toggle('waiting', !data.probe_gate?.ok);
      el.title = data.probe_gate?.reason || '每 5 秒更新；入队后仍按实时内存安全检查决定何时开始';
      if (retry && data.probe_gate?.ok) {
        const waiting = state.files.filter(f => f.retryable && !f.probing).slice(0, 2);
        if (waiting.length) { probeFiles(waiting, { checkSeek: false }); return; }
      }
      // Cached calculations refresh memory warnings without resetting drafts/focus.
      if (state.mode === 'compress' && !state.measuring && !state.batchMeasuring && state.files.length && state.files.every(f => f.media && !f.probing)) {
        await loadBatchEstimate({ quiet: true });
      }
    } catch (err) {
      $('memoryStatus').textContent = '实时内存读取失败，请点击刷新重试';
      $('memoryStatus').title = err.message;
      $('memoryStatus').parentElement.classList.add('waiting');
    } finally { clearTimeout(timer); }
  })();
  try { await memoryRead; } finally { memoryRead = null; }
}

function wireMemoryStatus() {
  $('memoryRefreshBtn').addEventListener('click', () => refreshMemory());
  refreshMemory();
  setInterval(() => { if (!document.hidden) refreshMemory(); }, MEMORY_REFRESH_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshMemory(); });
}

function fmtDuration(sec) {
  if (!Number.isFinite(sec) || sec <= 0) return "未知";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function fmtClock(sec) {
  if (!sec || sec < 0 || !isFinite(sec)) return "--:--";
  const s = Math.round(sec);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function joinPath(dir, name) {
  if (!dir) return name;
  const sep = dir.includes("\\") ? "\\" : "/";
  return dir.replace(/[\\/]+$/, "") + sep + name;
}

function currentLocation() {
  const picked = document.querySelector('input[name="location"]:checked');
  return picked ? picked.value : "beside";
}

function currentQualityMode() {
  const el = $("optQualityMode");
  return el ? el.value : "crf";
}

function applyQualityModeUI() {
  const mode = currentQualityMode();
  $("crfRow").classList.toggle("hidden", mode !== "crf");
  $("targetRow").classList.toggle("hidden", mode !== "target");
  if ($("targetStrictRow")) $("targetStrictRow").classList.toggle("hidden", mode !== "target");
  // The solver only makes sense in target mode.
  $("targetSolveBtn").classList.toggle("hidden", mode !== "target");
}

function overrides() {
  const num = (id) => {
    const v = $(id).value.trim();
    return v === "" ? 0 : Number(v);
  };
  const crfRaw = $("optCrf").value.trim();
  const location = currentLocation();
  if (state.selectionMode === "simple" && state.mode === "compress") {
    return {
      location, out_dir: location === "custom" ? $("optOutDir").value.trim() : "",
      default_out_dir: $("optDefaultDir").value.trim(),
      name_template: $("optTemplate").value.trim() || "{stem}_{profile}_{height}p",
      intent: { goal: state.goal, compatibility: state.settings.compatibility || "modern",
                slow_av1: state.settings.slow_av1 === true },
    };
  }
  const ov = {
    ...(state.manualSeed || {}),
    intent: {},
    location,
    // Only the relevant field is sent; the backend also falls back safely.
    out_dir: location === "custom" ? $("optOutDir").value.trim() : "",
    default_out_dir: $("optDefaultDir").value.trim(),
    name_template: $("optTemplate").value.trim() || "{stem}_{profile}_{height}p",
    max_long_edge: num("optMaxEdge"),
    scale_percent: num("optScalePct"),
    gop_seconds: num("optGop") || 1,
    audio_mode: $("optAudio").value,
    subtitle_mode: $("optSubs").value,
    container: $("optContainer") ? $("optContainer").value : "",
    deinterlace: $("optDeint").checked,
    denoise: $("optDenoise").checked,
    autocrop: $("optAutocrop") ? $("optAutocrop").checked : false,
    hdr_to_sdr: $("optHdr").checked,
    strip_metadata: $("optStrip").checked,
    // A7: let the extreme tiers raise their own encoder effort ("auto" | "off").
    effort: $("optEffort") && $("optEffort").checked ? "auto" : "off",
    // F7: copy the source's filesystem timestamps onto the output (default on).
    keep_timestamps: $("optKeepTime") ? $("optKeepTime").checked : true,
    allow_unsafe: $("expertMode").checked,
  };
  // Loudness normalisation (G1). 0 = off; anything else is a LUFS target, and the
  // backend fills in the measured source loudness so the filter can be the linear
  // two-pass form instead of the dynamic one.
  const loud = loudnessTarget();
  if (loud < 0) {
    ov.loudness = loud;
    const tp = $("optLoudnessTp") ? Number($("optLoudnessTp").value) : 0;
    ov.loudness_tp = tp < 0 ? tp : 0;
  }
  if (crfRaw !== "") ov.crf = Number(crfRaw);
  else delete ov.crf;
  // In target mode the solved CRF is the one that gets submitted.
  if (currentQualityMode() === "target" && state.solvedCrf) {
    ov.crf = state.solvedCrf;
  }
  delete ov.loudness_measured;
  return ov;
}

const ADVANCED_PROFILES = new Set(["P1", "P2", "P3", "P4", "P5", "P7", "A0", "A1", "A2"]);
const EXTREME_PROFILES = new Set(["P6", "A3", "A4", "A5", "A6"]);
function profileVisible(p) {
  return (state.settings.show_advanced === true && ADVANCED_PROFILES.has(p.id))
    || (state.settings.show_extreme === true && EXTREME_PROFILES.has(p.id));
}
function profileLabel(p) {
  return p.id === "P3" ? "硬件 HEVC · 日常" : p.id === "A2" ? "软件 AV1 · 高压缩归档" : p.name;
}
function profileOptions(current) {
  return state.profiles.filter((p) => profileVisible(p) || p.id === current);
}
function profileOptionHtml(current) {
  return profileOptions(current).map((p) => `<option value="${p.id}"${p.id === current ? " selected" : ""}${!profileVisible(p) ? " disabled" : ""}>${p.id} ${escapeHtml(profileLabel(p))}${!profileVisible(p) ? "（当前项，已隐藏）" : ""}</option>`).join("");
}
function fileSelection(f) {
  const own = f?.override || {};
  const main = overrides();
  const base = state.selectionMode === "manual" && f?.seed ? { ...f.seed } : { ...main };
  for (const key of ["location", "out_dir", "default_out_dir", "name_template", "keep_timestamps"])
    if (key in main) base[key] = main[key];
  const profile = own.profile || (state.selectionMode === "manual" && f?.seedProfile) || state.profileId;
  delete base.profile;
  delete base.loudness_measured;
  for (const [key, value] of Object.entries(own)) if (key !== "profile") base[key] = value;
  if (own.profile || Object.keys(own).length) base.intent = {};
  return { profile, overrides: base };
}
function syncManualSeeds(keys) {
  if (state.selectionMode !== "manual") return;
  const current = overrides();
  for (const f of state.files) if (f.seed) {
    for (const key of keys) {
      if (key in current) f.seed[key] = current[key];
      else delete f.seed[key];
    }
  }
}
function sizeBenefit(src, low, high = low) {
  if (![src, low, high].every((n) => Number.isFinite(n) && n > 0) || low > high) return null;
  const rLow = src / high, rHigh = src / low;
  const percent = (a, b) => Math.round(a) === Math.round(b) ? `${Math.round(a)}%` : `${Math.round(a)}–${Math.round(b)}%`;
  const delta = (a, b) => {
    const fmt = (n) => n === 0 ? "0 B" : fmtBytes(n);
    return a === b ? fmt(a) : `${fmt(a)}–${fmt(b)}`;
  };
  return {
    multiple: Math.abs(rHigh - rLow) < 0.005 ? `${rLow.toFixed(2)}×` : `${rLow.toFixed(2)}–${rHigh.toFixed(2)}×`,
    change: high <= src ? `节省 ${percent((1 - high / src) * 100, (1 - low / src) * 100)} · ${delta(src - high, src - low)}`
      : low >= src ? `体积增加 ${percent((low / src - 1) * 100, (high / src - 1) * 100)}，建议跳过`
      : `空间变化：可能节省 ${((1 - low / src) * 100).toFixed(0)}%，也可能增加 ${((high / src - 1) * 100).toFixed(0)}%`,
    grows: high > src,
  };
}
function bitrateText(media) {
  const video = media?.video?.bit_rate;
  if (Number.isFinite(video) && video > 0) return `视频码率 ${(video / 1e6).toFixed(2)} Mbps`;
  if (Number.isFinite(media?.bit_rate) && media.bit_rate > 0) return `总码率 ${(media.bit_rate / 1e6).toFixed(2)} Mbps`;
  if (Number.isFinite(media?.duration) && media.duration > 0 && Number.isFinite(media?.size_bytes) && media.size_bytes > 0)
    return `总码率估算 ${(media.size_bytes * 8 / media.duration / 1e6).toFixed(2)} Mbps`;
  return "码率未知";
}
function encoderLabel(name) {
  return ({ h264_nvenc: "H.264 · 硬件加速", hevc_nvenc: "HEVC · 硬件加速", av1_nvenc: "AV1 · 硬件加速",
    libx264: "H.264 · 软件编码", libx265: "HEVC · 软件编码", libsvtav1: "AV1 · 软件编码", copy: "仅换封装" })[name] || name || "算法未知";
}
function batchDurationText() {
  const known = state.files.filter((f) => Number.isFinite(f.media?.duration) && f.media.duration > 0);
  const total = known.reduce((sum, f) => sum + f.media.duration, 0);
  const unknown = state.files.filter((f) => f.media).length - known.length;
  return unknown ? `${total ? fmtDuration(total) + " + " : ""}${unknown} 项时长未知` : fmtDuration(total);
}
function applyInterface() {
  const manual = state.selectionMode === "manual";
  $("simpleGoals").classList.toggle("hidden", manual || state.mode === "repair");
  $("profileList").classList.toggle("hidden", !manual || state.mode === "repair");
  $("advanced").classList.toggle("hidden", !manual || !state.settings.show_advanced);
  $("selectionModeBtn").classList.toggle("hidden", state.mode === "repair" || (!manual && !state.settings.show_advanced && !state.settings.show_extreme));
  $("selectionModeBtn").textContent = manual ? "切回简单模式" : "切换手动模式";
  $("simpleHint").textContent = manual ? "手动模式：沿用已解析参数；已隐藏的当前档位继续保留。" : "自动选择算法，保持尺寸、帧率与原有内容；不保证固定倍率。";
  $("compatibilityChoice").value = settingValue('compatibility', state.settings.compatibility || 'modern');
  $("slowAv1Choice").checked = settingValue('slow_av1', state.settings.slow_av1 === true);
  $("slowAv1Row").classList.toggle('hidden', manual || state.mode === 'repair' || state.goal !== 'strong' || state.settings.compatibility !== 'av1');
  $("groupControls").classList.toggle("hidden", !state.settings.show_advanced && !state.settings.show_extreme);
  $("budgetControls").classList.toggle("hidden", !manual || !state.settings.show_advanced);
  for (const btn of $("simpleGoals").querySelectorAll("[data-goal]")) {
    btn.classList.toggle("active", btn.dataset.goal === state.goal);
    btn.setAttribute("aria-pressed", String(btn.dataset.goal === state.goal));
  }
  renderProfiles(); renderFiles(); syncGroupRows(); renderJobs(state.jobs);
}

function switchSelectionMode() {
  if (state.selectionMode === "simple") {
    if (!state.plan?.profile_id || state.plan.intent?.goal !== state.goal || !state.batchEstimate?.complete) {
      toast("请先加入可用文件并等待本次计划解析完成", 5000); return;
    }
    state.profileId = state.plan.profile_id;
    state.manualSeed = { ...state.plan.resolved_overrides, intent: {} };
    for (const row of state.batchEstimate.rows || []) {
      const f = state.files.find((x) => x.path === row.path);
      if (f && !f.override) { f.seed = row.resolved_overrides; f.seedProfile = row.profile; }
    }
    const ov = state.manualSeed;
    $("optCrf").value = ov.crf ?? "";
    $("optContainer").value = ov.container || "";
    $("optAudio").value = ov.audio_mode || "copy";
    $("optSubs").value = ov.subtitle_mode || "keep";
    $("optMaxEdge").value = ov.max_long_edge || 0;
    $("optScalePct").value = ov.scale_percent || 0;
    $("optGop").value = ov.gop_seconds || 1;
    $("optQualityMode").value = "crf";
    for (const id of ["optDeint", "optDenoise", "optAutocrop", "optHdr", "optStrip", "expertMode"]) $(id).checked = false;
    $("optLoudness").value = "0";
    $("optKeepTime").checked = true;
    state.selectionMode = "manual";
  } else {
    state.selectionMode = "simple";
    state.manualSeed = null;
    $("optQualityMode").value = "crf"; state.solvedCrf = null; state.targetResult = null;
    for (const f of state.files) { f.seed = null; f.seedProfile = null; }
  }
  applyQualityModeUI(); applyInterface(); refreshPlan();
}

/** The LUFS target from the UI: 0 = off, negative = normalise. */
function loudnessTarget() {
  const sel = $("optLoudness");
  if (!sel) return 0;
  if (sel.value === "custom") {
    const value = Number(($("optLoudnessCustom") || {}).value || 0);
    // An empty custom box means "off", not "normalise to 0 LUFS" (which is not a
    // thing). Clamped to the range the backend accepts so the number shown is the
    // number used.
    if (!isFinite(value) || value >= 0) return 0;
    return Math.max(-40, Math.min(-5, value));
  }
  return Number(sel.value) || 0;
}

/** Show the custom-target and true-peak rows only when normalisation is on. */
function syncLoudnessRows() {
  const target = loudnessTarget();
  const on = target < 0;
  const custom = $("optLoudness") && $("optLoudness").value === "custom";
  if ($("loudnessCustomRow")) $("loudnessCustomRow").classList.toggle("hidden", !custom);
  if ($("loudnessTpRow")) $("loudnessTpRow").classList.toggle("hidden", !on);
}

/* ------------------------------------------------------------ 目标体积 --- */

async function solveTarget() {
  const entry = state.files.find((f) => f.path === state.activeFile);
  if (!entry) { toast("先选中一个文件"); return; }
  const target = $("optTarget").value.trim();
  if (!target) { toast("请先填写目标体积，例如 200MB"); return; }

  const btn = $("targetSolveBtn");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "求解中…";
  toast("正在用 3 段真实样片反推参数（约 10-60 秒）", 8000);
  try {
    const data = await api("/api/target", {
      method: "POST",
      body: JSON.stringify({
        path: state.activeFile,
        profile: state.profileId,
        target,
        seconds: 6,
        strict: !!($("optTargetStrict") && $("optTargetStrict").checked),
        predict_quality: true,
        overrides: overrides(),
      }),
    });
    const t = data.target;
    state.solvedCrf = t.ok ? t.crf : null;
    state.targetResult = t.ok ? t : null;
    const reach = t.reachability || {};
    state.estimate = t.ok ? {
      low: t.low, high: t.high, mid: t.predicted_bytes,
      ratio: entry.media && entry.media.size_bytes
        ? t.predicted_bytes / entry.media.size_bytes : 0,
      source_bytes: entry.media ? entry.media.size_bytes : 0,
      source_kbps: 0, avg_kbps: 0, est_seconds: 0,
      samples: (t.samples || []).map((s) => `CRF${s.crf}→${fmtBytes(s.bytes)}`),
      basis: `按目标体积反推：拟合 ${t.fit.points} 个 CRF 采样点（斜率 ${t.fit.slope}）`,
      note: t.note,
      reachability: reach.message || "",
      reach_blocking: reach.tier === "impossible",
      secondary_ratio: dualBasis(entry.media, entry.media && entry.media.duration,
                                 t.predicted_bytes).secondary,
      target_explained: t.target_explained || "",
      quality: t.quality || null,
      strict: !!t.strict,
    } : null;
    renderEstimate();
    // One toast, not two: the second used to overwrite the first, so a solve with
    // warnings showed the warnings and hid the answer.
    if (!t.ok) {
      toast("求解失败：" + (t.warnings || []).join("；"), 9000);
    } else {
      const bits = [`已求解：CRF ${t.crf} → 预计 ${fmtBytes(t.predicted_bytes)}`];
      if (t.strict) bits.push("按「严格不超过目标」求解");
      if (t.quality && t.quality.ok) {
        bits.push(`预计画质 VMAF ${t.quality.score}（${t.quality.plain}）`);
      }
      if (reach.message) bits.push(reach.message);
      if (t.warnings && t.warnings.length) bits.push(t.warnings.join("；"));
      toast(bits.join("　|　"), reach.tier === "impossible" ? 14000 : 9000);
    }
    refreshPlan();
  } catch (err) {
    toast("求解失败: " + err.message, 8000);
  } finally {
    btn.disabled = false;
    btn.textContent = old || "求解目标体积";
  }
}


/* --------------------------------------------------------- output location */

function applyLocationUI() {
  const location = currentLocation();
  $("defaultDirRow").classList.toggle("hidden", location !== "default");
  $("customDirRow").classList.toggle("hidden", location !== "custom");
  localStorage.setItem("vcs-location", location);
  // The free-space line is about the CURRENT choice, so it follows the choice.
  refreshSpaceNote();
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    state.settings = data.settings || {};
    $("optDefaultDir").value = state.settings.default_out_dir
      || data.suggested_default_out_dir || "";
    const location = state.settings.location || localStorage.getItem("vcs-location") || "beside";
    const radio = document.querySelector(`input[name="location"][value="${location}"]`);
    if (radio) radio.checked = true;
    applyLocationUI();
    renderVerifyPanel();
  } catch (err) {
    applyLocationUI();
  }
}

function renderVerifyPanel() {
  if ($("verifySummary")) {
    const deep = state.settings.deep_verify === true;
    const vmaf = state.settings.vmaf_check === true;
    const parts = [deep ? "深度校验（逐帧解码）" : "快速校验（包级扫描）"];
    if (vmaf) parts.push("+VMAF 评分");
    $("verifySummary").textContent = parts.join(" ");
  }
}

/* ----------------------------------------------------------- history (📚) --- */

const HISTORY_PAGE = 50;

function historyWhen(seconds) {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const clock = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (sameDay) return `今天 ${clock}`;
  const yesterday = new Date(now.getTime() - 86400000);
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${clock}`;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${clock}`;
}

function historyDuration(seconds) {
  if (!seconds || !isFinite(seconds)) return "";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${sec} 秒`;
  return `${sec} 秒`;
}

function historyVerdict(entry) {
  if (entry.verdict === "passed") {
    const gap = entry.max_keyframe_gap;
    return `<span class="hv ok" title="${entry.keyframe_count} 个关键帧">校验通过${gap ? `（间隔 ${gap}s）` : ""}</span>`;
  }
  if (entry.verdict === "risky") return `<span class="hv warn">校验注意</span>`;
  if (entry.verdict === "failed") return `<span class="hv err">校验未通过</span>`;
  if (entry.status === "failed") return `<span class="hv err">编码失败</span>`;
  return `<span class="hv">未校验</span>`;
}

async function loadHistory({ append = false } = {}) {
  const h = state.history;
  if (!append) h.limit = HISTORY_PAGE;
  const params = new URLSearchParams({
    limit: String(h.limit),
    query: h.query,
    only_existing: h.onlyExisting ? "1" : "0",
  });
  try {
    const data = await api("/api/history?" + params.toString());
    h.entries = data.entries || [];
    h.stats = data.stats || null;
    renderHistory();
  } catch (err) {
    if ($("historyList")) {
      $("historyList").innerHTML =
        `<p class="empty">历史读取失败：${escapeHtml(err.message)}</p>`;
    }
  }
}

function renderHistory() {
  const h = state.history;
  const box = $("historyList");
  if (!box) return;
  const st = h.stats || {};

  if ($("historySummary")) {
    const parts = [];
    if (st.total) parts.push(`${st.total} 条`);
    if (st.bytes_saved) parts.push(`累计省下 ${fmtBytes(st.bytes_saved)}`);
    if (st.missing) parts.push(`${st.missing} 条成品已不在`);
    $("historySummary").textContent = parts.join(" · ") || "还没有记录";
  }

  // Prune selections whose rows vanished (deleted, filtered out, or off-page).
  const visible = new Set(h.entries.map((e) => e.id));
  for (const id of [...h.selected]) if (!visible.has(id)) h.selected.delete(id);

  if (!h.entries.length) {
    box.innerHTML = `<p class="empty">${h.query || h.onlyExisting
      ? "没有匹配的记录" : "还没有压缩记录 —— 压完一个就会出现在这里"}</p>`;
    if ($("historyMoreBtn")) $("historyMoreBtn").classList.add("hidden");
    renderHistoryBulk();
    return;
  }

  box.innerHTML = h.entries.map((e) => {
    const ratio = (e.ratio !== null && e.ratio !== undefined)
      ? `${(e.ratio * 100).toFixed(0)}%` : "";
    const meta = [
      `${fmtBytes(e.src_size)} → ${fmtBytes(e.total_size)}${ratio ? `（${ratio}）` : ""}`,
      e.elapsed ? `用时 ${historyDuration(e.elapsed)}` : "",
      e.encoder ? escapeHtml(e.encoder) : "",
      e.profile_id ? escapeHtml(e.profile_id) : "",
      e.mode === "repair" ? "修复" : "",
    ].filter(Boolean).join(" · ");

    const gone = !e.out_exists;
    const disabled = gone ? " disabled" : "";
    const warn = gone
      ? `<span class="hv err">成品已不在原位置（可能被移动或删除）</span>`
      : (e.src_exists ? "" : `<span class="hv warn">源文件已不在</span>`);
    const checked = h.selected.has(e.id) ? " checked" : "";

    return `
      <div class="history-item${gone ? " gone" : ""}${checked ? " picked" : ""}" data-id="${escapeHtml(e.id)}">
        <label class="history-pick" title="选中以批量删除">
          <input type="checkbox" data-act="pick" data-id="${escapeHtml(e.id)}"${checked}>
        </label>
        <div class="history-main">
          <div class="history-names" title="${escapeHtml(e.src)} → ${escapeHtml(e.out_path)}">
            <span class="hn-src">${escapeHtml(e.src_name || "(未知源)")}</span>
            <span class="hn-arrow">→</span>
            <span class="hn-out">${escapeHtml(e.out_name || "(无输出)")}</span>
          </div>
          <div class="history-meta">${meta} · ${historyVerdict(e)}</div>
          <div class="history-when">${historyWhen(e.finished)} ${warn}</div>
        </div>
        <div class="history-actions">
          <button class="ghost" data-act="compare"${disabled} title="源与成品并排对比">对比</button>
          <button class="ghost" data-act="preview"${disabled}>预览</button>
          <button class="ghost" data-act="reveal"${disabled}>打开成品</button>
          <button class="ghost" data-act="requeue">重新压缩</button>
          <button class="ghost" data-act="remove">删除记录</button>
        </div>
      </div>`;
  }).join("");

  if ($("historyMoreBtn")) {
    const hidden = h.entries.length < h.limit;
    $("historyMoreBtn").classList.toggle("hidden", hidden);
  }
  renderHistoryBulk();
}

/** Selection bar: counts, select-all state, and what a batch delete would affect. */
function renderHistoryBulk() {
  const h = state.history;
  const bar = $("historyBulkBar");
  if (!bar) return;
  const n = h.selected.size;
  const picked = h.entries.filter((e) => h.selected.has(e.id));
  const reclaim = picked.reduce((sum, e) => sum + (e.out_exists ? (e.total_size || 0) : 0), 0);
  const alsoDelete = !!($("historyAlsoDeleteFiles") && $("historyAlsoDeleteFiles").checked);

  bar.classList.toggle("hidden", !h.entries.length);
  if ($("historySelCount")) {
    $("historySelCount").textContent = n
      ? `已选 ${n} 条${reclaim ? `，成品合计 ${fmtBytes(reclaim)}` : ""}`
      : "未选择";
  }
  if ($("historyDeleteSelBtn")) {
    $("historyDeleteSelBtn").disabled = n === 0;
    $("historyDeleteSelBtn").textContent = alsoDelete ? "删除选中及成品" : "删除选中";
  }
  // Deleting the compressed files is irreversible, so it is an explicit opt-in that
  // has to be ticked BEFORE the click -- never the reflex-OK answer to a second
  // "are you sure?" prompt. The label always carries the amount at stake.
  if ($("historyAlsoWrap")) {
    const canDelete = reclaim > 0;
    $("historyAlsoWrap").classList.toggle("disabled", !canDelete);
    if ($("historyAlsoDeleteFiles")) {
      $("historyAlsoDeleteFiles").disabled = !canDelete;
      if (!canDelete) $("historyAlsoDeleteFiles").checked = false;
    }
    if ($("historyAlsoLabel")) {
      $("historyAlsoLabel").textContent = canDelete
        ? `同时删除成品文件（释放 ${fmtBytes(reclaim)}）`
        : "同时删除成品文件（无可删除的成品）";
    }
  }
  if ($("historySelectAll")) {
    $("historySelectAll").checked = n > 0 && n === h.entries.length;
    $("historySelectAll").indeterminate = n > 0 && n < h.entries.length;
  }
  // "Delete everything matching the filter" only makes sense with a filter on --
  // otherwise it is just 清空历史 under a different name.
  const filtered = !!h.query || h.onlyExisting;
  if ($("historyDeleteFilteredBtn")) {
    $("historyDeleteFilteredBtn").classList.toggle("hidden", !filtered);
    if (filtered) {
      $("historyDeleteFilteredBtn").textContent =
        `删除全部筛选结果（${h.entries.length >= h.limit ? "≥" : ""}${h.entries.length} 条）`;
      $("historyDeleteFilteredBtn").disabled = h.entries.length === 0;
    }
  }
}

/**
 * Batch delete.
 *
 * Whether the compressed files go too is decided by the explicit checkbox in the
 * bulk bar, before the click -- not by a second "are you sure?" whose OK button
 * would be the destructive answer. Then exactly one confirmation states the
 * consequence in full.
 */
async function removeHistoryBatch({ allMatching = false } = {}) {
  const h = state.history;
  let picked = allMatching ? h.entries : h.entries.filter((e) => h.selected.has(e.id));
  if (allMatching) {
    // The list is paged, so `h.entries` can be a truncated view while the server
    // resolves `all_matching` over EVERY record. Ask for the exact set first: the
    // confirmation then states a real number, and that number is sent back as
    // `expected_count` so the server can refuse if the set changed underneath us.
    try {
      const params = new URLSearchParams({
        limit: "0",
        query: h.query || "",
        only_existing: h.onlyExisting ? "1" : "0",
      });
      const full = await api("/api/history?" + params.toString());
      if (Array.isArray(full.entries)) picked = full.entries;
    } catch (err) {
      toast("无法获取完整筛选结果: " + err.message, 6000);
      return;
    }
  }
  if (!picked.length) { toast("没有选中任何记录"); return; }

  const deletable = picked.filter((e) => e.out_exists);
  const reclaim = deletable.reduce((sum, e) => sum + (e.total_size || 0), 0);
  const deleteOutput = !!($("historyAlsoDeleteFiles") && $("historyAlsoDeleteFiles").checked)
    && deletable.length > 0;

  const scope = allMatching
    ? `删除当前筛选出的全部 ${picked.length} 条历史记录？`
    : `删除选中的 ${picked.length} 条历史记录？`;
  const cond = allMatching
    ? [h.query ? `筛选条件：文件名含「${h.query}」` : "",
       h.onlyExisting ? "只看能用的" : ""].filter(Boolean).join(" + ")
    : "";
  const consequence = deleteOutput
    ? `⚠ 连同 ${deletable.length} 个成品文件一起删除，释放约 ${fmtBytes(reclaim)}。\n`
      + "文件不会进回收站，删除后无法恢复。"
    : `成品文件全部保留（${deletable.length} 个，约 ${fmtBytes(reclaim)}）。\n`
      + "如需连成品一起删除，请先勾选「同时删除成品文件」。";

  const text = [scope, cond ? `（${cond}）` : "", "", consequence].filter((x) => x !== "").join("\n");
  if (!window.confirm(text)) return;

  const body = allMatching
    ? { all_matching: true, query: h.query, only_existing: h.onlyExisting,
        delete_output: deleteOutput, expected_count: picked.length }
    : { ids: [...h.selected], delete_output: deleteOutput };
  if (deleteOutput) body.confirm_delete_output = true;

  try {
    const res = await api("/api/history/remove", { method: "POST", body: JSON.stringify(body) });
    const bits = [`已删除 ${res.removed} 条记录`];
    if (res.outputs_deleted) bits.push(`同时删掉 ${res.outputs_deleted} 个成品（释放 ${fmtBytes(res.freed_bytes || 0)}）`);
    else bits.push("成品文件已保留");
    if (res.skipped_source) bits.push(`${res.skipped_source} 条因输出与源相同被跳过`);
    if (res.missing_output) bits.push(`${res.missing_output} 条成品本就不在原位置`);
    if ((res.failures || []).length) bits.push(`${res.failures.length} 条失败`);
    toast(bits.join("；"), 6000);
    h.selected.clear();
    h.stats = res.stats || h.stats;
    await loadHistory();
  } catch (err) {
    toast("批量删除失败: " + err.message, 6000);
  } finally {
    // Disarm the destructive checkbox after EVERY attempt, success or failure.
    // Leaving it ticked after a failure means the next click -- possibly much later,
    // on a different selection -- would silently delete video files the user never
    // meant to touch. Opting in must be a fresh, deliberate act every time.
    if ($("historyAlsoDeleteFiles")) $("historyAlsoDeleteFiles").checked = false;
    renderHistoryBulk();
  }
}

/** A history row has no live `job`, so build the minimum openCompare needs. */
function historyAsJob(entry) {
  return {
    src_size: entry.src_size,
    total_size: entry.total_size,
    total: entry.total_size,
    verify: {
      max_keyframe_gap: entry.max_keyframe_gap,
      keyframe_count: entry.keyframe_count,
      verdict: entry.verdict,
      checks: entry.checks || [],
    },
  };
}

/**
 * Single-row delete: records only, always.
 *
 * Deleting the compressed file is irreversible, so it is reachable only through the
 * bulk bar's explicit "同时删除成品文件" checkbox. A per-row two-prompt flow where the
 * second OK destroys a file is exactly the kind of reflex-OK trap to avoid -- and
 * being consistent means the user never has to remember which route does what.
 */
async function removeHistoryEntry(entry) {
  const size = entry.out_exists ? `（${fmtBytes(entry.total_size || 0)}）` : "";
  const text = [
    `删除这条历史记录？`,
    "",
    `${entry.out_name || entry.src_name}${size}`,
    "",
    entry.out_exists
      ? "成品文件会保留在原位置。\n如需连成品一起删除，请勾选列表上方的「同时删除成品文件」后批量删除。"
      : "成品文件已不在原位置，仅删除记录。",
  ].join("\n");
  if (!window.confirm(text)) return;

  try {
    const res = await api("/api/history/remove", {
      method: "POST", body: JSON.stringify({ id: entry.id, delete_output: false }),
    });
    state.history.selected.delete(entry.id);
    state.history.stats = res.stats || state.history.stats;
    toast(entry.out_exists ? "记录已删除（成品文件保留）" : "记录已删除");
    await loadHistory();
  } catch (err) {
    toast("删除失败: " + err.message, 6000);
  }
}

function bindHistory() {
  const list = $("historyList");
  if (!list) return;

  // The pick checkbox uses a click handler (not "change") so the row's own click
  // handling and re-render stay in one place.
  list.addEventListener("click", async (ev) => {
    const box = ev.target.closest('input[data-act="pick"]');
    if (box) {
      const id = box.dataset.id;
      if (box.checked) state.history.selected.add(id);
      else state.history.selected.delete(id);
      const row = box.closest(".history-item");
      if (row) row.classList.toggle("picked", box.checked);
      renderHistoryBulk();
      return;
    }

    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const row = ev.target.closest(".history-item");
    if (!row) return;
    const entry = state.history.entries.find((e) => e.id === row.dataset.id);
    if (!entry) return;
    const act = btn.dataset.act;

    if (act === "compare") {
      if (!entry.out_exists) { toast("成品文件已不在原位置"); return; }
      openCompare(entry.src, entry.out_path, historyAsJob(entry));
    } else if (act === "preview") {
      if (!entry.out_exists) { toast("成品文件已不在原位置"); return; }
      openPreview(entry.out_path, { checks: entry.checks || [] }, null);
    } else if (act === "reveal") {
      try {
        await api("/api/reveal", { method: "POST", body: JSON.stringify({ path: entry.out_path }) });
      } catch (err) { toast("无法打开所在文件夹: " + err.message, 5000); }
    } else if (act === "requeue") {
      const added = addPaths([entry.src], "历史记录");
      if (added) toast(added === 1 ? "已加入队列" : `已加入 ${added} 个文件`);
    } else if (act === "remove") {
      await removeHistoryEntry(entry);
    }
  });

  if ($("historyQuery")) {
    let timer = null;
    $("historyQuery").addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.history.query = $("historyQuery").value.trim();
        loadHistory();
      }, 250);
    });
  }
  if ($("historyOnlyExisting")) {
    $("historyOnlyExisting").addEventListener("change", () => {
      state.history.onlyExisting = $("historyOnlyExisting").checked;
      loadHistory();
    });
  }
  if ($("historyRefreshBtn")) {
    $("historyRefreshBtn").addEventListener("click", () => {
      loadHistory().then(() => toast("历史已刷新"));
    });
  }
  if ($("historyMoreBtn")) {
    $("historyMoreBtn").addEventListener("click", () => {
      state.history.limit += HISTORY_PAGE;
      loadHistory({ append: true });
    });
  }
  if ($("historyClearMissingBtn")) {
    $("historyClearMissingBtn").addEventListener("click", async () => {
      const missing = (state.history.stats || {}).missing || 0;
      if (!missing) { toast("没有失效记录"); return; }
      if (!window.confirm(
        `清理 ${missing} 条失效记录？\n\n这些记录的成品文件已经不在原位置。\n` +
        "只删记录，不会删除任何视频文件。")) return;
      try {
        const res = await api("/api/history/clear", {
          method: "POST", body: JSON.stringify({ only_missing: true }),
        });
        toast(`已清理 ${res.removed} 条失效记录`);
        await loadHistory();
      } catch (err) { toast("清理失败: " + err.message, 6000); }
    });
  }
  if ($("historyClearAllBtn")) {
    $("historyClearAllBtn").addEventListener("click", async () => {
      const total = (state.history.stats || {}).total || 0;
      if (!total) { toast("历史已经是空的"); return; }
      if (!window.confirm(
        `清空全部 ${total} 条历史记录？\n\n` +
        "只删记录，压缩好的视频文件都会保留在原来的位置。")) return;
      try {
        const res = await api("/api/history/clear", {
          method: "POST", body: JSON.stringify({ only_missing: false, confirm: true }),
        });
        toast(`已清空 ${res.removed} 条记录（视频文件都还在）`);
        await loadHistory();
      } catch (err) { toast("清空失败: " + err.message, 6000); }
    });
  }

  // -- batch selection / deletion --
  if ($("historySelectAll")) {
    $("historySelectAll").addEventListener("change", () => {
      const h = state.history;
      if ($("historySelectAll").checked) h.entries.forEach((e) => h.selected.add(e.id));
      else h.selected.clear();
      renderHistory();
    });
  }
  if ($("historyDeleteSelBtn")) {
    $("historyDeleteSelBtn").addEventListener("click", () => removeHistoryBatch());
  }
  // Ticking this must immediately relabel the button: the label is the last warning
  // before an irreversible delete.
  if ($("historyAlsoDeleteFiles")) {
    $("historyAlsoDeleteFiles").addEventListener("change", renderHistoryBulk);
  }
  if ($("historyDeleteFilteredBtn")) {
    $("historyDeleteFilteredBtn").addEventListener("click", () => removeHistoryBatch({ allMatching: true }));
  }
}

async function saveDefaultDir(silent) {
  const value = $("optDefaultDir").value.trim();
  const saved = await savePreference({ default_out_dir: value });
  if (saved) {
    if (!silent) toast(value ? "默认目录已保存" : "已清除默认目录（将按源文件旁边存放）");
    return true;
  }
  return false;
}

/* -------------------------------------------------- destructive profiles (A3) --- */

/**
 * Ask before running a ladder profile that permanently discards resolution or
 * frame rate. Returns false when the user backs out.
 *
 * Only the ladder's own `needs_confirmation` flags trigger this (A5/A6 today). It is
 * deliberately a single, specific prompt rather than a blanket "are you sure" -- a
 * confirmation that fires for everything gets clicked through without being read.
 */
async function confirmDestructiveProfile(fileCount) {
  const selected = new Set(state.files.filter((f) => f.media).map((f) => {
    const selection = fileSelection(f);
    return selection.overrides.intent?.goal ? null : selection.profile;
  }));
  const profiles = state.profiles.filter((p) => selected.has(p.id) && p.ladder?.needs_confirmation);
  if (!profiles.length) return true;
  const profile = profiles[0];
  const lad = profile && profile.ladder;
  if (!lad || !lad.needs_confirmation) return true;

  const lines = [
    `档位 ${profile.id} · ${profile.name}`,
    "",
    "这个档位会明显牺牲画质：",
    ...profiles.map((p) => `${p.id}：${p.ladder.costs || p.summary}`),
    "",
    `预期倍率 ${lad.expected_ratio[0]}–${lad.expected_ratio[1]}×，`
      + `预期画质 VMAF ${lad.expected_vmaf[0] > 0
          ? lad.expected_vmaf[0] + "–" + lad.expected_vmaf[1]
          : "低于 " + lad.expected_vmaf[1]}`,
    "",
    `要压 ${fileCount} 个文件。确定继续吗？`,
  ];
  if (!window.confirm(lines.join("\n"))) {
    toast("已取消：该档位会明显降质", 5000);
    return false;
  }
  return true;
}

/* ------------------------------------------------- per-job editor (F12a) --- */

/**
 * Edit one QUEUED job's settings without touching the rest of the batch.
 *
 * The backend refuses this for anything that has already started, because a report
 * that describes settings other than the ones used would be worse than no report.
 * The dialog says so up front rather than letting the user find out on save.
 */
function openJobEditor(job) {
  if (!state.settings.show_advanced && !state.settings.show_extreme) { openSettings(); return; }
  const overlay = document.createElement("div");
  overlay.className = "preview-overlay";
  const ov = job.overrides || {};
  overlay.innerHTML = `
    <div class="preview-box" style="width:min(560px,100%)">
      <div class="panel-head">
        <h2>调整这一个任务的参数</h2>
        <button class="ghost" id="jeClose">关闭</button>
      </div>
      <p class="hint">只影响 <b>${escapeHtml(job.src_name || job.src)}</b>，不影响队列里其它任务。</p>
      <div class="adv-grid">
        <label>档位
          <select id="jeProfile">
            ${profileOptionHtml(job.profile_id)}
          </select>
        </label>
        <label class="${!state.settings.show_advanced ? "hidden" : ""}">画质 CRF（NVENC 档位映射为 -cq）
          <input type="number" id="jeCrf" min="0" max="63" value="${ov.crf !== undefined && ov.crf !== null ? ov.crf : ""}" placeholder="跟随档位">
        </label>
        <label class="${!state.settings.show_advanced ? "hidden" : ""}">缩放到（长边像素，0=不缩放）
          <input type="number" id="jeEdge" min="0" step="16" value="${ov.max_long_edge || 0}">
        </label>
        <label class="${!state.settings.show_advanced ? "hidden" : ""}">关键帧间隔（秒，≤2）
          <input type="number" id="jeGop" min="0.5" max="2" step="0.5" value="${ov.gop_seconds || 1}">
        </label>
      </div>
      <p class="hint" id="jeError"></p>
      <div class="loc-detail">
        <button id="jeSave" class="primary">保存并重算命令</button>
        <button id="jeCancel" class="ghost">取消</button>
      </div>
    </div>`;
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay || ev.target.id === "jeClose" || ev.target.id === "jeCancel") {
      overlay.remove();
    }
  });
  document.body.appendChild(overlay);

  overlay.querySelector("#jeProfile").addEventListener("change", () => {
    overlay.querySelector("#jeCrf").value = "";
    overlay.querySelector("#jeEdge").value = "0";
  });
  overlay.querySelector("#jeSave").addEventListener("click", async () => {
    const errBox = overlay.querySelector("#jeError");
    errBox.textContent = "";
    const crfRaw = overlay.querySelector("#jeCrf").value.trim();
    const chosen = state.profiles.find((p) => p.id === overlay.querySelector("#jeProfile").value);
    if (chosen?.id !== job.profile_id && chosen?.ladder?.needs_confirmation &&
        !window.confirm(`该任务将改用 ${chosen.id}，${chosen.ladder.costs || chosen.summary}。\n\n确认修改这一个排队任务吗？`)) return;
    const patch = {
      max_long_edge: Number(overlay.querySelector("#jeEdge").value) || 0,
      gop_seconds: Number(overlay.querySelector("#jeGop").value) || 1,
    };
    patch.crf = crfRaw !== "" ? Number(crfRaw) : chosen?.crf ?? null;
    try {
      const data = await api(`/api/jobs/${job.id}`, {
        method: "PATCH",
        body: JSON.stringify({ overrides: patch,
                               profile: overlay.querySelector("#jeProfile").value }),
      });
      toast("已更新：" + (data.job.out_path || "").split(/[\\/]/).pop(), 6000);
      overlay.remove();
      refreshJobs();
    } catch (err) {
      // Shown next to the fields, not as a transient toast: the user is mid-edit.
      errBox.textContent = err.message;
      errBox.className = "hint space-error";
    }
  });
}

/* ------------------------------------------------ 任务日志查看器（F8）--- */

/**
 * Show everything known about one job, in one place.
 *
 * The design goal is "自证清白": a user who hits a problem should be able to read the
 * timeline, the checks, the exact command and the raw output, and either solve it or
 * hand the whole thing to someone else with one button. So the diagnostic blob is built
 * server-side and copied verbatim -- no client-side reassembly that could disagree with
 * the server about what happened.
 */
async function openJobLog(job) {
  const overlay = document.createElement("div");
  overlay.className = "preview-overlay";
  overlay.innerHTML = `
    <div class="preview-box" style="width:min(920px,100%)">
      <div class="panel-head">
        <h2>任务日志 <span class="hint">${escapeHtml(job.src_name || job.src)}</span></h2>
        <button class="ghost" id="logClose">关闭</button>
      </div>
      <div id="logBody" class="hint">读取中…</div>
      <div class="loc-detail">
        <button id="logCopy" class="primary">复制诊断信息</button>
        <a id="logDownload" class="ghost" style="text-decoration:none;padding:6px 10px"
           href="#" download>下载完整日志</a>
      </div>
    </div>`;
  // Esc + backdrop dismissal, and a drag that ends outside the panel no longer closes
  // it (selecting log text is the common way to hit that).
  overlay.addEventListener("click", (ev) => {
    if (ev.target.id === "logClose") overlay.vcsClose();
  });
  wireOverlayDismiss(overlay);
  document.body.appendChild(overlay);

  let data = null;
  try {
    data = await api(`/api/jobs/${job.id}/log`);
  } catch (err) {
    $("logBody").innerHTML = `<span style="color:var(--error)">读取失败：${escapeHtml(err.message)}</span>`;
    return;
  }
  const it = data.job || {};
  const ts = (t) => {
    try { return new Date(t * 1000).toLocaleTimeString(); } catch { return "?"; }
  };
  const timeline = (data.events || []).map((e) => `
    <div class="log-event"><span class="log-time">${ts(e.t)}</span>
      <span class="log-kind">${escapeHtml(e.label || e.kind || "")}</span>
      <span class="log-text">${escapeHtml(e.text || "")}</span></div>`).join("");
  const checks = (((it.verify || {}).checks) || []).map((c) => `
    <div class="log-event"><span class="log-kind">${escapeHtml(c.status)}</span>
      <span class="log-text">${escapeHtml(c.id)} ${escapeHtml(c.label)} — ${escapeHtml(c.detail || "")}</span></div>`).join("");
  const warn = (it.warnings || []).map((w) => `<div class="warnline">⚠ ${escapeHtml(w)}</div>`).join("");

  $("logBody").innerHTML = `
    <div class="log-meta">
      <span class="tag">${escapeHtml(STATUS_TEXT[it.status] || it.status || "")}</span>
      <span>${escapeHtml(it.encoder || "")}</span>
      <span>${fmtBytes(it.src_size)} → ${it.total_size ? fmtBytes(it.total_size) : "—"}</span>
      <span>用时 ${(it.elapsed || 0).toFixed(1)}s${it.paused_total ? `（暂停 ${it.paused_total.toFixed(0)}s 已扣除）` : ""}</span>
      <span class="hint">${escapeHtml(data.environment && data.environment.ffmpeg_version || "")}</span>
    </div>
    ${it.error ? `<div class="job-error">${escapeHtml(it.error)}</div>` : ""}
    ${warn}
    <h3 class="log-head">时间线</h3>
    <div class="log-list">${timeline || '<span class="hint">（无）</span>'}</div>
    <h3 class="log-head">交付校验${(it.verify || {}).verdict ? "：" + escapeHtml(it.verify.verdict) : ""}</h3>
    <div class="log-list">${checks || '<span class="hint">（尚未校验——校验在编码完成后进行）</span>'}</div>
    <h3 class="log-head">命令（可复制重跑）</h3>
    <pre class="log-cmd">${escapeHtml(it.command || "（无）")}</pre>
    <h3 class="log-head">ffmpeg 输出末尾${data.log_truncated ? `（完整日志共 ${fmtBytes(data.log_bytes)}）` : ""}</h3>
    <pre class="log-raw">${escapeHtml(data.log_tail || "（无输出）")}</pre>`;

  $("logDownload").href = data.log_url || "#";
  $("logCopy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(data.diagnostics || "");
      toast("诊断信息已复制（含时间线、校验、命令与日志末尾）", 6000);
    } catch {
      // Clipboard can be denied; fall back to selecting the text so it is still usable.
      const pre = overlay.querySelector(".log-raw");
      if (pre) {
        const range = document.createRange();
        range.selectNodeContents(pre);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
      toast("复制被浏览器拒绝，已为你选中日志文本", 6000);
    }
  });
}

/* -------------------------------------------- submission and source consent */

function sourceKey(path) { return String(path || '').replace(/\\/g, '/').toLowerCase(); }

async function clearSuccessfulRecords() {
  if (!state.backendSupportsHandoff) { toast('请在任务空闲后重启应用启用新版清理规则', 6000); return; }
  try {
    const data = await api('/api/jobs/clear', { method: 'POST', body: '{}' });
    const removed = new Set(data.removed_ids || []);
    applyJobsNow(state.jobs.filter(j => !removed.has(j.id)), null);
    toast(data.removed ? `已清除 ${data.removed} 条成功记录，视频和历史档案保留`
      : '没有可清除的成功记录；失败、有风险和待处理项会保留');
    refreshJobs();
  } catch (err) { toast('清除失败: ' + err.message, 6000); }
}

function handoffSubmittedFiles(submitted, result) {
  const accepted = new Set((result.jobs || []).filter(j => !['failed', 'cancelled', 'interrupted'].includes(j.status)).map(j => sourceKey(j.src)));
  for (const item of result.skipped || []) accepted.add(sourceKey(item.path));
  const moved = new Set(submitted.filter(f => accepted.has(sourceKey(f.path))));
  state.files = state.files.filter(f => !moved.has(f));
  if (!state.files.some(f => f.path === state.activeFile)) state.activeFile = state.files[0]?.path || null;
  state.targetResult = null;
  renderFiles();
  refreshPlan();
  updateStartButton();
  const failed = (result.errors || []).length + (result.jobs || []).filter(j => j.status === 'failed').length;
  const queued = (result.jobs || []).filter(j => !['failed', 'cancelled', 'interrupted'].includes(j.status)).length;
  const message = `已加入队列 ${queued} 个` + (result.skipped?.length ? `，已压过并跳过 ${result.skipped.length} 个` : '')
    + (failed ? `；${failed} 个未成功添加，仍保留在上方` : '；可在下方查看进度与结果');
  $('handoffStatus').textContent = message;
  $('queueJumpBtn').classList.toggle('hidden', !queued);
  toast(message, 7000);
}

function confirmRecycleBatch(files, selections, rows) {
  const box = $('recycleConfirm');
  const bytes = files.reduce((sum, f) => sum + (Number(f.media?.size_bytes) || 0), 0);
  $('recycleConfirmSummary').textContent = `本次 ${files.length} 个源文件，合计 ${fmtBytes(bytes)}。请核对源文件与预计成品位置：`;
  $('recycleConfirmFiles').innerHTML = files.map(f => {
    const ov = selections[f.path].overrides;
    const row = rows.find(r => r.path === f.path);
    const parent = f.path.replace(/\\/g, '/').replace(/[^/]+$/, '');
    const directory = ov.location === 'custom' ? ov.out_dir : ov.location === 'default' ? ov.default_out_dir : parent + 'compressed';
    const output = row?.out_path || directory || '输出目录尚未确认';
    return `<div class="recycle-confirm-item"><b>${escapeHtml(f.name || baseName(f.path))}</b><div>源文件：${escapeHtml(f.path)}</div><div>预计成品：${escapeHtml(output)}</div></div>`;
  }).join('');
  box.classList.remove('hidden');
  const previous = document.activeElement;
  return new Promise(resolve => {
    const buttons = [$('recycleConfirmClose'), $('recycleConfirmCancel'), $('recycleConfirmAccept')];
    const finish = ok => {
      box.classList.add('hidden');
      document.removeEventListener('keydown', keydown);
      buttons.forEach(b => b.onclick = null);
      previous?.focus();
      resolve(ok);
    };
    const keydown = event => {
      if (event.key === 'Escape') { event.preventDefault(); finish(false); }
      if (event.key === 'Tab') {
        const index = buttons.indexOf(document.activeElement);
        const next = (index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length;
        event.preventDefault(); buttons[next].focus();
      }
    };
    buttons[0].onclick = buttons[1].onclick = () => finish(false);
    buttons[2].onclick = () => finish(true);
    document.addEventListener('keydown', keydown);
    buttons[1].focus();
  });
}

async function submitPendingFiles() {
  const files = state.files.filter(f => f.media && !f.probing);
  if (!files.length || state.submitting) return;
  if (!state.backendSupportsSimple || !state.backendSupportsHandoff) {
    toast('请在当前任务空闲后重启应用，启用本次提交和源文件处理功能', 6000); return;
  }
  if (state.mode === 'compress' && (!state.batchEstimate || state.batchEstimate.complete === false)) return;
  const mode = state.mode, seq = state.batchSeq, paths = files.map(f => f.path);
  const selections = Object.fromEntries(files.map(f => [f.path, JSON.parse(JSON.stringify(fileSelection(f)))]));
  const rows = [...(state.batchEstimate?.rows || [])];
  const recycle = mode === 'compress' && $('recycleSources').checked;
  const skip = !!$('skipDone')?.checked;
  const repair = mode === 'repair' ? repairOptions() : null;
  let sent = false;
  state.submitting = true;
  $('startBtn').disabled = true;
  $('recycleSources').disabled = true;
  try {
    if (!await preflightBatch(paths)) return;
    if (!await confirmDestructiveProfile(files.length)) return;
    if (mode === 'compress' && rows.some(r => r.est_high > r.src_bytes) && !window.confirm(
      '部分文件预计可能增大，可先精确测算。仍将本次文件添加到队列吗？')) return;
    if (recycle && !await confirmRecycleBatch(files, selections, rows)) return;
    if (seq !== state.batchSeq || mode !== state.mode) { toast('计划或文件已变化，请核对后重新添加'); return; }
    sent = true;
    const result = await api(mode === 'repair' ? '/api/repair/jobs' : '/api/jobs/batch', {
      method: 'POST', body: JSON.stringify(mode === 'repair' ? { paths, options: repair } : {
        paths, per_file: selections, skip_completed: skip, recycle_sources: recycle,
        confirm_recycle: recycle, expected_count: files.length,
      }),
    });
    handoffSubmittedFiles(files, result);
    refreshJobs();
  } catch (err) {
    toast(`提交结果未确认，文件已保留。请先查看下方队列以免重复添加：${err.message}`, 8000);
    refreshJobs();
  } finally {
    if (sent) $('recycleSources').checked = false;
    $('recycleSources').disabled = false;
    state.submitting = false;
    updateStartButton();
  }
}

/* ------------------------------------------------ 完成动作（G8）--- */

/**
 * Read the completion panel.
 *
 * Source handling belongs to a confirmed submission, never this global panel.
 */
function afterPolicy() {
  const notify = !!($("afterNotify") && $("afterNotify").checked);
  const power = ($("afterPower") && $("afterPower").checked) ? "shutdown" : "none";
  const delay = Number(($("afterDelay") || {}).value || 60);
  const onlyOk = !!($("afterOnlyOk") && $("afterOnlyOk").checked);
  return { notify, power, power_delay: delay, power_only_if_all_ok: onlyOk };
}

function syncAfterRows() {
  const power = !!($("afterPower") && $("afterPower").checked);
  if ($("afterDelayRow")) $("afterDelayRow").classList.toggle("hidden", !power);
  if ($("afterSummary")) {
    const bits = [];
    if (afterPolicy().notify) bits.push("提示音");
    if (power) bits.push("关机 " + afterPolicy().power_delay + "s");
    $("afterSummary").textContent = bits.length ? "已开启：" + bits.join(" · ") : "全部关闭";
  }
}

async function saveAfterPolicy() {
  try {
    const data = await api("/api/on-success", {
      method: "POST", body: JSON.stringify({ on_success: afterPolicy() }),
    });
    return data.on_success;
  } catch (err) {
    toast("完成动作设置失败: " + err.message, 6000);
    return null;
  }
}

function renderPowerBanner(power) {
  const box = $("powerBanner");
  if (!box) return;
  const armed = power && power.armed;
  box.classList.toggle("hidden", !armed);
  if (!armed) { box.innerHTML = ""; return; }
  const s = power.summary || {};
  box.innerHTML = `⚠ 已排定自动关机（${power.delay || 0} 秒后）——`
    + `本轮 ${s.done || 0} 个完成、${s.failed || 0} 个失败。`
    + `<button id="cancelPowerBtn" class="ghost" style="margin-left:8px">取消关机</button>`;
  const btn = $("cancelPowerBtn");
  if (btn) {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/power/cancel", { method: "POST", body: "{}" });
        toast("已取消自动关机", 5000);
        refreshJobs();
      } catch (err) { toast("取消失败: " + err.message, 6000); }
    });
  }
}

/* ---------------------------------------------------- pre-encode check (G4) --- */

/**
 * Ask the backend whether this batch can actually be written, and act on the answer.
 *
 * Problems block (a read-only or full volume cannot be worked around); warnings ask
 * for confirmation, because "the disk is only 6% free" is the user's call, not ours.
 * Returns false when the start should be abandoned.
 */
async function preflightBatch(pathList) {
  let data;
  try {
    data = await api("/api/preflight", {
      method: "POST",
      body: JSON.stringify({
        paths: pathList,
        location: currentLocation(),
        out_dir: $("optOutDir") ? $("optOutDir").value.trim() : "",
        default_out_dir: state.settings.default_out_dir || "",
        mode: state.mode,
      }),
    });
  } catch (err) {
    // A check that cannot run must not stop the user's work.
    toast("编码前检查未能完成（已跳过）: " + err.message, 5000);
    return true;
  }

  const problems = [];
  const warnings = [];
  for (const item of data.items || []) {
    for (const p of item.problems || []) problems.push(`${baseName(item.path)}：${p}`);
    for (const w of item.warnings || []) {
      if (!warnings.includes(w)) warnings.push(w);
    }
  }

  if (problems.length) {
    const shown = problems.slice(0, 8).join("\n");
    const more = problems.length > 8 ? `\n…还有 ${problems.length - 8} 条` : "";
    window.alert(`无法开始：有 ${problems.length} 处问题需要先解决\n\n${shown}${more}`);
    renderSpaceNote(data);
    return false;
  }

  renderSpaceNote(data);

  if (warnings.length) {
    const disk = (data.items || []).find((i) => i.space && i.space.known);
    const detail = disk ? `\n\n${disk.space.message}` : "";
    if (!window.confirm(
      `编码前提醒：\n\n${warnings.slice(0, 4).join("\n")}${detail}\n\n仍要开始吗？`)) {
      return false;
    }
  }
  return true;
}

/** Colour-coded free-space line under the output location (Shutter Encoder's idea). */
function renderSpaceNote(data) {
  const el = $("spaceNote");
  if (!el) return;
  const space = (data.items || []).map((i) => i.space).find((s) => s && s.known);
  if (!space) { el.textContent = ""; el.className = "hint"; return; }
  el.textContent = `目标盘：${space.message}`;
  el.className = "hint space-" + (space.level || "ok");
}

/** Refresh the free-space line for the CURRENT output choice, without queueing. */
async function refreshSpaceNote() {
  const el = $("spaceNote");
  if (!el) return;
  const first = state.files.find((f) => f.media);
  if (!first) { el.textContent = ""; el.className = "hint"; return; }
  try {
    const data = await api("/api/preflight", {
      method: "POST",
      body: JSON.stringify({
        paths: [first.path],
        location: currentLocation(),
        out_dir: $("optOutDir") ? $("optOutDir").value.trim() : "",
        default_out_dir: state.settings.default_out_dir || "",
        mode: state.mode,
      }),
    });
    renderSpaceNote(data);
  } catch { /* advisory only */ }
}

/* -------------------------------------------------- black-bar detection (G2) --- */

/**
 * Show what autocrop would remove from the CURRENT file.
 *
 * Detection is per file (bars differ between videos), so this is a preview for the
 * selected one -- the actual crop for a batch is resolved per source at submit time.
 * The result is also what makes the checkbox honest: the user sees the percentage
 * before agreeing to lose it.
 */
async function checkCrop() {
  const entry = state.files.find((f) => f.path === state.activeFile)
    || state.files.find((f) => f.media);
  const note = $("cropNote");
  if (!entry) { toast("先添加一个文件"); return; }
  if (note) { note.textContent = "检测中…"; note.className = "hint"; }
  const btn = $("cropCheckBtn");
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/crop", {
      method: "POST", body: JSON.stringify({ path: entry.path }),
    });
    const c = data.crop || {};
    if (note) {
      note.textContent = c.message || (c.ok ? "检出黑边" : "未检出黑边");
      note.className = "hint " + (c.ok ? "space-warn" : "space-ok");
    }
    if (c.ok && $("optAutocrop")) {
      $("optAutocrop").checked = true;
      refreshPlan();
    }
    toast(c.message || "检测完成", 6000);
  } catch (err) {
    if (note) { note.textContent = "检测失败：" + err.message; note.className = "hint space-error"; }
    toast("检测失败: " + err.message, 6000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* -------------------------------------------------------------------- init */

function renderCapsBadge(caps) {
  const badge = $('capsBadge');
  if (!caps.available) {
    badge.textContent = 'FFmpeg 不可用'; badge.className = 'badge badge-err';
    $('capsError').textContent = caps.error || '未检测到 FFmpeg，无法压缩。请安装后重启。';
    $('capsError').classList.remove('hidden');
    return;
  }
  const hw = [];
  if (caps.encoders.hevc_nvenc) hw.push('NVENC HEVC');
  if (caps.encoders.av1_nvenc) hw.push('NVENC AV1');
  if (caps.encoders.libsvtav1) hw.push('SVT-AV1');
  const pending = caps.hardware_pending || [];
  badge.textContent = (hw.some(n => n.startsWith('NVENC')) ? '硬件编码可用' : '编码环境就绪')
    + (pending.length ? ' · 补充检测中' : caps.supplemental_error ? ' · 补充检测失败' : '');
  badge.className = 'badge badge-ok';
  badge.title = `FFmpeg ${caps.version} · ${hw.join(' / ')}\n${caps.ffmpeg}\n硬解: ${(caps.hwaccels || []).join(', ')}`
    + (pending.length ? `\n尚未确认: ${pending.join(', ')}；已验证的档位可以使用。` : '')
    + (caps.supplemental_error ? `\n${caps.supplemental_error}` : '');
}

let supplementalRead = null, supplementalDeadline = 0;
async function refreshSupplementalCaps() {
  if (!state.caps?.hardware_pending?.length || supplementalRead) return supplementalRead;
  if (Date.now() >= supplementalDeadline) {
    $('capsBadge').textContent = $('capsBadge').textContent.replace('补充检测中', '补充检测暂未完成');
    return;
  }
  supplementalRead = (async () => {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 4000);
    try {
      const data = await api('/api/caps', { signal: controller.signal });
      if (!data.caps?.available) throw new Error('补充检测读数不可用');
      state.caps = {...state.caps, ...data.caps};
      renderCapsBadge(state.caps);
      if (!state.caps.hardware_pending?.length) {
        // Read updated recommendations only. Never apply late limits/settings.
        const details = await api('/api/capabilities', { signal: controller.signal });
        if (details.hardware) {
          state.hardware = details.hardware;
          $('settingsPerfBasis').textContent = `推荐 GPU ${state.hardware.gpu} · CPU ${state.hardware.cpu}；${state.hardware.basis || ''}`;
          renderPerf();
        }
      }
    } catch {
      const message = '\n补充检测更新暂不可用，已确认的编码器仍可使用。';
      if (!$('capsBadge').title.includes(message)) $('capsBadge').title += message;
    } finally { clearTimeout(timer); }
  })();
  try { await supplementalRead; } finally {
    supplementalRead = null;
    if (state.caps?.hardware_pending?.length) setTimeout(refreshSupplementalCaps, 1000);
  }
}

async function boot() {
  state.initializing = true;
  $('capsBadge').textContent = '正在检测编码环境…';
  for (const id of ['pickBtn','pickDirBtn','settingsBtn','historyBtn']) $(id).disabled = true;
  // Start independent reads together; publish caps before dependent shell metadata.
  const capsRead = api('/api/caps'), profilesRead = api('/api/profiles');
  // Attach handlers immediately so a fast failure cannot become an unhandled rejection.
  const [capsResult, profilesResult] = await Promise.allSettled([capsRead, profilesRead]);
  try {
    if (capsResult.status === 'rejected') throw capsResult.reason;
    const caps = capsResult.value;
    state.caps = caps.caps;
    renderCapsBadge(state.caps);
  } catch (err) {
    $("capsBadge").textContent = "后端连接失败";
    $("capsBadge").className = "badge badge-err";
  }

  try {
    if (profilesResult.status === 'rejected') throw profilesResult.reason;
    const data = profilesResult.value;
    state.profiles = data.profiles;
    state.backendSupportsSimple = data.simple_mode === true;
    state.backendSupportsHandoff = data.queue_handoff === true;
    $("clearDoneBtn").disabled = !state.backendSupportsHandoff;
    if (!state.backendSupportsSimple || !state.backendSupportsHandoff) {
      $("capsError").textContent = "界面已更新，当前后端尚未启用新版操作台。请等待当前任务空闲后重新启动应用；现有队列继续按原参数运行。";
      $("capsError").classList.remove("hidden");
    }
    state.defaultProfile = data.default || "P3";
    state.profileId = state.defaultProfile;
    renderProfiles();
  } catch (err) {
    toast("档位加载失败: " + err.message);
  }

  await Promise.all([refreshJobs(), loadCapabilities(), loadSettings()]);
  document.querySelector(".topbar-right").prepend(document.querySelector(".tools-menu"));
  applyInterface();
  connectEvents();
  applyMode();
  applyQualityModeUI();
  state.initializing = false;
  for (const id of ['pickBtn','pickDirBtn']) $(id).disabled = !state.caps;
  updateStartButton();
  wireSettings();
  wireMemoryStatus();
  syncSettingsPanel();
  for (const id of ['settingsBtn','historyBtn']) $(id).disabled = false;
  window.__vcsReady = true;
  if (state.caps?.hardware_pending?.length) {
    supplementalDeadline = Date.now() + 30000;
    setTimeout(refreshSupplementalCaps, 500);
  }
  // Fetch the version once at startup (not only when the panel opens) so the badge and
  // the stale-page banner are present without the user having to hunt for them.
  loadAbout();
  // ...and keep checking. The scenario this exists for is "the backend was restarted
  // with newer code while this tab stayed open", which cannot be noticed at load time.
  // 2 minutes costs one tiny JSON request.
  setInterval(loadAbout, 120000);
  // Test hooks for the end-to-end probe: it has to be able to PROVE the mismatch path
  // renders a warning, and module-scope state is not reachable from the page otherwise.
  window.__vcsSetPageSig = (value) => {
    PAGE_ASSET_SIG = String(value || "");
    checkStalePage(state.version);
    return PAGE_ASSET_SIG;
  };
  window.__vcsPageSig = () => PAGE_ASSET_SIG;
}

/* ----------------------------------------------------------------- profiles */

function renderProfiles() {
  const box = $("profileList");
  box.innerHTML = "";
  const stars = (n) => "★".repeat(n) + "☆".repeat(5 - n);

  /**
   * Compression stars from the CANONICAL size field.
   *
   * The card used to read `p.ratio`, which had two meanings in one field: a 1..6 class code
   * on the P tiers and a compression MULTIPLE (3..58) on the A tiers. Sizing was fixed to
   * use `expected_fraction` (output ÷ source), and the rating follows the same field so a
   * card can never disagree with the number the estimate panel prints. Thresholds are the
   * P ladder's own breakpoints, so the P tiers keep the ratings they always had.
   */
  const compressStars = (profile) => {
    const f = Number(profile.expected_fraction);
    if (!isFinite(f) || f <= 0) return 3;
    if (f > 0.70) return 1;
    if (f > 0.45) return 2;
    if (f > 0.32) return 3;
    if (f > 0.18) return 4;
    return 5;
  };

  // Two families with different promises: P1-P8 trade speed vs compatibility, while
  // A0-A6 are the goal-driven ladder whose whole point is the cost list. Rendering
  // them as one flat list hid that difference.
  const groups = [
    { ids: ADVANCED_PROFILES, title: "高级手动档位" },
    { ids: EXTREME_PROFILES, title: "极限压缩 · 请留意画质与尺寸代价" },
    { ids: new Set(["P8"]), title: "工具 · 仅换封装" },
  ];

  for (const group of groups) {
    const members = state.profiles.filter((p) => group.ids.has(p.id) && (profileVisible(p) || p.id === state.profileId));
    if (!members.length) continue;
    const head = document.createElement("div");
    head.className = "profile-group";
    head.textContent = group.title;
    box.appendChild(head);

    for (const p of members) {
      const card = document.createElement("div");
      card.className = "profile-card" + (p.id === state.profileId ? " active" : "") +
        (p.available ? "" : " unavailable");
      card.title = p.available ? "" : `本机 ffmpeg 缺少 ${p.encoder} 编码器`;
      const lad = p.ladder || null;
      const ratioText = lad
        ? `预期 ${lad.expected_ratio[0]}–${lad.expected_ratio[1]}×`
        : null;
      const vmafText = lad
        ? (lad.expected_vmaf[0] > 0
            ? `VMAF ${lad.expected_vmaf[0]}–${lad.expected_vmaf[1]}`
            : `VMAF <${lad.expected_vmaf[1]}`)
        : null;
      card.innerHTML = `
        <div class="profile-id">${p.id}</div>
        <div>
          <div class="profile-name">${profileLabel(p)}${!profileVisible(p) ? "（当前项，已隐藏）" : ""} <span class="hint" style="font-weight:400">${p.name_en}</span></div>
          <div class="profile-sum">${p.summary}</div>
          <div class="profile-flags">
            <span class="flag ${p.compat >= 4 ? "good" : p.compat === 3 ? "mid" : "bad"}">兼容 ${stars(p.compat)}</span>
            <span class="flag ${compressStars(p) >= 4 ? "good" : "mid"}">压缩 ${stars(compressStars(p))}</span>
            <span class="flag">${p.speed}</span>
            ${ratioText ? `<span class="flag mid">${ratioText}</span>` : ""}
            ${vmafText ? `<span class="flag ${p.ladder.expected_vmaf[0] >= 90 ? "good" : p.ladder.expected_vmaf[0] >= 75 ? "mid" : "bad"}">${vmafText}</span>` : ""}
            ${p.max_edge ? `<span class="flag">≤${p.max_edge}p</span>` : ""}
            ${p.auto_effort ? `<span class="flag good" title="${escapeHtml(p.auto_effort_note || "")}">自动提精度 preset ${p.auto_effort}</span>` : ""}
            ${p.fps_cap ? `<span class="flag bad">降帧到 ${p.fps_cap}fps</span>` : ""}
            ${lad && lad.needs_confirmation ? `<span class="flag bad">需确认</span>` : ""}
            ${lad && !lad.measured ? `<span class="flag">倍率为外推</span>` : ""}
            ${p.effective_encoder && p.effective_encoder !== p.encoder
              ? `<span class="flag mid">本机改用 ${p.effective_encoder}</span>` : ""}
            ${p.estimated && !lad ? `<span class="flag">体积为估算</span>` : ""}
          </div>
          ${lad && lad.costs ? `<div class="profile-cost">${escapeHtml(lad.costs)}</div>` : ""}
        </div>`;
      card.addEventListener("click", () => {
        if (!profileVisible(p)) return;
        state.profileId = p.id;
        state.manualSeed = null;
        $("optCrf").value = "";
        for (const f of state.files) { f.seed = null; f.seedProfile = null; }
        renderProfiles();
        refreshPlan();
        if (lad && lad.needs_confirmation) {
          toast("该档位会明显牺牲画质（" + (lad.costs || "") + "）", 8000);
        }
      });
      box.appendChild(card);
    }
  }
}

/* -------------------------------------------------------------------- files */

function extOf(name) {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}

function addPaths(paths, source) {
  const added = [];
  const rejected = [];
  for (const raw of paths) {
    const path = String(raw || "").trim().replace(/^"|"$/g, "");
    if (!path) continue;
    if (!VIDEO_EXT.has(extOf(path))) continue;
    // Never let a browser fake path become a "file not found" card.
    if (!looksLikeRealPath(path)) { rejected.push(path); continue; }
    if (state.files.some((f) => f.path.toLowerCase() === path.toLowerCase())) continue;
    const entry = {
      path,
      name: path.split(/[\\/]/).pop(),
      media: null,
      error: "",
      source: source || "",
    };
    state.files.push(entry);
    added.push(entry);
  }
  if (rejected.length) {
    toast(rejectReason(rejected), 8000);
    showPathFallback(rejectReason(rejected));
  }
  renderFiles();
  updateStartButton();
  if (added.length) probeFiles(added);
  if (!state.activeFile && state.files.length) selectFile(state.files[0].path);
  togglePathFallback();
  return added.length;
}

/**
 * Is this a real file path the server could open?
 *
 * Browsers hand out FAKE absolute paths in two cases, and both used to reach the
 * probe and show up as a confusing "文件不存在 (WinError 3)" card:
 *   - C:\fakepath\<name>   (what a picked file reports)
 *   - a bare name          (what a dropped file reports)
 * Reject them here so the message can explain what actually happened.
 */
function looksLikeRealPath(p) {
  const value = String(p || "").trim();
  if (!value) return false;
  if (/^[a-zA-Z]:[\\/]/.test(value)) {          // E:\... or E:/...
    return !/^[a-zA-Z]:[\\/]fakepath[\\/]/i.test(value);
  }
  return /^\\\\/.test(value);                    // UNC \\server\share
}

function rejectReason(paths) {
  const fakes = paths.filter((p) => /fakepath/i.test(String(p)));
  if (fakes.length) {
    return "浏览器把文件路径伪造成了 C:\\fakepath\\…（网页无法获得真实路径）。"
      + "请改用「选择文件夹」或「打开收件箱」。";
  }
  return "拿不到这些文件的完整路径（拖拽/选择在浏览器里拿不到真实路径）。"
    + "请改用「选择文件夹」或「打开收件箱」。";
}

/** Turn a File into an absolute path.
 *
 * A plain browser never gives an absolute path. For folder picks it gives
 * `webkitRelativePath`, which ALREADY STARTS WITH THE SELECTED FOLDER NAME
 * (picking E:\x\Yuluer yields "Yuluer/a.mp4"), so naively joining it with the base
 * duplicates that folder: E:\x\Yuluer\Yuluer\a.mp4. Drop the leading segment when
 * it matches the last segment of the base path. */
function fileToPath(file, baseDir) {
  if (file.path) return file.path;                       // Electron/WebView
  const rel = (file.webkitRelativePath || file.name).replace(/\\/g, "/");
  if (!baseDir) return rel;

  const sep = baseDir.includes("\\") ? "\\" : "/";
  const base = baseDir.replace(/[\\/]+$/, "");
  const baseLeaf = base.split(/[\\/]/).pop().toLowerCase();

  let parts = rel.split("/").filter((p) => p && p !== ".");
  if (baseLeaf && parts.length > 1 && parts[0].toLowerCase() === baseLeaf) {
    parts = parts.slice(1);          // drop the duplicated folder name
  }
  return base + sep + parts.join(sep);
}

function filesFromInput(fileList, baseDir) {
  const out = [];
  for (const f of fileList) out.push(fileToPath(f, baseDir));
  return out;
}

/** Recursively walk dropped directory entries (Chromium FileSystemEntry API).
 *
 * Dragging a FOLDER works where dragging loose files cannot: the entry API exposes
 * the full path. This is the practical fix for "the browser won't give us paths". */
function walkEntry(entry, collected, depth = 0) {
  return new Promise((resolve) => {
    if (!entry || depth > 12) { resolve(); return; }
    if (entry.isFile) {
      entry.file((file) => {
        collected.push({ path: entry.fullPath || file.name, file });
        resolve();
      }, () => resolve());
      return;
    }
    const reader = entry.createReader();
    const readBatch = () => {
      reader.readEntries(async (entries) => {
        if (!entries.length) { resolve(); return; }
        for (const child of entries) await walkEntry(child, collected, depth + 1);
        readBatch();   // directories can come back in several batches
      }, () => resolve());
    };
    readBatch();
  });
}

async function handleDrop(event) {
  const dt = event.dataTransfer;
  if (!dt) return 0;

  // 1) A folder (or files) dropped with entry data -> full paths available.
  const items = dt.items ? Array.from(dt.items) : [];
  const entries = items
    .map((it) => (it.kind === "file" && it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean);

  if (entries.length) {
    const collected = [];
    for (const entry of entries) await walkEntry(entry, collected);
    const paths = collected
      .map((c) => c.file.path || c.path)          // entry.fullPath is /-rooted and incomplete
      .filter(Boolean);
    // entry.fullPath is relative to the drop root, so only use it for display; we
    // still need a real absolute path. A dropped folder has no absolute path in a
    // plain browser, so fall back to asking the user to pick the folder.
    const usable = paths.filter((p) => /^[A-Za-z]:[\\/]|^\\\\/.test(p));
    if (usable.length) return addPaths(usable);
  }

  // 2) Plain file drop: paths are unavailable in a browser.
  const names = Array.from(dt.files || []).map((f) => f.name);
  if (names.length) {
    showPathFallback(`浏览器不提供完整路径（拖入了 ${names.length} 个文件）。`
      + `请改用「选择文件夹」——这样能自动识别全部视频；或把路径粘贴到下面的框里。`);
    return 0;
  }
  return 0;
}

async function probeFiles(entries, { checkSeek = true } = {}) {
  entries = entries.filter((f) => state.files.includes(f) && !f.probing);
  if (!entries.length) return;
  for (const f of entries) { f.probing = true; if (f.retryable) f.notice = "正在重试文件探测…"; }
  try {
    const data = await api("/api/probe", {
      method: "POST",
      body: JSON.stringify({ paths: entries.map((e) => e.path), check_seek: checkSeek }),
    });
    data.items.forEach((item, i) => {
      const entry = entries[i];
      if (!entry || !state.files.includes(entry)) return;
      entry.media = item.ok ? item : null;
      entry.error = item.ok ? "" : (item.error || "探测失败");
      entry.notice = ""; entry.retryable = false;
      entry.seek = item.seek || null;
    });
    renderFiles();
    maybeWarnBroken();
  } catch (err) {
    entries.forEach((e) => {
      e.retryable = !!err.retryable;
      if (err.retryable) { e.error = ""; e.notice = "探测暂时等待资源：" + err.message; }
      else { e.error = err.message; e.media = null; e.notice = ""; }
    });
    renderFiles();
  } finally {
    for (const f of entries) f.probing = false;
    renderFiles(); // Re-enable the retry controls after the request settles.
    // Media info only exists after probing, and the start button requires it --
    // forgetting this left the button permanently disabled after adding files.
    updateStartButton();
    refreshPlan();
    refreshMemory({ retry: false });
  }
}

/** The manual path box is a fallback: hide it once files are actually in the list. */
function togglePathFallback() {
  const box = $("pathFallback");
  if (state.files.length > 0) {
    box.classList.add("hidden");
  } else if (state.needsPathFallback) {
    box.classList.remove("hidden");
  }
}

function showPathFallback(reason) {
  state.needsPathFallback = true;
  $("pathFallback").classList.remove("hidden");
  if (reason) toast(reason, 6000);
}

function maybeWarnBroken() {
  const broken = state.files.filter((f) => f.seek && f.seek.verdict === "broken");
  if (!broken.length) return;
  if (state.mode !== "repair") {
    toast(`检测到 ${broken.length} 个文件无法拖动进度条，可切到「修复」模式处理`, 6000);
  }
}

function seekBadge(seek) {
  if (!seek) return "";
  if (seek.verdict === "checking") {
    return `<span class="seek-badge" title="正在扫描关键帧">检测中…</span>`;
  }
  if (seek.verdict === "broken") {
    return `<span class="seek-badge seek-broken" title="${escapeHtml((seek.reasons || []).join('；'))}">拖不动 · 需修复</span>`;
  }
  if (seek.verdict === "coarse") {
    return `<span class="seek-badge seek-coarse" title="${escapeHtml((seek.reasons || []).join('；'))}">关键帧偏疏</span>`;
  }
  if (seek.verdict === "seekable") {
    return `<span class="seek-badge seek-ok">可拖动</span>`;
  }
  return "";
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/**
 * Wire the two ways a modal is expected to close: Esc, and a click on the backdrop.
 *
 * Two defects this replaces:
 *   * Esc did nothing. The only way out was clicking the X or the backdrop, which is
 *     the first thing a keyboard user tries and the reason "the dialog won't close"
 *     gets reported.
 *   * A click that STARTED inside the dialog (dragging the compare slider, selecting
 *     text in the log) and ENDED on the backdrop closed it. The `click` event lands on
 *     the backdrop because that is where the button came up, so releasing a drag
 *     outside the panel threw the user out of the view they were working in. Requiring
 *     the press to start on the backdrop as well fixes that.
 *
 * `onClose` runs once, before the node is removed, so callers can release blob URLs and
 * pause media.
 */
function wireOverlayDismiss(overlay, onClose) {
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    try { if (onClose) onClose(); } finally { overlay.remove(); }
  };
  const onKey = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  };
  let pressedBackdrop = false;
  overlay.addEventListener("mousedown", (event) => {
    pressedBackdrop = event.target === overlay;
  });
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && pressedBackdrop) close();
  });
  document.addEventListener("keydown", onKey);
  // Expose the closer so a button inside the panel (the X) can use the same path.
  overlay.vcsClose = close;
  return close;
}

function renderFiles() {
  const box = $("fileList");
  $("fileCount").textContent = String(state.files.length);
  if (!state.files.length) {
    box.innerHTML = `<p class="empty">还没有文件</p>`;
    return;
  }
  box.innerHTML = "";
  for (const f of state.files) {
    const card = document.createElement("div");
    card.dataset.path = f.path;
    card.className = "file-card" + (f.path === state.activeFile ? " active" : "") + (f.error ? " bad" : f.retryable ? " waiting" : "");
    const m = f.media;
    let meta = `<span class="hint">${f.retryable ? '等待安全探测' : '探测中…'}</span>`;
    if (f.error) {
      meta = `<span style="color:var(--error)">${escapeHtml(f.error)}</span>`;
    } else if (m) {
      const s = m.summary;
      // `s.resolution` is the DISPLAY size (a portrait phone clip is shown 360x640 even
      // though it is stored 640x360). Say so when it differs, or the number looks wrong
      // to anyone who checks the file's properties.
      const rotated = s.rotation
        ? `<span class="tag" title="源带 ${s.rotation}° 旋转标记（存储 ${s.coded_resolution}）；编码时会把旋转烧进画面">已旋转 ${s.rotation}°</span>`
        : "";
      const shot = s.creation_time
        // Escaped: `creation_time` is a CONTAINER TAG, i.e. whatever the file says,
        // not a value this app produced. A tag like `<img src=x onerror=...>` used to
        // be interpolated straight into innerHTML.
        ? `<span class="tag" title="容器里的拍摄时间，压完会保留">📅 ${escapeHtml(String(s.creation_time).slice(0, 10))}</span>`
        : "";
      meta = `
        <span>${s.resolution}</span>
        ${rotated}
        <span>${s.codec}</span>
        <span>${s.fps ? s.fps.toFixed(2) + " fps" : "帧率未知"}</span>
        <span>${fmtDuration(m.duration)}</span>
        <span>${fmtBytes(m.size_bytes)}</span>
        <span>${bitrateText(m)}</span>
        ${shot}
        <span class="tag">${s.audio_tracks} 音轨</span>
        <span class="tag">${s.subtitle_tracks} 字幕</span>
        ${s.bit_depth >= 10 ? `<span class="tag">${s.bit_depth}-bit</span>` : ""}
        ${s.hdr ? `<span class="tag hdr">HDR</span>` : ""}`;
    }
    card.innerHTML = `
      <div class="file-name">${escapeHtml(f.name)}${seekBadge(f.seek)}</div>
      <button class="file-remove" title="移除">✕</button>
      <div class="file-meta">${meta}</div>
      <div class="file-notice${f.notice || f.error ? "" : " hidden"}">${escapeHtml(f.notice || "")}
        <button class="ghost probe-retry" type="button"${f.probing ? " disabled" : ""}>重新探测</button></div>
      <div class="file-plan${f.planError ? " blocked" : ""}">${f.planError ? escapeHtml(f.planError) : escapeHtml(f.planText || "等待计划解析")}</div>
      <div class="file-own${!state.settings.show_advanced && !state.settings.show_extreme && !f.override ? " hidden" : ""}">
        <button class="ghost own-toggle" title="只给这一个文件换档位/参数">${f.override ? "已单独设置 ▾" : "单独设置 ▾"}</button>
        <span class="hint own-summary">${fileOverrideSummary(f)}</span>
      </div>
      <div class="own-editor hidden">
        <label>档位
          <select class="own-profile">
            <option value="">跟随上方</option>
            ${profileOptionHtml(f.override?.profile)}
          </select>
        </label>
        <label class="${!state.settings.show_advanced ? "hidden" : ""}">画质 CRF
          <input type="number" class="own-crf" min="0" max="63" placeholder="跟随"
                 value="${f.override && f.override.crf !== undefined ? f.override.crf : ""}">
        </label>
        <label class="${!state.settings.show_advanced ? "hidden" : ""}">缩放到（长边，0=不动）
          <input type="number" class="own-edge" min="0" step="16" placeholder="跟随"
                 value="${f.override && f.override.max_long_edge ? f.override.max_long_edge : ""}">
        </label>
        <button class="ghost own-clear">清除单独设置</button>
      </div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest('button, input, select, label, .own-editor')) return;
      selectFile(f.path);
    });
    card.querySelector('.probe-retry').addEventListener('click', (e) => {
      e.stopPropagation(); probeFiles([f], { checkSeek: false });
    });
    card.querySelector(".file-remove").addEventListener("click", () => {
      state.files = state.files.filter((x) => x.path !== f.path);
      if (state.activeFile === f.path) state.activeFile = state.files[0]?.path || null;
      renderFiles();
      updateStartButton();
      refreshPlan();
    });

    // ---- per-file settings (F12a) ----
    // Scoped to ONE file, for the case where the batch-level choice is right for
    // everything except this one. Kept on the file entry and sent at submit time, so
    // it survives re-renders without a round trip.
    const editor = card.querySelector(".own-editor");
    card.querySelector(".own-toggle").addEventListener("click", (ev) => {
      ev.stopPropagation();
      editor.classList.toggle("hidden");
    });
    const readOwn = () => {
      const profile = card.querySelector(".own-profile").value;
      const crfRaw = card.querySelector(".own-crf").value.trim();
      const edgeRaw = card.querySelector(".own-edge").value.trim();
      const next = {};
      if (profile) next.profile = profile;
      if (crfRaw !== "") next.crf = Number(crfRaw);
      if (edgeRaw !== "" && Number(edgeRaw) > 0) next.max_long_edge = Number(edgeRaw);
      f.override = Object.keys(next).length ? next : null;
      card.querySelector(".own-summary").textContent = fileOverrideSummary(f);
      card.querySelector(".own-toggle").textContent =
        f.override ? "已单独设置 ▾" : "单独设置 ▾";
      refreshPlan();
    };
    for (const sel of [".own-profile", ".own-crf", ".own-edge"]) {
      card.querySelector(sel).addEventListener("change", (ev) => {
        ev.stopPropagation();
        readOwn();
      });
    }
    card.querySelector(".own-clear").addEventListener("click", (ev) => {
      ev.stopPropagation();
      f.override = null;
      card.querySelector(".own-profile").value = "";
      card.querySelector(".own-crf").value = "";
      card.querySelector(".own-edge").value = "";
      card.querySelector(".own-summary").textContent = fileOverrideSummary(f);
      card.querySelector(".own-toggle").textContent = "单独设置 ▾";
      refreshPlan();
    });
    box.appendChild(card);
  }
}

/** One-line summary of a file's own settings, so the card states its deviation. */
function fileOverrideSummary(f) {
  const o = f.override;
  if (!o) return "";
  const bits = [];
  if (o.profile) bits.push(`档位 ${o.profile}`);
  if (o.crf !== undefined) bits.push(`CRF ${o.crf}`);
  if (o.max_long_edge) bits.push(`长边 ${o.max_long_edge}`);
  return bits.join(" · ");
}

function selectFile(path) {
  state.activeFile = path;
  renderFiles();
  const entry = state.files.find((f) => f.path === path);
  if (entry && (!entry.media || entry.retryable)) probeFiles([entry], { checkSeek: false });
  else refreshPlan();
  refreshMemory({ retry: false });
  updateStartButton();
}

function updateStartButton() {
  const ready = state.files.some((f) => f.media);
  const saving = state.initializing || ['compatibility','slow_av1','location','default_out_dir'].some(key => settingsDraft.has(key));
  $("startBtn").disabled = saving || state.submitting || !state.backendSupportsSimple || !state.backendSupportsHandoff || !ready || (state.mode === "compress" && (!state.batchEstimate || state.batchEstimate.complete === false));
  $("previewBtn").disabled = saving || !ready;
  $("targetSolveBtn").disabled = saving || !ready;
  $("measureBtn").disabled = saving || state.measuring || !state.files.find((f) => f.path === state.activeFile)?.media;
}

/* --------------------------------------------------- native dialog dispatch ---
 * Two implementations exist and both are needed:
 *   - pywebview's own dialog when running in the desktop shell. It is parented to
 *     the app window (so it cannot open BEHIND it, which was a real complaint) and
 *     it is DPI-aware (a shelled-out WinForms dialog came out blurry on HiDPI).
 *   - the backend's PowerShell dialog elsewhere (Edge --app window, browser tab).
 */
async function pickDialog(kind, initialDir) {
  const api = window.pywebview && window.pywebview.api;
  if (api && typeof api.pick_paths === "function") {
    try {
      const result = await api.pick_paths(kind, initialDir || "");
      return result || { cancelled: true };
    } catch (err) {
      toast("桌面对话框失败，改用备用方式: " + err.message, 6000);
    }
  }
  return pickNative(kind, initialDir);
}

/* --------------------------------------------------- backend dialog (async) ---
 * The dialog runs outside the HTTP request, so a window that ends up behind the
 * app can never hang the page. The answer arrives as an SSE "picked" event (with a
 * poll backstop in case SSE is unavailable).
 */
function pickNative(kind, initialDir) {
  return new Promise(async (resolve) => {
    const token = `pk${Date.now()}${Math.floor(Math.random() * 1000)}`;
    const waiter = { token, resolve };
    state.pickWaiters = state.pickWaiters || [];
    state.pickWaiters.push(waiter);

    // `stopPoll` is created BEFORE `finish` so that EVERY way of settling the wait --
    // SSE event, poll hit, timeout, error -- also stops the backstop poll. The timeout
    // path used to call `finish` directly, which left a 2.5 s poll running forever
    // against a token the server had already forgotten: invisible traffic that never
    // stops, and a waiter that is never removed from the queue.
    let poll = null;
    let timer = null;
    const stopPoll = () => {
      if (poll !== null) { clearInterval(poll); poll = null; }
      if (timer !== null) { clearTimeout(timer); timer = null; }
      delete (state.pickPollers || {})[token];
    };

    const finish = (result) => {
      stopPoll();
      state.pickWaiters = (state.pickWaiters || []).filter((w) => w !== waiter);
      resolve(result || { cancelled: true });
    };
    timer = setTimeout(() => finish({ cancelled: true, timeout: true }), 600000);
    const settle = (result) => finish(result);

    // poll backstop (covers a dropped SSE connection)
    poll = setInterval(async () => {
      try {
        const data = await api("/api/pick/result", {
          method: "POST", body: JSON.stringify({ token }),
        });
        if (!data.pending && data.result) settle(data.result);
      } catch { /* keep trying until the timeout */ }
    }, 2500);
    state.pickPollers = state.pickPollers || {};
    // The poller's stop function is the SAME one `finish` calls -- one owner, so a
    // token can never end up with two live timers.
    state.pickPollers[token] = stopPoll;

    try {
      await api(kind === "folder" ? "/api/pick/folder" : "/api/pick/files", {
        method: "POST",
        body: JSON.stringify({ token, initial_dir: initialDir || "" }),
      });
    } catch (err) {
      settle({ ok: false, cancelled: false, error: err.message });
    }
  });
}

function resolvePickEvent(token, payload) {
  const list = state.pickWaiters || [];
  const index = list.findIndex((w) => w.token === token);
  if (index < 0) return false;
  const waiter = list[index];
  state.pickWaiters.splice(index, 1);
  const stopper = (state.pickPollers || {})[token];
  if (stopper) { stopper(); delete state.pickPollers[token]; }
  waiter.resolve(payload || { cancelled: true });
  return true;
}



/* --------------------------------------------- native drag & drop (pywebview) ---
 * Inside the PyWebView window, pywebview REPLACES the HTML5 drop handling with its
 * own, and the real absolute path only exists on the Python side (as
 * `pywebviewFullPath`). So desktop.py buffers the dropped paths and calls
 * __vcsDrainDropped(), which pulls them through the JS API into the normal add flow.
 * Doing this in a DOM 'drop' listener instead would pair paths by basename and
 * mis-assign same-named files.
 */
window.__vcsAddPaths = function (paths) {
  const list = Array.isArray(paths) ? paths : [paths];
  const n = addPaths(list, "拖拽");
  if (!n && list.length) toast("拖入的内容里没有可识别的视频文件", 5000);
  return n;
};

window.__vcsDrainDropped = function () {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.take_dropped) return;
  api.take_dropped().then((list) => {
    if (list && list.length) window.__vcsAddPaths(list);
  }).catch(() => { /* nothing to drain */ });
};

/** True when hosted by the PyWebView desktop shell. */
function isDesktopShell() {
  return window.__vcsDesktopShell === true || !!(window.pywebview && window.pywebview.api);
}

/** Called by desktop.py after it has flagged the shell as ready. */
window.__vcsOnDesktopShell = function () {
  // Shell metadata is process-wide; late shell events update presentation only.
  loadCapabilities();
};


function isAppWindow() {
  const params = new URLSearchParams(location.search);
  if (params.get("app") === "1") return true;
  try { return window.matchMedia("(display-mode: standalone)").matches; } catch { return false; }
}

let capabilitiesRead = null, capabilitiesLoaded = false;
async function loadCapabilities() {
  if (!state.caps) return;
  if (capabilitiesLoaded) { renderShellCapabilities(); return; }
  if (capabilitiesRead) return capabilitiesRead;
  capabilitiesRead = (async () => {
  const readRevision = settingsRevision;
  try {
    const data = await api("/api/capabilities");
    state.caps.nativeDialogs = !!data.native_dialogs;
    state.caps.inboxDir = data.inbox_dir || "";
    state.hardware = data.hardware || null;
    if (readRevision === settingsRevision) state.limits = data.limits || state.settings.limits || { gpu: 1, cpu: 1 };
    capabilitiesLoaded = true;
  } catch {
    state.caps.nativeDialogs = false;
  }
  renderPerf();
  renderShellCapabilities();
  })();
  try { await capabilitiesRead; } finally { capabilitiesRead = null; }
}

function renderShellCapabilities() {
  // Expose the capabilities for the browser probe (read-only diagnostic).
  window.__vcsCaps = { nativeDialogs: !!state.caps.nativeDialogs,
                       inboxDir: state.caps.inboxDir || "" };

  if (state.caps.nativeDialogs) {
    $("pickNote").textContent = isDesktopShell()
      ? "桌面窗口模式：可以直接把文件或文件夹拖进窗口，也可以用上面两个按钮（都是原生对话框）。"
      : "提示：上面两个按钮用的是系统原生对话框，路径 100% 准确。";
  } else {
    $("pickNote").textContent =
      "当前是浏览器模式：拖拽与文件选择都无法得到完整路径（浏览器安全限制）。"
      + "请用「打开收件箱」把文件拖进去，或先把路径粘贴到下框。";
    $("pathFallback").classList.remove("hidden");
  }
  $("inboxPath").value = state.caps.inboxDir || "";
  if (isAppWindow() || isDesktopShell()) {
    document.body.classList.add("app-window");
  }
  if (isDesktopShell()) {
    $("dropHint").className = "hint";
    $("dropHint").textContent =
      "直接把视频或整个文件夹拖到窗口里即可（桌面窗口能拿到真实路径）。";
  }
}

/** Add every video inside a folder, using a real absolute path. */
async function addFolderFromPath(dir) {
  try {
    const data = await api("/api/browse", {
      method: "POST",
      body: JSON.stringify({ path: dir }),
    });
    const vids = (data.entries || []).filter(
      (e) => !e.dir && VIDEO_EXT.has(extOf(e.name)));
    const subdirs = (data.entries || []).filter((e) => e.dir);
    if (!vids.length && !subdirs.length) {
      toast("该文件夹里没有视频文件");
      return;
    }
    const n = addPaths(vids.map((e) => e.path));
    toast(subdirs.length
      ? `已加入 ${n} 个视频（子文件夹未展开，如需要请用「浏览目录…」逐个进入）`
      : `已加入 ${n} 个视频`, 5000);
  } catch (err) {
    toast("读取文件夹失败: " + err.message, 6000);
  }
}

/* ------------------------------------------------- 设置 / 关于（规范化） --- */

/** This page's own asset fingerprint, injected by the server into index.html.
 *  Compared against `/api/version` to detect a stale tab.
 *
 *  `let` (not `const`) and reachable through `window.__vcsSetPageSig` so the end-to-end
 *  probe can prove the mismatch path actually renders a warning. An untested
 *  "we detect staleness" claim would be exactly the kind of plausible-looking fiction
 *  this feature exists to prevent. */
let PAGE_ASSET_SIG = (document.querySelector('meta[name="vcs-asset-signature"]')
  || {}).content || "";

/**
 * Compare this page's build against the build the server is serving right now.
 *
 * The failure this catches: the backend was restarted with newer code (a normal event
 * here -- start.ps1 and the desktop launcher both replace a stale instance) while the
 * tab stayed open. The page then keeps running the old JS against a new server, and
 * every "the change had no effect" report starts exactly here.
 */
function checkStalePage(info) {
  const bar = $("stalePageBar");
  if (!bar) return false;
  const serverSig = (info && info.asset_signature) || "";
  const stale = !!(PAGE_ASSET_SIG && serverSig && PAGE_ASSET_SIG !== serverSig);
  if (!stale) {
    bar.classList.add("hidden");
    return false;
  }
  bar.classList.remove("hidden");
  bar.innerHTML =
    "这个页面是<b>旧版本</b>（页面资源 " + escapeHtml(PAGE_ASSET_SIG)
    + "，服务端 " + escapeHtml(serverSig) + "）——后端已经重新启动过了。"
    + '请按 <b>Ctrl+F5</b> 强制刷新，否则这里的行为可能不是当前代码的行为。'
    + ' <button id="staleReloadBtn" class="ghost">立即刷新</button>';
  const btn = $("staleReloadBtn");
  if (btn) btn.addEventListener("click", () => window.location.reload());
  return true;
}

function fmtUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

function fmtWhen(epochSeconds) {
  if (!epochSeconds) return "—";
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadAbout() {
  let info = null;
  try {
    info = await api("/api/version");
  } catch (err) {
    if ($("aboutTable")) {
      $("aboutTable").innerHTML =
        `<tbody><tr><td colspan="2" class="hint">读取版本信息失败：${escapeHtml(err.message)}</td></tr></tbody>`;
    }
    return null;
  }
  state.version = info;
  $('settingsVersion').textContent = 'v' + (info.version || '?');
  if ($("versionBadge")) $("versionBadge").textContent = "v" + (info.version || "?");

  const ff = info.ffmpeg || {};
  const limits = info.limits || {};
  const stale = !!(PAGE_ASSET_SIG && info.asset_signature
                   && PAGE_ASSET_SIG !== info.asset_signature);
  const rows = [
    ["软件版本", `v${escapeHtml(info.version || "?")}`],
    ["前端资源版本", `${escapeHtml(info.asset_signature || "?")}`
      + (stale ? ' <span class="badge badge-warn">本页是旧版本</span>' : "")],
    ["后端代码时间", escapeHtml(fmtWhen(info.backend_mtime))],
    ["本次运行时长", escapeHtml(fmtUptime(info.uptime))],
    ["进程号 / 进行中任务", `${info.pid ?? "?"} / ${info.active_jobs ?? 0}`],
    ["FFmpeg", ff.available
      ? `${escapeHtml(ff.version || "?")}<br><span class="hint">${escapeHtml(ff.path || "")}</span>`
      : '<span class="job-error">不可用</span>'],
    ["Python / 平台", `${escapeHtml(info.python || "?")} · ${escapeHtml(info.platform || "?")}`],
    ["并行上限", `GPU ${limits.gpu ?? "?"} · CPU ${limits.cpu ?? "?"}`],
    ["归档目录", `<span class="hint">${escapeHtml(info.store_dir || "")}</span>`],
    ["收件箱", `<span class="hint">${escapeHtml(info.inbox_dir || "")}</span>`],
  ];
  if ($("aboutTable")) {
    $("aboutTable").innerHTML = "<tbody>"
      + rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")
      + "</tbody>";
  }
  checkStalePage(info);
  return info;
}

const settingsDraft = new Map(), settingsFailures = new Map();
let settingsSerial = Promise.resolve(), settingsRevision = 0;
const settingFields = {
  show_advanced: 'setShowAdvanced', show_extreme: 'setShowExtreme',
  compatibility: 'compatibilityChoice', slow_av1: 'slowAv1Choice',
  'limits.gpu': 'setLimitGpu', 'limits.cpu': 'setLimitCpu',
  deep_verify: 'setDeepVerify', vmaf_check: 'setVmafCheck',
  memory_guard: 'setMemoryGuard', memory_margin_mb: 'setMemoryMargin', memory_low_water_mb: 'setMemoryLowWater',
  default_out_dir: 'optDefaultDir',
};

function settingValue(key, saved) {
  const draft = settingsDraft.get(key);
  return draft && draft.value !== undefined ? draft.value : saved;
}

function renderSettingsSaveStatus() {
  const failed = settingsFailures.size > 0;
  const message = settingsDraft.size ? '正在保存…' : failed ? '部分更改未保存，已恢复最后成功值' : '已保存';
  for (const id of ['settingsSaveStatus', 'preferencesSaveStatus']) {
    $(id).textContent = message;
    $(id).classList.toggle('save-error', failed);
  }
  $('settingsRetryBtn').classList.toggle('hidden', !failed || settingsDraft.size > 0);
  for (const [key, id] of Object.entries(settingFields)) {
    const field = $(id), failure = settingsFailures.get(key);
    field.setAttribute('aria-invalid', String(!!failure));
    let error = $(id + 'SaveError');
    if (!error && field.parentElement) {
      error = document.createElement('span'); error.id = id + 'SaveError';
      error.className = 'field-save-error hidden'; field.parentElement.appendChild(error);
    }
    if (error) { error.textContent = failure ? '未保存：' + failure.message : ''; error.classList.toggle('hidden', !failure); }
  }
}

function syncSettingsPanel({ force = [] } = {}) {
  const s = state.settings, limits = state.limits || {gpu:1,cpu:1};
  const values = {show_advanced:s.show_advanced === true,show_extreme:s.show_extreme === true,
    compatibility:s.compatibility || 'modern',slow_av1:s.slow_av1 === true,
    'limits.gpu':limits.gpu,'limits.cpu':limits.cpu,deep_verify:s.deep_verify === true,vmaf_check:s.vmaf_check === true,
    memory_guard:s.memory_guard !== false,memory_margin_mb:s.memory_margin_mb ?? 2500,memory_low_water_mb:s.memory_low_water_mb ?? 1500,
    default_out_dir:s.default_out_dir || ''};
  for (const [key,id] of Object.entries(settingFields)) {
    const el = $(id);
    if (settingsDraft.has(key) || (el === document.activeElement && !force.includes(key))) continue;
    if (typeof values[key] === 'boolean') el.checked = values[key]; else el.value = values[key];
  }
  if (!settingsDraft.has('location')) {
    for (const radio of document.querySelectorAll('input[name="location"]')) radio.checked = radio.value === (s.location || 'beside');
    applyLocationUI();
  }
  const hw = state.hardware;
  if (hw) {
    $('setLimitGpu').max = hw.gpu_cap || 8; $('setLimitCpu').max = hw.cpu_cap || 4;
    $('settingsPerfBasis').textContent = `推荐 GPU ${hw.gpu} · CPU ${hw.cpu}；${hw.basis || ''}`;
  }
  $('setResetLimits').disabled = !hw;
  $('setMemoryHint').textContent = s.memory_guard === false ? '保护已关闭；手动暂停保持暂停' :
    `保护已启用 · 安全余量 ${s.memory_margin_mb ?? 2500} MB · 恢复水位 ${(s.memory_low_water_mb ?? 1500)+1000} MB`;
  $('memoryPolicySummary').textContent = s.memory_guard === false ? '内存保护关闭' : '内存保护开启';
  renderPerf(); renderVerifyPanel();
  $('settingsRuntimeSummary').textContent = `${$('perfSummary').textContent} · ${$('verifySummary').textContent} · ${$('memoryPolicySummary').textContent}`;
  renderSettingsSaveStatus();
}

function savePreference(patch) {
  const body = JSON.parse(JSON.stringify(patch)), revision = ++settingsRevision;
  const entries = Object.entries(body).flatMap(([key,value]) => key === 'limits' ? Object.entries(value).map(([k,v])=>['limits.'+k,v]) :
    [[key,value]]);
  const retrying = entries.some(([key]) => settingsFailures.has(key));
  for (const [key,value] of entries) { settingsDraft.set(key,{revision,value}); settingsFailures.delete(key); }
  renderSettingsSaveStatus();
  updateStartButton();
  const run = async () => {
    let saved = false;
    try {
      const data = await api('/api/settings', {method:'POST',body:JSON.stringify(body)});
      if (!data?.settings || typeof data.settings !== 'object' || Array.isArray(data.settings)) throw new Error('未收到有效的保存确认');
      state.settings = data.settings; state.limits = data.limits || state.limits;
      saved = true;
      if (retrying) toast('设置已保存', 2500);
    } catch (err) {
      for (const [key,value] of entries) if (settingsDraft.get(key)?.revision === revision) settingsFailures.set(key,{value,message:err.message});
      toast('设置未保存：' + err.message, 8000);
    } finally {
      const settled = [];
      for (const [key] of entries) if (settingsDraft.get(key)?.revision === revision) { settingsDraft.delete(key); settled.push(key); }
      syncSettingsPanel({force:settled});
      if (saved && ['show_advanced','show_extreme','compatibility','slow_av1'].some(key=>key in body)) applyInterface();
      if (['compatibility','slow_av1','location','default_out_dir'].some(key=>key in body)) refreshPlan();
      updateStartButton();
    }
    return saved;
  };
  const result = settingsSerial.then(run);
  settingsSerial = result.catch(() => {});
  return result;
}

function retrySettings() {
  const patch = {};
  for (const [key,item] of settingsFailures) {
    if (key.startsWith('limits.')) (patch.limits ||= {})[key.slice(7)] = item.value;
    else patch[key] = item.value;
  }
  if (Object.keys(patch).length) return savePreference(patch);
}

function openSettings(runtime = false) {
  const overlay = $("settingsOverlay");
  if (!overlay) return;
  overlay.classList.remove("hidden");
  syncSettingsPanel();
  if (runtime === true) $('settingsRuntime').open = true;
  loadAbout();
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  const first = overlay.querySelector("button, input, select");
  if (first) first.focus();
}

function closeSettings() {
  const overlay = $("settingsOverlay");
  if (overlay) overlay.classList.add("hidden");
}

function wireSettings() {
  $("historyBtn").addEventListener("click", () => {
    $("historyPanel").open = true; $("historyPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  if ($("settingsBtn")) $("settingsBtn").addEventListener("click", () => openSettings());
  $('runtimeSettingsBtn').addEventListener('click', () => openSettings(true));
  if ($("settingsClose")) $("settingsClose").addEventListener("click", closeSettings);
  if ($("settingsOverlay")) {
    $("settingsOverlay").addEventListener("click", (event) => {
      // Same rule as the other modals: the press has to start on the backdrop.
      if (event.target === $("settingsOverlay")) closeSettings();
    });
  }
  for (const [id,key] of Object.entries({setShowAdvanced:'show_advanced',setShowExtreme:'show_extreme',setDeepVerify:'deep_verify',setVmafCheck:'vmaf_check',setMemoryGuard:'memory_guard',slowAv1Choice:'slow_av1'})) {
    $(id).addEventListener('change', () => savePreference({[key]:$(id).checked}));
  }
  $('compatibilityChoice').addEventListener('change', () => savePreference({compatibility:$('compatibilityChoice').value}));
  for (const [id,pool] of [['setLimitGpu','gpu'],['setLimitCpu','cpu']]) $(id).addEventListener('change', () => savePreference({limits:{[pool]:Number($(id).value)}}));
  for (const id of ['setMemoryMargin','setMemoryLowWater']) $(id).addEventListener('change', () => savePreference({
    memory_margin_mb:Number($('setMemoryMargin').value),memory_low_water_mb:Number($('setMemoryLowWater').value)}));
  $('settingsRetryBtn').addEventListener('click', retrySettings);
  $("selectionModeBtn").addEventListener("click", switchSelectionMode);
  for (const btn of $("simpleGoals").querySelectorAll("[data-goal]")) btn.addEventListener("click", () => {
    state.goal = btn.dataset.goal; applyInterface(); refreshPlan();
  });
  $("remuxBtn").addEventListener("click", () => {
    state.mode = "compress";
    document.querySelector('input[name="mode"][value="compress"]').checked = true;
    state.selectionMode = "manual"; state.profileId = "P8"; state.manualSeed = null;
    $("optCrf").value = ""; $("optContainer").value = "";
    $("optMaxEdge").value = 0; $("optScalePct").value = 0;
    for (const id of ["optDeint", "optDenoise", "optAutocrop", "optHdr", "optStrip", "expertMode"]) $(id).checked = false;
    for (const f of state.files) { f.seed = null; f.seedProfile = null; }
    applyMode(); applyInterface(); refreshPlan();
  });
  if ($("setResetLimits")) {
    $("setResetLimits").addEventListener("click", () => {
      if (state.hardware) return savePreference({limits:{gpu:state.hardware.gpu,cpu:state.hardware.cpu}});
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("settingsOverlay")
        && !$("settingsOverlay").classList.contains("hidden")) {
      closeSettings();
    }
  });
}

/* ------------------------------------------------------------- 并行设置 --- */

function renderPerf() {
  const limits = state.limits || { gpu: 1, cpu: 1 };
  if ($("perfSummary")) {
    $("perfSummary").textContent = `GPU ${limits.gpu} · CPU ${limits.cpu}`;
  }
}

/**
 * Uncompressed-equivalent size: W x H x fps x 1.5 bytes (8-bit 4:2:0) x duration.
 *
 * This is the ONLY denominator under which a three-digit "compression ratio" is
 * honest, so it is shown as small print next to the real ratio (which is always
 * "against the input file") and never as the headline number.
 */
function uncompressedBytes(media, duration) {
  const v = media && media.video;
  if (!v || !v.width || !v.height || !v.fps || !duration) return 0;
  return Math.round(v.width * v.height * v.fps * duration * 1.5);
}

function dualBasis(media, duration, outBytes) {
  if (!outBytes) return { main: "", secondary: "" };
  const srcBytes = (media && media.size_bytes) || 0;
  const uncompressed = uncompressedBytes(media, duration || (media && media.duration));
  const main = srcBytes
    ? `压缩到 ${(outBytes / srcBytes * 100).toFixed(0)}%（相对输入文件）` : "";
  const secondary = uncompressed
    ? `≈ ${(uncompressed / outBytes).toFixed(0)}× 于未压缩等价值（仅参考，不是相对原文件的倍率）`
    : "";
  return { main, secondary };
}

function renderEstimate() {
  const box = $("estimateBox");
  // A solved target is a MEASURED result (three real sample encodes plus a sampled
  // VMAF). The plan-based estimate is a guess from a profile coefficient, and it used
  // to overwrite the solved one moments after it appeared -- so the whole target
  // result was effectively invisible. The measured answer wins while it is current.
  const solvedTarget = currentQualityMode() === "target" ? state.targetResult : null;
  if (solvedTarget && solvedTarget.ok) {
    renderTargetEstimate(box, solvedTarget);
    return;
  }
  const est = state.estimate;
  if (!est) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const rows = [];
  const ratio = est.ratio ? `（源的 ${(est.ratio * 100).toFixed(0)}%）` : "";
  rows.push(`<tr><td>预计输出体积</td><td><span class="big-num">${fmtBytes(est.low)} – ${fmtBytes(est.high)}</span> ${ratio}</td></tr>`);
  rows.push(`<tr><td>源文件体积</td><td>${fmtBytes(est.source_bytes)}${est.source_kbps ? ` · ${est.source_kbps} kbps` : ""}</td></tr>`);
  // A1 -- the honest second reading, as small print and clearly marked "reference
  // only". Putting this number in the headline position is exactly how the "100x"
  // claims in the wild are manufactured.
  if (est.secondary_ratio) {
    rows.push(`<tr><td>另一口径（仅参考）</td><td class="hint">${escapeHtml(est.secondary_ratio)}</td></tr>`);
  }
  if (est.avg_kbps) {
    rows.push(`<tr><td>实测样片码率</td><td>${est.samples.join(" / ")} kbps（均值 ${est.avg_kbps}）</td></tr>`);
  }
  if (est.est_seconds) {
    rows.push(`<tr><td>预计耗时</td><td>约 ${fmtDuration(est.est_seconds)}（误差较大）</td></tr>`);
  }
  rows.push(`<tr><td>依据</td><td>${escapeHtml(est.basis)}</td></tr>`);
  // The other end of the trade: what this size will look like. Without it the user
  // is picking a number blind, which is the whole complaint about size-only tools.
  if (est.quality && est.quality.ok) {
    rows.push(`<tr><td>预计画质</td><td><b>VMAF ${est.quality.score}</b>`
      + `　<span class="vmaf-plain">${escapeHtml(est.quality.plain)}</span>`
      + `<div class="vmaf-why">${escapeHtml(est.quality.explanation)}</div>`
      + `<div class="vmaf-basis">基准：${escapeHtml(est.quality.basis)}</div>`
      + `<div class="vmaf-basis">${escapeHtml(est.quality.note || "")}</div></td></tr>`);
  } else if (est.quality && est.quality.error) {
    rows.push(`<tr><td>预计画质</td><td class="hint">未能预估：${escapeHtml(est.quality.error)}</td></tr>`);
  }
  if (est.target_explained) {
    rows.push(`<tr><td>目标解释</td><td>${escapeHtml(est.target_explained)}`
      + `${est.strict ? "　<b>（严格不超过目标）</b>" : ""}</td></tr>`);
  }
  if (est.note) rows.push(`<tr><td>说明</td><td>${escapeHtml(est.note)}</td></tr>`);
  if (est.reachability) {
    rows.push(`<tr><td>可达性</td><td class="${est.reach_blocking ? "space-error" : "hint"}">`
      + `${escapeHtml(est.reachability)}</td></tr>`);
  }
  box.innerHTML = `<h4>📊 体积预估</h4><table>${rows.join("")}</table>`;
}

/** The solved-target panel: size AND the quality it buys, both measured. */
function renderTargetEstimate(box, t) {
  box.classList.remove("hidden");
  const reach = t.reachability || {};
  const rows = [];
  rows.push(`<tr><td>预计输出体积</td><td><span class="big-num">${fmtBytes(t.low)} – ${fmtBytes(t.high)}</span>`
    + `　（目标 ${fmtBytes(t.target_bytes)}，CRF ${t.crf}${t.strict ? "，严格模式" : ""}）</td></tr>`);
  // The other end of the trade: what this size will look like. Without it the user is
  // picking a number blind, which is the whole complaint about size-only tools.
  if (t.quality && t.quality.ok) {
    rows.push(`<tr><td>预计画质</td><td><b>VMAF ${t.quality.score}</b>`
      + `　<span class="vmaf-plain">${escapeHtml(t.quality.plain)}</span>`
      + `<div class="vmaf-why">${escapeHtml(t.quality.explanation)}</div>`
      + `<div class="vmaf-basis">基准：${escapeHtml(t.quality.basis)}</div>`
      + `<div class="vmaf-basis">${escapeHtml(t.quality.note || "")}</div></td></tr>`);
  } else if (t.quality && t.quality.error) {
    rows.push(`<tr><td>预计画质</td><td class="hint">未能预估：${escapeHtml(t.quality.error)}</td></tr>`);
  }
  if (t.target_explained) {
    rows.push(`<tr><td>目标解释</td><td>${escapeHtml(t.target_explained)}`
      + `${t.strict ? "　<b>（严格不超过目标）</b>" : ""}</td></tr>`);
  }
  if (reach.message) {
    rows.push(`<tr><td>可达性</td><td class="${t.reach_blocking ? "space-error" : "hint"}">`
      + `${escapeHtml(reach.message)}</td></tr>`);
  }
  if (t.overhead_bytes) {
    rows.push(`<tr><td>音频/封装占用</td><td class="hint">${fmtBytes(t.overhead_bytes)}`
      + `（其中音轨 ${fmtBytes(t.audio_bytes || 0)}）—— 已从视频预算中扣除</td></tr>`);
  }
  rows.push(`<tr><td>依据</td><td>依据：拟合 ${t.fit.points} 个真实样片 CRF 采样点（斜率 ${t.fit.slope}）</td></tr>`);
  if (t.note) rows.push(`<tr><td>说明</td><td>${escapeHtml(t.note)}</td></tr>`);
  if (t.warnings && t.warnings.length) {
    rows.push(`<tr><td>提醒</td><td class="hint">${escapeHtml(t.warnings.join("；"))}</td></tr>`);
  }
  const basis = dualBasis(
    (state.files.find((f) => f.path === state.activeFile) || {}).media,
    undefined, t.predicted_bytes).secondary;
  if (basis) rows.push(`<tr><td>另一口径（仅参考）</td><td class="hint">${escapeHtml(basis)}</td></tr>`);
  box.innerHTML = `<h4>🎯 目标体积求解</h4><table>${rows.join("")}</table>`;
}

function estimateFromPlan(plan, media) {
  const srcBytes = (media && media.size_bytes) || 0;
  const mid = plan.est_output_bytes || 0;
  if (!mid) return null;
  return {
    low: Math.round(mid * 0.7),
    high: Math.round(mid * 1.4),
    ratio: srcBytes ? mid / srcBytes : 0,
    source_bytes: srcBytes,
    source_kbps: srcBytes && media.duration ? Math.round(srcBytes * 8 / media.duration / 1000) : 0,
    avg_kbps: 0,
    samples: [],
    est_seconds: media && media.duration && media.summary && media.summary.fps ? 0 : 0,
    basis: `档位 ${plan.encoder} 经验系数（未实测，误差可能较大）`,
    note: "点「精确测算体积」可跑真实样片得到更准的数字（压缩模式用样片码率外推）",
  };
}

async function measureCost() {
  const entry = state.files.find((f) => f.path === state.activeFile);
  if (!entry) { toast("先选中一个文件"); return; }
  const btn = $("measureBtn");
  const seq = state.batchSeq;
  state.measuring = true;
  btn.disabled = true;
  btn.textContent = "测算中…";
  toast("正在跑 3 段真实样片，约需 30-60 秒", 8000);
  try {
    if (state.mode === "repair") {
      const data = await api("/api/repair/measure", {
        method: "POST",
        body: JSON.stringify({ path: state.activeFile, seconds: 8, options: repairOptions() }),
      });
      const m = data.measure;
      if (!m.ok) throw new Error(m.error || "测算失败");
      state.estimate = {
        low: m.est_low, high: m.est_high, ratio: m.ratio,
        source_bytes: entry.media.size_bytes, source_kbps: m.source_kbps,
        avg_kbps: m.avg_kbps, samples: m.samples_kbps,
        est_seconds: m.est_full_seconds,
        basis: `修复策略「${state.repairStrategy || "默认"}」实测 ${m.sample_positions.length} 段样片外推`,
        note: m.note,
      };
    } else {
      const data = await api("/api/measure", {
        method: "POST",
        body: JSON.stringify({ path: state.activeFile, ...fileSelection(entry), seconds: 8 }),
      });
      if (seq !== state.batchSeq) { toast("设置已变化，本次测算结果已过期"); return; }
      state.estimate = data.estimate;
    }
    renderEstimate();
  } catch (err) {
    toast("测算失败: " + err.message, 6000);
  } finally {
    state.measuring = false;
    updateStartButton();
    btn.textContent = "精确测算体积";
  }
}

function repairOptions() {
  const location = currentLocation();
  const gop = Number($("optGop").value) || 1;
  const crfRaw = $("optCrf").value.trim();
  const opts = {
    gop_seconds: gop,
    location,
    out_dir: location === "custom" ? $("optOutDir").value.trim() : "",
    default_out_dir: $("optDefaultDir").value.trim(),
  };
  if (crfRaw !== "") opts.crf = Number(crfRaw);
  if ($("optStrip").checked) opts.strip_metadata = true;
  return opts;
}

function renderResolvedLocation(plan) {
  const box = $("locResolved");
  const loc = plan && plan.out_location;
  if (!loc) {
    box.textContent = "";
    return;
  }
  const names = { beside: "源文件旁边", default: "固定默认目录", custom: "本次指定目录" };
  box.innerHTML = `将保存到 <b>${escapeHtml(loc.directory)}</b>`
    + `　（${names[loc.location] || loc.location}）`;
}

/* --------------------------------------------------------------------- plan */

let planTimer = null;
/**
 * The batch panel must never show a number that belongs to the PREVIOUS settings.
 *
 * Reported from real use: "the batch estimate and the budget don't update in time when I
 * change the profile". Two separate causes, both fixed here:
 *
 *   1. The panel only refreshed after a 600 ms debounce and gave no sign that anything
 *      was happening -- so for that window the old figure sat there looking current.
 *   2. The BUDGET table was never recomputed at all on a profile change (it was only
 *      built by the button), so it kept showing allocations computed for the old profile.
 *
 * So every setting change now marks the panel stale IMMEDIATELY (visible), and the
 * recompute runs on a short debounce -- including the budget, when one is already shown.
 */
function markBatchStale(reason) {
  state.batchSeq = (state.batchSeq || 0) + 1;
  state.batchEstimate = null;
  state.batchDisplayMode = null;
  $("batchTotals").innerHTML = '<p class="empty">正在重算本次计划…</p>';
  $("resolvedPlans").textContent = "正在重算…";
  $("planDecisions").classList.add("hidden");
  for (const f of state.files) { f.planText = "正在重算…"; f.planError = ""; }
  renderFiles();
  updateStartButton();
  if ($("batchProgress")) {
    $("batchProgress").textContent = (reason || "档位/参数已变") + "，正在重算…";
  }
  if ($("batchTotals")) $("batchTotals").classList.add("stale");
  if ($("budgetResult") && state.budget) {
    if ($("budgetHint")) $("budgetHint").textContent = "正在按新的档位重算…";
    $("budgetResult").classList.add("stale");
  }
}

function refreshBatch() {
  markBatchStale();
  clearTimeout(state.batchTimer);
  state.batchTimer = setTimeout(async () => {
    if (state.files.some(f => !f.media || f.probing)) {
      $('batchTotals').innerHTML = '<p class="empty">等待文件信息；资源不足时会安全重试，也可点击文件重新探测。</p>';
      return;
    }
    await loadBatchEstimate();
    await loadGroups();
    // Only re-run the allocation when a table is actually on screen: silently computing
    // it for a user who never opened that part would be wasted work.
    const shown = $("budgetResult") && $("budgetResult").querySelector(".budget-table");
    if (shown && state.budget) await applyBudget({ quiet: true });
  }, 150);
}

function refreshPlan() {
  ++planSeq;
  state.plan = null; state.estimate = null;
  renderEstimate();
  clearTimeout(planTimer);
  planTimer = setTimeout(doRefreshPlan, 180);
  refreshSpaceNote();
  // The batch panel answers questions about the WHOLE selection, so it has to follow the
  // selection: a stale total after adding five files is worse than no total.
  //
  // Grouping is different: it is re-FETCHED only when the file set actually changed
  // (`loadGroups` compares a signature), and the rows are re-derived locally the rest of
  // the time. Re-fetching on every keystroke is what used to wipe a group's chosen
  // profile a moment after the user picked it.
  syncGroupRows();
  refreshBatch();
}

function applyMode() {
  const repair = state.mode === "repair";
  $("sourcePolicyRow").classList.toggle("hidden", repair);
  if (repair) $("recycleSources").checked = false;
  $("startBtn").textContent = repair ? "添加到修复队列" : "添加到压缩队列";
  $("previewBtn").classList.toggle("hidden", repair);
  $("measureBtn").classList.toggle("hidden", !repair && !state.caps);
  $("measureBtn").classList.remove("hidden");
  $("profileList").classList.toggle("dimmed", repair);
  $("modeHint").textContent = repair
    ? "修复模式：针对已经压过、但拖动进度条卡死的文件，重编码并强制插入关键帧"
    : "";
  const broken = state.files.filter((f) => f.seek && f.seek.verdict !== "seekable").length;
  if (repair && broken) $("modeHint").textContent += `（已检出 ${broken} 个需要修复）`;
  renderEstimate();
  applyInterface();
}

// Monotonic generation counter for plan refreshes. `doRefreshPlan` is debounced but
// still racy on its own: typing fast (or switching file/profile) issues several
// requests, and the SLOWEST one may answer last. Without this guard the command box
// ends up showing the plan for an earlier selection -- and it would be submitted as
// such, which is why this is not merely cosmetic.
let planSeq = 0;

async function doRefreshPlan() {
  const seq = ++planSeq;
  const stale = () => seq !== planSeq;
  if (!state.activeFile) {
    $("cmdText").textContent = "—";
    return;
  }
  const entry = state.files.find((f) => f.path === state.activeFile);
  if (!entry || !entry.media) {
    $("cmdText").textContent = "该文件无法探测，不能生成命令";
    return;
  }

  if (state.mode === "repair") {
    try {
      const data = await api("/api/repair/plan", {
        method: "POST",
        body: JSON.stringify({ path: state.activeFile, options: repairOptions() }),
      });
      if (stale()) return;
      state.plan = data.plan;
      state.repairStrategy = data.plan.strategy.id;
      $("cmdText").textContent = data.plan.command;
      $("cmdRules").textContent = (data.plan.rules_applied || []).join(" · ");
      renderResolvedLocation(data.plan);
      const d = data.diagnosis;
      $("planHint").textContent =
        `诊断：${d.summary}（关键帧 ${d.keyframes} 个，最大间隔 ${d.max_keyframe_gap}s）`
        + (data.plan.warnings.length ? "  ⚠ " + data.plan.warnings[0] : "");
      if (data.plan.estimate) {
        state.estimate = {
          low: data.plan.estimate.low,
          high: data.plan.estimate.high,
          mid: data.plan.estimate.mid,
          ratio: d.size_bytes ? data.plan.estimate.mid / d.size_bytes : 0,
          source_bytes: d.size_bytes,
          source_kbps: d.duration ? Math.round(d.size_bytes * 8 / d.duration / 1000) : 0,
          avg_kbps: 0, samples: [], est_seconds: 0,
          basis: "按经验系数估算（关键帧变多的代价）",
          note: "点「精确测算体积」跑真实样片可得到 ±20% 的准确数字",
        };
      }
      renderEstimate();
      return;
    } catch (err) {
      if (stale()) return;
      $("cmdText").textContent = "生成修复命令失败: " + err.message;
      return;
    }
  }

  try {
    const data = await api("/api/plan", {
      method: "POST",
      body: JSON.stringify({
        path: state.activeFile,
        ...fileSelection(entry),
      }),
    });
    if (stale()) return;   // a newer keystroke already asked for its own plan
    state.plan = data.plan;
    $("cmdText").textContent = data.plan.command;
    $("cmdRules").textContent = (data.plan.rules_applied || []).join(" · ");
    renderResolvedLocation(data.plan);
    const est = data.plan.est_output_bytes;
    $("planHint").textContent = est
      ? `当前文件 ${entry.name}：预计输出约 ${fmtBytes(est)}（估算）→ ${data.plan.out_path}`
      : `输出: ${data.plan.out_path}`;
    if (data.plan.warnings && data.plan.warnings.length) {
      $("planHint").textContent += "  ⚠ " + data.plan.warnings[0];
    }
    state.estimate = estimateFromPlan(data.plan, entry.media);
    renderEstimate();
  } catch (err) {
    if (stale()) return;
    $("cmdText").textContent = "生成命令失败: " + err.message;
    $("planHint").textContent = "待决定：" + err.message;
  }
}

/* --------------------------------------------------------------------- jobs */

// Monotonic generation counter for list refreshes.
//
// The queue is refreshed from two independent sources: the SSE stream (per job
// event) and explicit calls after an action. Both issue GET /api/jobs, and the
// older request can resolve LAST -- re-rendering a list that was read before the
// action. That is exactly how "已清除 1 条记录" ended up leaving the job on
// screen: the pre-clear list won the race. Only the newest response may render.
let jobsFetchSeq = 0;

async function refreshJobs() {
  const seq = ++jobsFetchSeq;
  try {
    const data = await api("/api/jobs");
    if (seq !== jobsFetchSeq) return;   // a newer response already landed
    state.stats = data.stats;
    state.jobs = data.jobs;
    state.restore = data.restore;
    applyAfterPolicy(data.on_success);
    renderPowerBanner(data.power);
    renderJobs(data.jobs);
  } catch (err) {
    /* transient */
  }
}

/** Reflect the server's completion policy in the panel (the server is the source of
 *  truth: it survives restarts, and a stale page must not disagree with it). */
function applyAfterPolicy(policy) {
  if (!policy || !$("afterNotify")) return;
  $("afterNotify").checked = !!policy.notify;
  $("afterPower").checked = policy.power && policy.power !== "none";
  if ($("afterDelay")) $("afterDelay").value = String(policy.power_delay || 60);
  if ($("afterOnlyOk")) $("afterOnlyOk").checked = policy.power_only_if_all_ok !== false;
  syncAfterRows();
}

/** Apply a known-good list immediately (used by actions that change the queue).
 *
 * Clearing must not wait for a network round-trip: the server already told us
 * what happened, so invalidate any in-flight refresh and render the truth now. */
function applyJobsNow(jobs, stats) {
  jobsFetchSeq += 1;
  if (stats) state.stats = stats;
  state.jobs = jobs || [];
  renderJobs(state.jobs);
}

const STATUS_TEXT = {
  queued: "排队中",
  running: "压缩中",
  paused: "已暂停",
  verifying: "校验中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

const VERIFY_STAGE_TEXT = {
  probe: "读取输出信息",
  scan: "整片读取",
  decode: "整片解码",
  subtitle_decode: "图像字幕解码",
  keyframes: "分析关键帧",
};

function verifyLabel(job) {
  if (job.status !== "verifying") return "";
  const stage = VERIFY_STAGE_TEXT[job.verify_stage || ""] || "校验中";
  const percent = job.verify_percent;
  if (typeof percent === "number" && isFinite(percent)) {
    return `${stage} ${percent.toFixed(0)}%`;
  }
  if (job.verify_frames) {
    const unit = job.verify_stage === "subtitle_decode" ? "个事件" : "帧";
    return `${stage}（已处理 ${job.verify_frames} ${unit}）`;
  }
  return stage;
}

function statusClass(status) {
  if (status === "done") return "ok";
  if (status === "failed") return "err";
  if (status === "running" || status === "verifying") return "run";
  return "";
}

/**
 * Job cards, keyed by id, so a progress update PATCHES the card instead of rebuilding it.
 *
 * This is not a performance tweak -- rebuilding the list broke the UI in two user-visible
 * ways, both reported from real use:
 *
 *   1. **Buttons flickered under the pointer.** `box.innerHTML = ""` destroyed and
 *      recreated every card, and the SSE stream pushes progress ~4x a second, so the
 *      `:hover` state was lost continuously. The list looked like it was buzzing.
 *   2. **Clicks were silently swallowed.** A click only fires when mousedown and mouseup
 *      land on the SAME element. The button the user pressed was replaced mid-click, so
 *      `对比源片` / `预览播放` did nothing -- the most confusing possible failure, because
 *      the button visibly reacted to the press.
 *
 * So: the skeleton is created once, text/width/class are updated in place, and the
 * INTERACTIVE parts (buttons, drag handlers) are rebuilt only when the thing that decides
 * them -- the status -- actually changes.
 */
const jobCards = new Map();

/** What the buttons and drag handlers depend on. Changes rarely; progress does not.
 *
 *  Warnings and errors are deliberately NOT part of it: they render into `.job-extra`,
 *  a separate node, so including them would rebuild the button row every time a warning
 *  was appended -- reintroducing the lost-click bug on the very cards that warn most. */
function jobActionSig(job) {
  return [job.status, job.mode || "", job.out_path ? 1 : 0, !!state.settings.show_advanced, !!state.settings.show_extreme, job.source_cleanup || '', job.source_cleanup_note || ''].join("|");
}

function renderJobs(jobs) {
  const box = $("jobList");
  // The batch-level stop only makes sense when there is something to stop. A PAUSED job
  // counts: its encoder is alive and still holds its slot, so 停止全部 must reach it too.
  const active = jobs.filter((j) => ["running", "verifying", "queued", "paused"].includes(j.status));
  const paused = jobs.filter((j) => j.status === "paused");
  const runningish = jobs.filter((j) => j.status === "running" || j.status === "verifying");
  if ($("stopAllBtn")) {
    const show = active.length > 0;
    $("stopAllBtn").classList.toggle("hidden", !show);
    if (show) {
      const running = active.filter((j) => j.status !== "queued").length;
      $("stopAllBtn").textContent = running
        ? `⏹ 停止全部（进行中 ${running}）` : `⏹ 停止全部（排队 ${active.length}）`;
    }
  }
  // A RESTORED queue no longer starts by itself (opening the app used to begin encoding
  // immediately, which lagged the whole machine before the user had clicked anything).
  // Say what was restored, and give one button to let it run.
  if ($("restoreBanner")) {
    const restore = (state.stats && state.stats.restore) || state.restore || null;
    const held = !!(restore && restore.held && restore.queued > 0);
    $("restoreBanner").classList.toggle("hidden", !held);
    if (held) {
      $("restoreBanner").innerHTML =
        `已恢复上次排队中的 <b>${restore.queued}</b> 个任务，<b>不会自动开始</b>`
        + `（避免一打开软件就占满内存与 CPU）。`
        + ` <button id="restoreReleaseBtn" class="primary">▶ 开始这些任务</button>`
        + ` <button id="restoreDropBtn" class="ghost">移除这些任务</button>`;
      const release = $("restoreReleaseBtn");
      if (release) {
        release.addEventListener("click", async () => {
          try {
            const data = await api("/api/jobs/release", { method: "POST", body: "{}" });
            toast(`已放开 ${data.released} 个任务，按顺序开始执行`, 6000);
            refreshJobs();
          } catch (err) { toast("开始失败: " + err.message, 6000); }
        });
      }
      const drop = $("restoreDropBtn");
      if (drop) {
        drop.addEventListener("click", async () => {
          try {
            const data = await api("/api/jobs/discard-restored", {
              method: "POST", body: JSON.stringify({ confirm: true }),
            });
            toast(`已移除 ${data.removed} 个恢复任务，视频文件保留`, 6000);
            refreshJobs();
          } catch (err) { toast("清空失败: " + err.message, 6000); }
        });
      }
    }
  }
  // Batch pause / resume (user request): per-job 暂停 existed, but "I paused everything,
  // now how do I start it again?" had no answer. The two buttons are mutually exclusive
  // and each says how many jobs it will touch.
  if ($("pauseAllBtn")) {
    $("pauseAllBtn").classList.toggle("hidden", runningish.length === 0);
    $("pauseAllBtn").textContent = `⏸ 全部暂停（${runningish.length}）`;
  }
  if ($("resumeAllBtn")) {
    $("resumeAllBtn").classList.toggle("hidden", paused.length === 0);
    $("resumeAllBtn").textContent = `▶ 全部继续（${paused.length}）`;
  }
  if (!jobs.length) {
    // Drop the tracked cards AND their nodes: leaving the nodes behind while clearing the
    // map is what let a later render update an orphaned element.
    for (const entry of jobCards.values()) entry.el.remove();
    jobCards.clear();
    box.innerHTML = `<p class="empty">队列为空</p>`;
    $("queueStats").textContent = "0";
    return;
  }
  const running = jobs.filter((j) => j.status === "running" || j.status === "verifying").length;
  // Show which pools are occupied so parallel behaviour is visible, not implied.
  const busy = (state.stats && state.stats.busy) || null;
  const limits = (state.stats && state.stats.limits) || state.limits || null;
  let poolText = "";
  if (busy && limits) {
    poolText = ` · GPU ${busy.gpu || 0}/${limits.gpu || 1} · CPU ${busy.cpu || 0}/${limits.cpu || 1}`;
  }
  $("queueStats").textContent = `${jobs.length}（进行中 ${running}${poolText}）`;

  const wanted = jobs.slice().reverse();
  const seen = new Set();

  for (const job of wanted) {
    seen.add(job.id);
    const entry = jobCards.get(job.id) || createJobCard(job);
    updateJobCard(entry, job);
  }
  // Drop cards for jobs that are gone (removed / cleared).
  for (const [id, entry] of [...jobCards.entries()]) {
    if (!seen.has(id)) {
      entry.el.remove();
      jobCards.delete(id);
    }
  }
  // Make sure the "队列为空" placeholder from a previous render is gone.
  const placeholder = box.querySelector(":scope > p.empty");
  if (placeholder) placeholder.remove();

  // Re-order ONLY when the order actually differs, OR when a card is not in the list yet
  // (a freshly created card is deliberately not appended by `createJobCard`, which cannot
  // know its position). Re-appending identical nodes every tick would re-insert the
  // element under the cursor and reintroduce the very problems this function now avoids;
  // NOT appending a new card at all is the bug the end-to-end probe caught -- the DOM
  // kept showing the previous element for that id while updates went to an orphan.
  const current = [...box.children].map((el) => el.dataset.jobId).filter(Boolean);
  const desired = wanted.map((j) => j.id);
  const missing = wanted.some((j) => {
    const entry = jobCards.get(j.id);
    return !entry || entry.el.parentNode !== box;
  });
  if (missing || current.length !== desired.length
      || current.some((id, i) => id !== desired[i])) {
    for (const job of wanted) {
      const entry = jobCards.get(job.id);
      if (entry) box.appendChild(entry.el);
    }
  }
}

/** Build the (non-interactive) structure of one job card, once per job. */
function createJobCard(job) {
  const el = document.createElement("div");
  el.className = `job ${job.status}`;
  el.dataset.jobId = job.id;
  el.innerHTML = `
    <div class="job-top">
      <div class="job-name"></div>
      <div class="job-status"></div>
    </div>
    <div class="bar"><i></i></div>
    <div class="job-meta"></div>
    <div class="job-extra"></div>
    <div class="job-actions"></div>`;
  const entry = {
    el,
    name: el.querySelector(".job-name"),
    status: el.querySelector(".job-status"),
    bar: el.querySelector(".bar i"),
    meta: el.querySelector(".job-meta"),
    extra: el.querySelector(".job-extra"),
    actions: el.querySelector(".job-actions"),
    sig: "",
    pct: null,
    text: "",
  };
  jobCards.set(job.id, entry);
  return entry;
}

/** Update the changing parts of a card in place. */
function updateJobCard(entry, job) {
  const el = entry.el;
  // ONE place decides the class list. Assigning `className` here used to wipe the
  // `draggable` class that `buildJobActions` had added (the queued row silently stopped
  // being draggable while `el.draggable` stayed true), so the state is now derived
  // rather than toggled from two directions.
  const cls = `job ${job.status}`
    + (job.status === "queued" ? " draggable" : "")
    + (job.mode === "repair" ? " repair" : "");
  if (el.className !== cls) el.className = cls;

  const pct = Math.max(0, Math.min(100, job.progress || 0));
  const showPct = job.status === "running" || job.status === "verifying";
  const statusText = `${STATUS_TEXT[job.status] || job.status}${showPct ? " " + pct.toFixed(0) + "%" : ""}`;
  if (entry.text !== statusText) {
    entry.text = statusText;
    entry.status.textContent = statusText;
  }
  const statusCls = `job-status ${statusClass(job.status)}`;
  if (entry.status.className !== statusCls) entry.status.className = statusCls;
  if (entry.pct !== pct) {
    entry.pct = pct;
    entry.bar.style.width = pct + "%";
  }

  const nameHtml = (job.mode === "repair" ? `<span class="tag" title="修复模式">修复</span> ` : "")
    + escapeHtml(job.src_name || job.src || "");
  if (entry.nameHtml !== nameHtml) {
    entry.nameHtml = nameHtml;
    entry.name.innerHTML = nameHtml;
  }

  const parts = [];
  if (job.status === "verifying") {
    // Verification decodes the whole output; its own progress is what the user
    // needs to see, not the finished encode's 100%.
    const vlabel = verifyLabel(job);
    if (vlabel) parts.push(vlabel);
    if (job.verify_frames) parts.push(`已解码 ${job.verify_frames} 帧`);
  } else {
    if (job.duration) parts.push(`${fmtClock(job.out_time)} / ${fmtClock(job.duration)}`);
    if (job.fps) parts.push(`${job.fps.toFixed(1)} fps`);
    if (job.speed) parts.push(`${job.speed.toFixed(2)}×`);
    if (job.total_size) parts.push(fmtBytes(job.total_size));
    if (job.src_size) parts.push(`源 ${fmtBytes(job.src_size)}`);
  }
  if (job.elapsed) parts.push(`用时 ${fmtDuration(job.elapsed)}`);
  if (job.status === "queued" && job.waiting_reason) {
    // A queued job that is not starting needs its reason ON the card, or the queue looks
    // stuck. The memory guard supplies it ("等待内存：需要 … 可用 …").
    parts.push(`⏳ ${job.waiting_reason}`);
  }
  if (job.eta_seconds && job.status === "running") {
    // "How much longer?" is the question every user asks, and we can answer it from
    // numbers we already have (see jobs._eta -- written and measured, not guessed).
    parts.push(`剩余约 ${fmtDuration(job.eta_seconds)}`);
  }
  if (job.status === "paused" && job.paused_seconds) {
    parts.push(`已暂停 ${fmtDuration(job.paused_seconds)}`);
  }
  if (job.status === "done" && job.src_size && job.total_size) {
    const benefit = sizeBenefit(job.src_size, job.total_size);
    if (benefit) parts.push(`实测倍率 ${benefit.multiple} · ${benefit.change}`);
  } else if (job.status === "queued" && job.est_output_bytes) {
    const benefit = sizeBenefit(job.src_size, Math.floor(job.est_output_bytes * 0.7), Math.floor(job.est_output_bytes * 1.4));
    if (benefit) parts.push(`预计 ${fmtBytes(job.src_size)} → ${fmtBytes(job.est_output_bytes)} · 预计倍率 ${benefit.multiple}`);
  }
  if (job.status === "done" && job.speed && job.duration) {
    parts.push(`平均 ${(job.duration / Math.max(0.1, job.elapsed)).toFixed(2)}×`);
  }
  const metaHtml = parts.map((p) => `<span>${p}</span>`).join("");
  if (entry.metaHtml !== metaHtml) {
    entry.metaHtml = metaHtml;
    entry.meta.innerHTML = metaHtml;
  }

  const sig = jobActionSig(job);
  if (entry.sig !== sig) {
    entry.sig = sig;
    // Everything below is rebuilt only here: the extra block can contain a verify list
    // that only appears at the end, and the buttons depend on the status. Doing it on
    // every tick is what made the buttons unclickable.
    let verifyHtml = "";
    if (job.verify && job.verify.checks) {
      const items = job.verify.checks
        .filter((c) => c.status !== "skipped")
        .map((c) => `<li class="${escapeHtml(c.status)}"><b>${escapeHtml(c.label)}</b>：${escapeHtml(c.detail)}</li>`)
        .join("");
      if (items) verifyHtml = `<ul class="verify-list">${items}</ul>`;
    }
    const q = job.verify && job.verify.vmaf;
    if (q && q.ok) {
      verifyHtml += `<div class="vmaf-box">`
        + `<span class="vmaf-score">VMAF ${q.score}</span>`
        + `<span class="vmaf-plain">${escapeHtml(q.plain)}</span>`
        + `<span class="vmaf-why">${escapeHtml(q.explanation)}</span>`
        + `<span class="vmaf-basis">基准：${escapeHtml(q.basis)}</span>`
        + `</div>`;
    }
    const warnHtml = (job.warnings && job.warnings.length)
      ? `<div class="warnline">⚠ ${job.warnings.map(escapeHtml).join("；")}</div>` : "";
    const errHtml = job.error
      ? `<div class="job-error">${escapeHtml(job.error)}</div>` : "";
    const seekHtml = job.seek
      ? `<div class="warnline">诊断：${escapeHtml(job.seek.summary)}`
        + `（原有 ${job.seek.keyframes} 个关键帧，最大间隔 ${job.seek.max_keyframe_gap}s）</div>`
      : "";
    let basisHtml = "";
    if (job.status === "done" && job.total_size) {
      const b = dualBasis(job.media, job.duration, job.total_size);
      if (b.secondary) basisHtml = `<div class="basis-note">${escapeHtml(b.secondary)}</div>`;
    }
    const cleanupHtml = job.source_cleanup_note && !(job.warnings || []).includes(job.source_cleanup_note)
      ? `<div class="source-cleanup-note">${escapeHtml(job.source_cleanup_note)}</div>` : "";
    entry.extra.innerHTML = `${basisHtml}${seekHtml}${cleanupHtml}${warnHtml}${verifyHtml}${errHtml}`;
    buildJobActions(entry, job);
  }
}

/** (Re)build the button row + drag handlers for one card. Called on status change only. */
function buildJobActions(entry, job) {
  const el = entry.el;
  const actions = entry.actions;
  actions.innerHTML = "";

  const mkBtn = (label, cls, handler) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = cls;
    b.addEventListener("click", handler);
    actions.appendChild(b);
    return b;
  };

  if (job.status === "running" || job.status === "queued" || job.status === "verifying"
      || job.status === "paused") {
    // "停止此项" not "取消": it is the narrow half of stop-current vs stop-all, and
    // the pairing only reads correctly if both are named for their scope.
    mkBtn("停止此项", "ghost", () => act(`/api/jobs/${job.id}/cancel`));
  }
  if (job.status === "running") {
    // Pause means the encoder process is suspended: the output file stays incomplete
    // and keeps its slot, which is why the button says 暂停（挂起）rather than 暂停.
    mkBtn("⏸ 暂停", "ghost", () => act(`/api/jobs/${job.id}/pause`));
  } else if (job.status === "paused") {
    mkBtn("▶ 继续", "primary", () => act(`/api/jobs/${job.id}/resume`));
  }
  if (job.status === "queued" && (state.settings.show_advanced || state.settings.show_extreme)) {
    mkBtn("调整参数", "ghost", () => openJobEditor(job));
  }
  // F8: every job gets a log entry point, whatever its status -- the interesting
  // failures are exactly the ones that are no longer "running".
  mkBtn("日志", "ghost", () => openJobLog(job));
  if (["failed", "cancelled", "interrupted", "done"].includes(job.status)) {
    if (!['processing', 'review'].includes(job.source_cleanup)) mkBtn("重试", "ghost", () => act(`/api/jobs/${job.id}/retry`));
    if (!['pending', 'processing', 'review'].includes(job.source_cleanup)) mkBtn("移除", "ghost", () => act(`/api/jobs/${job.id}`, "DELETE"));
  }
  if (job.status === "done" && job.out_path) {
    mkBtn("预览播放", "ghost", () => openPreview(job.out_path, job.verify));
    mkBtn("对比源片", "ghost", () => openCompare(job.src, job.out_path, job));
    mkBtn("打开目录", "ghost", () => {
      const dir = job.out_path.replace(/[\\/][^\\/]*$/, "");
      api("/api/browse", { method: "POST", body: JSON.stringify({ path: dir }) })
        .then(() => toast("输出目录：" + dir))
        .catch(() => toast("输出目录：" + dir));
    });
  }
  mkBtn("命令", "ghost", () => {
    $("cmdText").textContent = job.command;
    $("cmdPanel").open = true;
    $("cmdPanel").scrollIntoView({ behavior: "smooth" });
  });

  // Drag-to-reorder is wired here too, because it depends on the same thing (status):
  // only queued jobs are draggable -- a running job's position is a fact.
  if (job.status === "queued") {
    el.draggable = true;
    el.classList.add("draggable");
    if (!el.dataset.dragWired) {
      el.dataset.dragWired = "1";
      el.addEventListener("dragstart", (ev) => {
        ev.dataTransfer.setData("text/plain", job.id);
        ev.dataTransfer.effectAllowed = "move";
        el.classList.add("dragging");
      });
      el.addEventListener("dragend", () => {
        el.classList.remove("dragging");
        document.querySelectorAll(".job.drop-target").forEach((n) => n.classList.remove("drop-target"));
      });
      el.addEventListener("dragover", (ev) => {
        if (!ev.dataTransfer.types.includes("text/plain")) return;
        ev.preventDefault();
        el.classList.add("drop-target");
      });
      el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
      el.addEventListener("drop", async (ev) => {
        ev.preventDefault();
        el.classList.remove("drop-target");
        const moved = ev.dataTransfer.getData("text/plain");
        if (!moved || moved === job.id) return;
        try {
          await api(`/api/jobs/${moved}/move`, {
            method: "POST", body: JSON.stringify({ before: job.id }),
          });
          refreshJobs();
        } catch (err) { toast("调整顺序失败: " + err.message, 6000); }
      });
    }
  } else if (el.dataset.dragWired) {
    delete el.dataset.dragWired;
    el.draggable = false;
    el.classList.remove("draggable");
  }
}

async function act(url, method = "POST") {
  try {
    await api(url, { method });
    refreshJobs();
  } catch (err) {
    toast("操作失败: " + err.message);
  }
}

/* --------------------------------------------------------------- SSE stream */

function connectEvents() {
  if (!window.EventSource) return;  // the existing polling fallback still updates jobs
  const source = new EventSource("/api/events");
  source.onmessage = (evt) => {
    let data;
    try { data = JSON.parse(evt.data); } catch { return; }
    if (data.event === "snapshot") {
      // A snapshot arrives only when the stream (re)connects, and it is a full list
      // built from the server's CURRENT state. It must still invalidate any list read
      // already in flight: that read may have been issued before a clear/remove, so
      // letting it land afterwards put the cleared rows back on screen (and they stayed
      // until the next refresh). Bumping the sequence is what makes "latest wins" true
      // in both directions.
      jobsFetchSeq += 1;
      state.stats = data.stats;
      state.jobs = data.jobs || [];
      state.restore = data.restore;
      renderJobs(state.jobs);
      return;
    }
    if (data.event === "seek") {
      // Keyframe scan finished for one file; merge it in.
      const entry = state.files.find((f) => f.path === data.path);
      if (entry) {
        entry.seek = data.seek;
        renderFiles();
        maybeWarnBroken();
      }
      return;
    }
    if (data.event === "picked") {
      resolvePickEvent(data.token, data.payload);
      return;
    }
    if (data.event === "inbox") {
      // Files were copied into the inbox folder: add them automatically.
      const paths = data.paths || [];
      const n = addPaths(paths);
      if (n) toast(`收件箱发现 ${n} 个新文件，已自动加入`, 5000);
      return;
    }
    if (data.job) {
      mergeJob(data.job);
      // A job that has just reached a terminal state has only now written its
      // report.json. Refresh the archive when the history panel is open, so the
      // entry the user just created is actually there when they look for it.
      if (data.event === "finished" && $("historyPanel") && $("historyPanel").open) {
        loadHistory();
      }
    }
    if (data.stats) state.stats = data.stats;
  };
  source.onerror = () => {
    /* the browser retries automatically */
  };
}

// Job events from the SSE stream already carry the authoritative job payload, so
// merge them into the local list instead of firing another GET /api/jobs. Those
// extra GETs were what raced the post-clear refresh and resurrected deleted rows.
function mergeJob(job) {
  if (!job || !job.id) return;
  jobsFetchSeq += 1;              // invalidate any list read already in flight
  const list = Array.isArray(state.jobs) ? state.jobs.slice() : [];
  const index = list.findIndex((j) => j.id === job.id);
  if (index >= 0) list[index] = job;
  else list.push(job);
  state.jobs = list;
  renderJobs(list);
}

/* -------------------------------- 批量预估 / 分组 / 总预算 (A1/C3/C1) ---
 *
 * Three questions the single-file estimate cannot answer, all sharing ONE server-side
 * implementation (`core/batch.py`) so the batch total and the per-file card agree:
 *
 *   A1  整批预计多大、要跑多久          — 经验系数默认，一键换真实样片测算
 *   C3  哪些文件可以用同一档位          — 自动分组 + 每组一个下拉框
 *   C1  整批不超过 N GB 时怎么摊        — 按「时长 × 像素」加权，并指出谁被压得最狠
 */

function batchPaths() {
  return state.files.map((f) => f.path);
}

/** Per-file / per-group settings, keyed by path -- the batch panel must account for them.
 *
 *  Without this the panel answered "how big will this batch be" using the MAIN profile
 *  only, so after grouping files onto different profiles (a feature this panel drives) the
 *  totals and the budget table described a batch that would never be encoded. Measured:
 *  changing a group's profile left the budget table byte-identical. */
function perFileOverrides() {
  const out = {};
  for (const f of state.files) {
    const own = { ...(state.selectionMode === "manual" ? f.seed : {}), ...(f.override || {}) };
    if (state.selectionMode === "manual" && f.seedProfile && !own.profile) own.profile = f.seedProfile;
    if (!own) continue;
    const entry = { ...own };
    delete entry.loudness_measured;
    for (const key of ["location", "out_dir", "default_out_dir", "name_template", "keep_timestamps"]) delete entry[key];
    if (Object.keys(entry).length) out[f.path] = entry;
  }
  return out;
}

function batchBody(extra) {
  return Object.assign({
    paths: batchPaths(),
    profile: state.profileId,
    overrides: overrides(),
    per_file: perFileOverrides(),
  }, extra || {});
}

function budgetBytes() {
  const value = Number(($("budgetValue") || {}).value) || 0;
  const unit = ($("budgetUnit") || {}).value || "gb";
  const factor = unit === "gb" ? 1024 * 1024 * 1024 : 1024 * 1024;
  return Math.round(value * factor);
}

async function loadBatchEstimate({ measure = false, quiet = false } = {}) {
  const seq = state.batchSeq;
  const paths = batchPaths();
  if (!paths.length) {
    if ($("batchTotals")) $("batchTotals").innerHTML =
      '<p class="empty">还没有文件。加入文件后这里会给出整批的预计体积与用时。</p>';
    if ($("batchSummary")) $("batchSummary").textContent = "";
    renderGroups([]);
    if ($("budgetResult")) $("budgetResult").innerHTML = "";
    return;
  }
  if (!quiet && $("batchProgress")) $("batchProgress").textContent = "";
  const controller = quiet ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), MEMORY_TIMEOUT_MS) : null;
  try {
    const data = await api("/api/estimate/batch", {
      method: "POST",
      body: JSON.stringify(batchBody()),
      ...(controller ? { signal: controller.signal } : {}),
    });
    if (seq !== state.batchSeq) return;
    if (quiet && state.batchDisplayMode === 'measured') {
      renderPlanDecisions(data); // Keep completed real sample estimates intact.
      return;
    }
    state.batchEstimate = data;
    renderBatchTotals(data, measure ? "measured" : "coefficient", { preserveCards: quiet });
  } catch (err) {
    if (seq !== state.batchSeq || quiet) return;
    if ($("batchTotals")) {
      $("batchTotals").classList.remove("stale");
      $("batchTotals").innerHTML =
        `<p class="job-error">整批预估失败：${escapeHtml(err.message)}</p>`;
    }
  } finally { if (timer !== null) clearTimeout(timer); }
}

function renderPlanDecisions(data) {
  const blocked = data.blocked || [];
  const warnings = [...new Set((data.rows || []).flatMap((r) => r.warnings || []))];
  $('planDecisions').classList.toggle('hidden', !blocked.length && !warnings.length);
  $('planDecisions').textContent = [...blocked.map(r => `${baseName(r.path)}：${r.error}`), ...warnings].join('；');
}

function renderBatchTotals(data, mode, { preserveCards = false } = {}) {
  state.batchDisplayMode = mode;
  const t = data.totals || {};
  const measured = mode === "measured";
  const rows = [
    ["文件数", `${data.files} 个`],
    ["源总体积", fmtBytes(t.src_bytes || 0)],
    [`预计输出总体积`, measured
      ? `<span class="big-num">${fmtBytes(t.est_bytes || 0)}</span>`
      : `<span class="big-num">${fmtBytes(t.est_low || 0)} – ${fmtBytes(t.est_high || 0)}</span>`
        + ` <span class="hint">（中值 ${fmtBytes(t.est_bytes || 0)}）</span>`],
    ["输入总时长", batchDurationText()],
    ["预计总耗时", t.est_seconds
      ? `约 ${fmtDuration(t.est_seconds)}${measured ? "（按样片实测外推）" : ""}`
      : "—"],
  ];
  if ($("batchTotals")) {
    const low = measured ? (t.est_low || t.est_bytes) : t.est_low, high = measured ? (t.est_high || t.est_bytes) : t.est_high;
    const benefit = data.complete === false ? null : sizeBenefit(t.src_bytes, low, high);
    $("batchTotals").innerHTML = benefit
      ? `<div class="benefit-flow"><div><span class="hint">输入文件</span><strong>${fmtBytes(t.src_bytes)}</strong></div><span class="benefit-arrow">→</span><div class="output"><span class="hint">${measured ? "样片测算外推" : "预计成品"}</span><strong>${fmtBytes(low)}${low === high ? "" : " – " + fmtBytes(high)}</strong></div></div><div class="benefit-metrics"><span>预计倍率 <b>${benefit.multiple}</b></span><span>${benefit.change}</span></div><p class="hint">${rows[0][1]} · 时长 ${rows[3][1]} · 预计用时 ${rows[4][1]}</p>`
      : `<p class="plan-decisions">${data.partial_measure ? "仅测算了部分文件，不展示整批实测外推收益；其余仍为经验估算。" : data.complete === false ? "存在待决定文件，尚无完整批次预计收益。请处理或从清单移除这些文件。" : "缺少可靠的体积数据，预计收益未知。"}</p>`;
  }
  const blocked = data.blocked || [];
  renderPlanDecisions(data);
  $("resolvedPlans").innerHTML = (data.rows || []).map((r) => {
    const benefit = sizeBenefit(r.src_bytes, r.est_low, r.est_high);
    const f = state.files.find((x) => x.path === r.path);
    const text = `${encoderLabel(r.encoder)} · ${(r.container || "").toUpperCase()} · 预计 ${fmtBytes(r.est_low)}–${fmtBytes(r.est_high)}${benefit ? " · " + benefit.multiple : ""}`;
    if (f) { f.planText = text; f.planError = ""; }
    return `<div class="resolved-plan"><b>${escapeHtml(r.name)}</b>${escapeHtml(text)}<div class="hint">${escapeHtml((r.decision || []).join("；"))}</div></div>`;
  }).join("");
  for (const r of blocked) {
    const f = state.files.find((x) => x.path === r.path);
    if (f) { f.planError = r.error; f.planText = ""; }
  }
  if (preserveCards) {
    for (const card of $('fileList').querySelectorAll('.file-card')) {
      const f = state.files.find(x => x.path === card.dataset.path);
      if (!f) continue;
      const plan = card.querySelector('.file-plan');
      plan.textContent = f.planError || f.planText || '等待计划解析';
      plan.classList.toggle('blocked', !!f.planError);
    }
  } else renderFiles();
  updateStartButton();
  if ($("batchSummary")) {
    $("batchSummary").classList.remove("stale");
    // If some files carry their own profile, the main profile cannot move their numbers --
    // say so, or an accurate panel looks broken.
    const own = Object.keys(perFileOverrides()).length;
    const used = (data.profiles_used || []).length > 1
      ? ` · 实际使用 ${(data.profiles_used || []).join("/")}` : "";
    $("batchSummary").textContent =
      `${data.files} 个 · 预计 ${fmtBytes(t.est_bytes || 0)} · ${fmtDuration(t.est_seconds || 0)}`
      + (state.selectionMode === "simple" ? ` · ${{light:"轻度压缩",balanced:"均衡压缩",strong:"较强压缩"}[state.goal]}` : ` · 档位 ${data.profile || state.profileId}`)
      + (own ? `（其中 ${own} 个有单独设置）` : "") + used;
  }
  if ($("batchTotals")) $("batchTotals").classList.remove("stale");
  if ($("batchProgress")) {
    // Say WHEN it was computed: with an auto-refreshing panel, "is this number current?"
    // must be answerable at a glance rather than by clicking something and watching.
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    $("batchProgress").textContent =
      `已按本次目标重算于 ${pad(now.getHours())}:`
      + `${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }
  if ($("batchBasis")) {
    $("batchBasis").innerHTML = "体积依据：" + escapeHtml(data.size_basis || "")
      + "<br>用时依据：" + escapeHtml(data.time_basis || "")
      + (measured
        ? "<br><b>已用真实样片测算</b>：每个文件跑 3 段样片外推，误差通常在 ±20% 内。"
        : "<br>经验系数只保证量级正确；点「精确测算整批」会用真实样片替换上面的数字。");
  }
}

/** 精确测算整批: one file at a time, with visible progress and a real measured total. */
async function measureBatch() {
  const paths = batchPaths();
  if (!paths.length) { toast("先加入文件"); return; }
  const seq = state.batchSeq;
  const originalRows = state.batchEstimate?.rows || [];
  const btn = $("batchMeasureBtn");
  if (btn) btn.disabled = true;
  const measured = [];
  state.batchMeasuring = true;
  let bytes = 0;
  let seconds = 0;
  try {
    for (let index = 0; index < paths.length; index++) {
      if ($("batchProgress")) {
        $("batchProgress").textContent =
          `精确测算中… ${index + 1}/${paths.length}（每个文件跑 3 段样片，可随时点停止）`;
      }
      const data = await api("/api/measure", {
        method: "POST",
        body: JSON.stringify({ path: paths[index], ...fileSelection(state.files.find((f) => f.path === paths[index])), seconds: 6 }),
      });
      const est = data.estimate || {};
      if (seq !== state.batchSeq) { toast("目标或文件已变化，本次批量测算已过期，不再启动后续样片"); return; }
      bytes += est.mid || 0;
      seconds += est.est_seconds || 0;
      const original = originalRows.find((r) => r.path === paths[index]) || {};
      measured.push({ ...original, path: paths[index], mid: est.mid || 0, seconds: est.est_seconds || 0,
        est_bytes: est.mid || 0, est_low: est.low || 0, est_high: est.high || 0 });
      if (state.batchCancelled) { state.batchCancelled = false; break; }
    }
    const srcBytes = measured.reduce((sum, r) => {
      const f = state.files.find((x) => x.path === r.path);
      return sum + ((f && f.media && f.media.size_bytes) || 0);
    }, 0);
    renderBatchTotals({
      files: measured.length,
      complete: measured.length === paths.length,
      partial_measure: measured.length !== paths.length,
      rows: measured,
      totals: { src_bytes: srcBytes, est_bytes: bytes, est_seconds: seconds,
                est_low: measured.reduce((sum, r) => sum + r.est_low, 0),
                est_high: measured.reduce((sum, r) => sum + r.est_high, 0),
                ratio: srcBytes ? bytes / srcBytes : 0 },
      size_basis: "按每个文件的真实样片外推（不是档位系数）",
      time_basis: "按每个文件样片的实测耗时外推",
    }, "measured");
    if ($("batchProgress")) {
      $("batchProgress").textContent =
        `已精确测算 ${measured.length} 个文件（其余仍为经验系数）`;
    }
    toast(`精确测算完成：${measured.length} 个文件，预计共 ${fmtBytes(bytes)}`, 8000);
  } catch (err) {
    if ($("batchProgress")) $("batchProgress").textContent = "";
    toast("精确测算中断: " + err.message, 8000);
  } finally {
    state.batchMeasuring = false;
    if (btn) btn.disabled = false;
  }
}

/**
 * Group rows, keyed by `group.key`, patched in place.
 *
 * Two user-reported defects came from the first version of this function, and both had
 * the same shape: the row was REBUILT (and re-selected from the server's *suggestion*) on
 * every refresh.
 *
 *   1. "I picked a profile and the groups below didn't follow." The selected value came
 *      from `group.suggested_profile` -- a rule-based ADVICE that by design does not
 *      depend on what the user picked. It is now shown as advice, and the dropdown shows
 *      what will actually run: this group's explicit override, else the current profile.
 *   2. "I changed a group's profile and it snapped back when I clicked away." The change
 *      handler wrote the override and then called `refreshPlan()`, whose debounced
 *      `loadGroups()` re-rendered the row from the suggestion ~600 ms later. The override
 *      was still applied at submit time, so the UI showed one profile while the encoder
 *      would run another -- the worst version of this bug, because the screen lied.
 *
 * So: rows are created once per group key, the `<select>` element lives on, and its value
 * is always DERIVED (never remembered) -- which also means an empty override collapsing
 * back to the batch profile happens by itself.
 */
const groupRows = new Map();

/** Fresh group data by key. Never trust a captured object: rows outlive the data they
 *  were built from, and a handler closing over an old group writes to an old path list
 *  (measured: after adding files, a group change silently touched only one file while the
 *  row displayed four). */
function groupByKey(key) {
  return (state.groups || []).find((g) => g.key === key) || null;
}

/** What this group will actually run: a uniform override, else the batch profile. */
function groupEffective(group) {
  const main = state.selectionMode === "simple" ? "" : state.profileId;
  if (!group) return { value: main, mode: "batch", mixed: 0, count: 0 };
  const overrides = [];
  let missing = 0;
  for (const path of group.paths) {
    const entry = state.files.find((f) => f.path === path);
    if (!entry) continue;
    if (entry.override && entry.override.profile) overrides.push(entry.override.profile);
    else missing += 1;
  }
  const distinct = [...new Set(overrides)];
  if (!distinct.length) {
    return { value: main, mode: "batch", mixed: 0, count: 0 };
  }
  if (distinct.length === 1 && !missing) {
    return { value: distinct[0], mode: "override", mixed: 0, count: overrides.length };
  }
  if (distinct.length === 1) {
    // Same override, but not every file in the group has one.
    return { value: distinct[0], mode: "partial", mixed: missing, count: overrides.length };
  }
  return { value: main, mode: "mixed", mixed: distinct.length, count: overrides.length };
}

function ensureGroupRows(groups) {
  const box = $("groupList");
  if (!box) return;
  if (!groups.length) {
    box.innerHTML = '<p class="empty">加入文件后自动分组</p>';
    groupRows.clear();
    return;
  }
  const placeholder = box.querySelector(":scope > p.empty");
  if (placeholder) placeholder.remove();
  const seen = new Set();
  for (const group of groups) {
    seen.add(group.key);
    let entry = groupRows.get(group.key);
    if (!entry) {
      const row = document.createElement("div");
      row.className = "group-row";
      row.innerHTML = `
        <div class="group-head">
          <b class="group-title"></b>
          <span class="pill group-count"></span>
          <span class="hint group-size"></span>
        </div>
        <div class="group-body">
          <label class="inline">档位 <select class="group-profile"></select></label>
          <span class="hint group-advice"></span>
        </div>
        <div class="group-names hint"></div>
        <div class="group-warn warnline hidden"></div>`;
      entry = {
        key: group.key,
        el: row,
        title: row.querySelector(".group-title"),
        count: row.querySelector(".group-count"),
        size: row.querySelector(".group-size"),
        select: row.querySelector(".group-profile"),
        advice: row.querySelector(".group-advice"),
        names: row.querySelector(".group-names"),
        warn: row.querySelector(".group-warn"),
        optionsSig: "",
      };
      // The handler looks the group up by KEY at call time (see `groupByKey`).
      entry.select.addEventListener("change", () => {
        onGroupProfileChange(entry.key, entry.select.value);
      });
      groupRows.set(group.key, entry);
      box.appendChild(row);
    }
  }
  for (const [key, entry] of [...groupRows.entries()]) {
    if (!seen.has(key)) { entry.el.remove(); groupRows.delete(key); }
  }
}

function onGroupProfileChange(key, value) {
  const group = groupByKey(key);
  if (!group) return;
  // Setting a group back to the batch profile CLEARS the override instead of pinning the
  // same value: otherwise "I set it back" would leave a stale per-file override behind
  // that silently survives future changes to the main profile.
  const sameAsBatch = !value || (state.selectionMode === "manual" && value === state.profileId);
  let changed = 0;
  let missed = 0;
  for (const path of group.paths) {
    const entry = state.files.find((f) => f.path === path);
    if (!entry) { missed += 1; continue; }
    if (sameAsBatch) {
      if (entry.override) {
        delete entry.override.profile;
        if (!Object.keys(entry.override).length) entry.override = null;
      }
    } else {
      entry.override = entry.override || {};
      entry.override.profile = value;
    }
    changed += 1;
  }
  renderFiles();
  // No `refreshPlan()` here on purpose: it would re-fetch the grouping and rebuild this
  // row (the revert bug). The batch panel is refreshed without touching the rows -- and
  // that includes the BUDGET table, which depends on `estimate_at_profile` and therefore
  // goes stale the moment a group's profile changes.
  refreshBatch();
  syncGroupRows();
  // Say it when the write did not reach every file in the group: "只影响这一组" is a lie
  // if part of the group could not be matched.
  const tail = missed ? `（另有 ${missed} 个文件已不在列表中）` : "";
  toast(sameAsBatch
    ? `已把 ${changed} 个文件改回当前档位 ${value}（取消单独设置）${tail}`
    : `已把 ${changed} 个文件改用 ${value}（只影响这一组）${tail}`, 6000);
  // A group whose composition changed since it was fetched must be re-read, or the next
  // change would again use a stale path list.
  if (changed !== group.count) loadGroups({ force: true });
}

/** Re-derive every row's value from the real state. Cheap, no network, no rebuild. */
function syncGroupRows() {
  for (const [key, entry] of groupRows) {
    const group = groupByKey(key);
    if (!group) continue;
    const effective = groupEffective(group);
    const optionsSig = '<option value="">跟随当前主目标</option>' + profileOptionHtml(effective.value);
    if (entry.optionsSig !== optionsSig) {
      entry.optionsSig = optionsSig;
      entry.select.innerHTML = optionsSig;
    }
    if (entry.select.value !== effective.value) entry.select.value = effective.value;
    // A group whose files disagree must SAY so: showing one value for a group that will
    // actually be encoded three different ways is the kind of quiet lie this project
    // keeps removing. The advice stays visually separate from what will run.
    let warn = "";
    if (effective.mode === "mixed") {
      warn = `⚠ 本组内档位不一致（${effective.count} 个文件被单独设置过）；`
        + "在这里选一次即可把本组统一。";
    } else if (effective.mode === "partial") {
      warn = `本组有 ${effective.mixed} 个文件没有单独设置，将跟随主档位 ${state.profileId}。`;
    } else if (effective.mode === "override") {
      warn = `本组 ${effective.count} 个文件已单独设为 ${effective.value}（不随主档位变化）。`;
    }
    entry.warn.textContent = warn;
    entry.warn.classList.toggle("hidden", !warn);
    entry.advice.textContent = state.selectionMode === "simple" ? "未单独设置的文件按当前简单目标逐项选择算法，保留尺寸与内容。" : "工具建议：" + group.suggested_profile + " —— " + group.reason;
  }
}

function renderGroups(groups) {
  state.groups = groups || [];
  ensureGroupRows(state.groups);
  for (const group of state.groups) {
    const entry = groupRows.get(group.key);
    if (!entry) continue;
    entry.title.textContent = `${group.resolution} · ${group.duration} · ${group.codec}`
      + (group.hdr ? " · HDR" : "");
    entry.count.textContent = `${group.count} 个`;
    entry.size.textContent = `共 ${fmtBytes(group.src_bytes)} · ${fmtDuration(group.total_seconds)}`;
    entry.names.textContent = group.names.slice(0, 6).join("、")
      + (group.names.length > 6 ? ` 等 ${group.names.length} 个` : "");
  }
  syncGroupRows();
}

/** Signature of the current file selection, so grouping is only re-fetched when it changed. */
function batchSignature() {
  return batchPaths().slice().sort().join("\u0000");
}

async function loadGroups({ force = false } = {}) {
  const paths = batchPaths();
  if (!paths.length) {
    state.groups = [];
    state.groupSignature = "";
    renderGroups([]);
    return;
  }
  const signature = batchSignature();
  if (!force && signature === state.groupSignature && state.groups) {
    syncGroupRows();       // same files: just re-derive the values, no request
    return;
  }
  try {
    const data = await api("/api/group", {
      method: "POST", body: JSON.stringify({ paths }),
    });
    state.groupSignature = signature;
    renderGroups(data.groups || []);
  } catch (err) {
    if ($("groupList")) {
      $("groupList").innerHTML =
        `<p class="job-error">自动分组失败：${escapeHtml(err.message)}</p>`;
      groupRows.clear();
    }
  }
}

async function applyBudget({ quiet = false } = {}) {
  const paths = batchPaths();
  if (!paths.length) { toast("先加入文件"); return; }
  const total = budgetBytes();
  if (!total) { toast("请填写总预算"); return; }
  try {
    const data = await api("/api/budget", {
      method: "POST",
      body: JSON.stringify(batchBody({ total_bytes: total })),
    });
    state.budget = data;
    renderBudget(data, { quiet });
  } catch (err) {
    if ($("budgetResult")) {
      $("budgetResult").classList.remove("stale");
      $("budgetResult").innerHTML =
        `<p class="job-error">预算分配失败：${escapeHtml(err.message)}</p>`;
    }
  }
}

function renderBudget(data, { quiet = false } = {}) {
  const box = $("budgetResult");
  if (!box) return;
  box.classList.remove("stale");
  const head = `<div class="hint">${escapeHtml(data.basis || "")}</div>`;
  const warn = [];
  if (data.squeezed) {
    warn.push(`<div class="warnline">⚠ ${data.squeezed} 个文件会被压得比档位本身更狠`
      + `（预算只有按档位的 ${Math.round((data.rows[0] || {}).tightness * 100 || 0)}% 左右）：`
      + `${escapeHtml((data.squeezed_names || []).slice(0, 5).join("、"))}`
      + `${(data.squeezed_names || []).length > 5 ? " 等" : ""}</div>`);
  }
  if (data.unreachable) {
    warn.push(`<div class="job-error">${data.unreachable} 个文件分配到的 bpp 低于任何编码器
      能承受的下限 —— 这个总预算需要下调分辨率或抽帧才可能达到。</div>`);
  }
  const rows = data.rows.map((r) => `
    <tr>
      <td>${escapeHtml(r.name)}<div class="hint">${r.duration ? fmtDuration(r.duration) : ""}</div></td>
      <td>${fmtBytes(r.allocated_bytes)}<div class="hint">占 ${(r.share * 100).toFixed(1)}%</div></td>
      <td>${r.bpp ? r.bpp.toFixed(4) : "—"}<div class="hint">${
        r.tightness ? `按档位预计 ${fmtBytes(r.estimate_at_profile)}（${Math.round(r.tightness * 100)}%）` : ""}</div></td>
      <td><span class="budget-verdict ${escapeHtml(r.verdict)}">${
        { comfortable: "安全", normal: "常规", degraded: "明显劣化", extreme: "极限",
          impossible: "不可达", unknown: "未知" }[r.verdict] || r.verdict}</span></td>
    </tr>`).join("");
  box.innerHTML = head + warn.join("")
    + `<table class="about-table budget-table">
        <thead><tr><th>文件</th><th>分配体积</th><th>bpp</th><th>判定</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
  if ($("budgetHint")) {
    $("budgetHint").textContent =
      `共 ${fmtBytes(data.total_bytes)} · 源 ${fmtBytes(data.src_bytes)} · ${data.files} 个文件`
      + ` · 按档位 ${data.profile || state.profileId} 分配`;
  }
  if (quiet) {
    // A background recompute must not spam toasts -- but it must be visible that the table
    // now describes the CURRENT profile, which the hint above says.
    return;
  }
  toast(`预算已按档位 ${data.profile || state.profileId} 分配`, 5000);
}


/* ----------------------------------------------------------- 对比播放 --- */

function compareUrl(path) {
  return "/media?path=" + encodeURIComponent(path);
}

/**
 * Side-by-side source vs output with ONE shared timeline.
 *
 * Two independent players drift apart and make the comparison useless, so only the
 * LEFT video is interactive: its timeupdate/seeked/play/pause drive the right one.
 * Audio comes from a single chosen side, otherwise the soundtrack doubles up.
 */
function openCompare(srcPath, outPath, job) {
  const overlay = document.createElement("div");
  overlay.className = "preview-overlay";
  const ratio = job && job.src_size && job.total_size
    ? `${(job.total_size / job.src_size * 100).toFixed(0)}%` : "";
  const gap = job && job.verify ? job.verify.max_keyframe_gap : null;
  overlay.innerHTML = `
    <div class="compare-box">
      <div class="panel-head">
        <h2>对比：源 vs 输出</h2>
        <div class="panel-actions">
          <label class="inline">声音
            <select id="cmpAudio">
              <option value="left">源</option>
              <option value="right" selected>输出</option>
              <option value="none">静音</option>
            </select>
          </label>
          <button class="ghost" id="cmpClose">关闭</button>
        </div>
      </div>
      <div class="compare-grid">
        <div>
          <div class="compare-label">源 ${job ? "· " + fmtBytes(job.src_size) : ""}</div>
          <video id="cmpLeft" src="${compareUrl(srcPath)}" preload="metadata" muted></video>
        </div>
        <div>
          <div class="compare-label">输出 ${job ? "· " + fmtBytes(job.total_size) +
            (ratio ? " （" + ratio + "）" : "") : ""}</div>
          <video id="cmpRight" src="${compareUrl(outPath)}" preload="metadata" muted></video>
        </div>
      </div>
      <div class="compare-controls">
        <button class="ghost" id="cmpPlay">播放 / 暂停</button>
        <input type="range" id="cmpSeek" min="0" max="1000" value="0" step="1">
        <span id="cmpTime" class="hint">0:00</span>
        <span class="cmp-audio">
          <button class="ghost" id="cmpMute" title="静音 / 恢复">🔊</button>
          <input type="range" id="cmpVolume" min="0" max="100" step="1" value="100"
                 title="音量" aria-label="音量">
          <span id="cmpVolumeLabel" class="hint">100%</span>
        </span>
        <button class="ghost" id="cmpFull" title="全屏（左侧画面）">⛶</button>
      </div>
      <p class="hint">
        拖动滑块或点画面播放/暂停，右侧同步跟随 —— 用同一时间点对比画质差异。
        ${gap !== null && gap !== undefined ? "该输出的最大关键帧间隔 " + gap + "s。" : ""}
      </p>
      <details class="quality-panel" id="cmpQualityPanel">
        <summary>🔬 画质热点分析 <span id="cmpQualityNote" class="hint">找出画质最差的几帧</span></summary>
        <p class="hint">
          码率、CRF 这些数字不会告诉你「哪里变差了」。这里用<b>逐帧 VMAF</b> 排序列出最差的几帧，
          再把那一处按 1:1 放大并排对比；「闪烁」是判断「能不能看出来」的经典方法 ——
          同一位置来回切换，肉眼能看出跳变就是真的看得见。
        </p>
        <div class="qp-controls">
          <button class="ghost" id="cmpAnalyze">开始分析（较慢）</button>
          <button class="ghost" id="cmpBanding" title="CAMBI 是无参考指标，只扫成品即可，比 VMAF 快">扫描色带（只需成品）</button>
          <button class="ghost" id="cmpArtifacts" title="块效应与模糊度：VMAF 对这两类结构性损伤不够敏感">块效应 / 锐度</button>
          <label class="inline">放大
            <select id="cmpZoom">
              <option value="1">1× 整帧</option>
              <option value="2" selected>2× 中心</option>
              <option value="4">4× 中心</option>
              <option value="8">8× 中心</option>
            </select>
          </label>
          <label class="inline" title="同位置以 2Hz 来回切换——看不出跳变就是真的看不出">
            <input type="checkbox" id="cmpFlicker"> 闪烁对比
          </label>
          <label class="inline" title="把源与成品的逐像素差异放大给人看。它同时放大噪声和对齐误差，倍数也没有权威取值，所以只用来找“哪里变了”，不能用来判断画质好坏">
            <input type="checkbox" id="cmpDiff"> 差值图（只作定位）
          </label>
          <label class="inline">倍数
            <select id="cmpGain">
              <option value="1">1×</option>
              <option value="2">2×</option>
              <option value="4" selected>4×</option>
              <option value="8">8×</option>
              <option value="16">16×</option>
            </select>
          </label>
          <span id="cmpPooled" class="hint"></span>
        </div>
        <div id="cmpBandingBox" class="banding-box hidden"></div>
        <div id="cmpArtifactBox" class="banding-box hidden"></div>
        <div id="cmpWorst" class="worst-list"></div>
        <div id="cmpStage" class="crop-stage hidden">
          <div class="crop-side">
            <div class="compare-label">源（缩到成品分辨率后 1:1）</div>
            <img id="cmpCropSrc" alt="源">
          </div>
          <div class="crop-side">
            <div class="compare-label">成品 1:1</div>
            <img id="cmpCropOut" alt="成品">
          </div>
        </div>
        <div id="cmpDiffBox" class="crop-stage hidden">
          <div class="crop-side">
            <div class="compare-label" id="cmpDiffLabel">差值图 |源 − 成品|</div>
            <img id="cmpDiffImg" alt="差值图">
          </div>
        </div>
        <div id="cmpDiffNote" class="hint"></div>
        <div id="cmpScaleNote" class="hint"></div>
      </details>
    </div>`;

  overlay.addEventListener("click", (e) => {
    if (e.target.id === "cmpClose" || e.target.id === "cmpCloseBtn") overlay.vcsClose();
  });
  // Releasing a drag outside the panel must not dismiss it, and Esc must work: both are
  // handled by the shared dismiss wiring.
  wireOverlayDismiss(overlay, () => {
    overlay.querySelectorAll("video").forEach((v) => v.pause());
    releaseDiffUrl();
    // The flicker interval paints into the two crop <img> elements; left running after
    // the modal is gone it keeps a timer alive against detached nodes.
    if (flickerTimer !== null) { clearInterval(flickerTimer); flickerTimer = null; }
  });
  document.body.appendChild(overlay);

  const left = overlay.querySelector("#cmpLeft");
  const right = overlay.querySelector("#cmpRight");
  const seek = overlay.querySelector("#cmpSeek");
  const timeLabel = overlay.querySelector("#cmpTime");
  const audioSel = overlay.querySelector("#cmpAudio");
  const playBtn = overlay.querySelector("#cmpPlay");
  const volume = overlay.querySelector("#cmpVolume");
  const volumeLabel = overlay.querySelector("#cmpVolumeLabel");
  const muteBtn = overlay.querySelector("#cmpMute");
  const fullBtn = overlay.querySelector("#cmpFull");

  // Neither player keeps its native controls: the only volume slider a user could
  // see used to belong to the LEFT video, which is muted by default (the output
  // side owns the sound), so dragging it did nothing audible.  One control bar now
  // owns playback for both, and its volume always reaches whichever side is
  // actually audible.
  left.controls = false;
  right.controls = false;
  let syncing = false;
  let lastAudible = "right";

  /** Volume is applied to BOTH videos, so switching 源↔输出 needs no re-apply. */
  function applyVolume() {
    const v = Math.max(0, Math.min(1, Number(volume.value) / 100));
    left.volume = v;
    right.volume = v;
    volumeLabel.textContent = `${Math.round(v * 100)}%`;
    try { localStorage.setItem("vcs-cmp-volume", String(Math.round(v * 100))); } catch { /* ignore */ }
  }

  function applyAudio() {
    const choice = audioSel.value;
    if (choice !== "none") lastAudible = choice;
    left.muted = choice !== "left";
    right.muted = choice !== "right";
    const silent = choice === "none";
    muteBtn.textContent = silent ? "🔇" : "🔊";
    muteBtn.classList.toggle("active", silent);
  }

  function toggleMute() {
    audioSel.value = audioSel.value === "none" ? lastAudible : "none";
    applyAudio();
  }

  try {
    const saved = Number(localStorage.getItem("vcs-cmp-volume"));
    if (isFinite(saved) && saved >= 0 && saved <= 100) volume.value = String(saved);
  } catch { /* ignore */ }
  applyVolume();
  applyAudio();
  volume.addEventListener("input", applyVolume);
  audioSel.addEventListener("change", applyAudio);
  muteBtn.addEventListener("click", toggleMute);

  fullBtn.addEventListener("click", () => {
    const doc = document;
    if (doc.fullscreenElement) doc.exitFullscreen().catch(() => {});
    else left.requestFullscreen?.().catch(() => {});
  });

  function follower() {
    if (syncing) return;           // ignore the echo of our own seek
    syncing = true;
    try {
      if (isFinite(left.currentTime) &&
          Math.abs(right.currentTime - left.currentTime) > 0.08) {
        right.currentTime = left.currentTime;
      }
    } catch { /* right side not ready yet */ }
    syncing = false;
  }

  left.addEventListener("timeupdate", () => {
    follower();
    const d = left.duration || 0;
    if (d) {
      seek.value = String(Math.round(left.currentTime / d * 1000));
      timeLabel.textContent = `${fmtClock(left.currentTime)} / ${fmtClock(d)}`;
    }
  });
  left.addEventListener("seeked", follower);
  left.addEventListener("play", () => { right.play().catch(() => {}); });
  left.addEventListener("pause", () => { right.pause(); });
  left.addEventListener("ratechange", () => { right.playbackRate = left.playbackRate; });

  function togglePlay() {
    if (left.paused) left.play().catch(() => {}); else left.pause();
  }
  // Click/double-click on either picture: playback is what a click should do here
  // (the old hint implied clicking seeks, which never happened).
  for (const v of [left, right]) {
    v.addEventListener("click", togglePlay);
    v.addEventListener("dblclick", () => {
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      else v.requestFullscreen?.().catch(() => {});
    });
  }

  seek.addEventListener("input", () => {
    const d = left.duration || 0;
    if (d) left.currentTime = Number(seek.value) / 1000 * d;
  });

  playBtn.addEventListener("click", togglePlay);

  right.addEventListener("seeked", () => {
    if (syncing) return;
    syncing = true;
    try { left.currentTime = right.currentTime; } catch { /* ignore */ }
    syncing = false;
  });

  /* ------------------------------------------- V1/V2/V3: where & how bad --- */

  const analyzeBtn = overlay.querySelector("#cmpAnalyze");
  const zoomSel = overlay.querySelector("#cmpZoom");
  const flickerBox = overlay.querySelector("#cmpFlicker");
  const worstBox = overlay.querySelector("#cmpWorst");
  const pooledNote = overlay.querySelector("#cmpPooled");
  const bandingBtn = overlay.querySelector("#cmpBanding");
  const bandingBox = overlay.querySelector("#cmpBandingBox");
  const artifactBtn = overlay.querySelector("#cmpArtifacts");
  const artifactBox = overlay.querySelector("#cmpArtifactBox");
  const stage = overlay.querySelector("#cmpStage");
  const cropSrc = overlay.querySelector("#cmpCropSrc");
  const cropOut = overlay.querySelector("#cmpCropOut");
  const scaleNote = overlay.querySelector("#cmpScaleNote");
  let analysis = null;
  let currentTime = 0;
  let flickerTimer = null;

  /** URL for one side of the 1:1 comparison, at a common resolution + crop. */
  function cropUrl(path, at, common, zoom, side) {
    const params = new URLSearchParams({
      path, t: String(at), w: String(common.w), h: String(common.h),
      zoom: String(zoom), q: "3", side,
    });
    return "/api/frame?" + params.toString();
  }

  function loadCrops() {
    if (!analysis || !analysis.frame) return;
    const common = analysis.frame;
    const zoom = Number(zoomSel.value) || 2;
    cropSrc.src = cropUrl(srcPath, currentTime, common, zoom, "src");
    cropOut.src = cropUrl(outPath, currentTime, common, zoom, "out");
    const z = zoom <= 1 ? "整帧" : `${zoom}× 中心区域`;
    scaleNote.textContent =
      `对比口径：两侧都缩放到成品分辨率 ${common.width}x${common.height}，再取${z}，1:1 像素显示。`
      + "　这个口径不含降分辨率本身的损失（那部分在整帧视图里才看得到）。";
    loadDiff();
  }

  /* ------------------------------------------- V6: 差值图 —— 只作定位 --- */

  const diffBox = overlay.querySelector("#cmpDiff");
  const gainSel = overlay.querySelector("#cmpGain");
  const diffPanel = overlay.querySelector("#cmpDiffBox");
  const diffImg = overlay.querySelector("#cmpDiffImg");
  const diffLabel = overlay.querySelector("#cmpDiffLabel");
  const diffNote = overlay.querySelector("#cmpDiffNote");
  let diffUrl = null;

  /**
   * Ask the backend for the amplified difference at the current frame.
   *
   * Fetched rather than pointed at with <img src>: when the two sides cannot be paired
   * frame by frame the backend REFUSES (409) with a reason, and an <img> cannot show
   * that reason -- the user would just see a broken picture and assume a bug.
   */
  async function loadDiff() {
    if (!diffBox || !diffBox.checked) return;
    diffPanel.classList.remove("hidden");
    if (!analysis || !analysis.frame) {
      // The 1:1 stage and this map share the analysed frame, so there is nothing to
      // show yet -- say so instead of leaving an empty panel behind a checked box.
      diffImg.removeAttribute("src");
      diffNote.className = "hint";
      diffNote.textContent = "先点「开始分析（较慢）」定下要看的那一帧，再回来看差值图。";
      return;
    }
    const common = analysis.frame;
    const zoom = Number(zoomSel.value) || 2;
    const gain = Number(gainSel.value) || 4;
    const params = new URLSearchParams({
      src: srcPath, out: outPath, t: String(currentTime),
      zoom: String(zoom), gain: String(gain),
    });
    diffLabel.textContent = `差值图 |源 − 成品| ×${gain}`;
    try {
      const res = await fetch("/api/frame/diff?" + params.toString());
      if (!res.ok) {
        let reason = `HTTP ${res.status}`;
        try { reason = (await res.json()).error || reason; } catch { /* keep the code */ }
        if (diffUrl) { URL.revokeObjectURL(diffUrl); diffUrl = null; }
        diffImg.removeAttribute("src");
        diffNote.className = "hint space-error";
        diffNote.textContent = `不出差值图：${reason}`;
        return;
      }
      const blob = await res.blob();
      const basis = {
        common: res.headers.get("X-VCS-Common") || `${common.width}x${common.height}`,
        flags: res.headers.get("X-VCS-Flags") || "bilinear",
        gain: res.headers.get("X-VCS-Gain") || String(gain),
        saturate: res.headers.get("X-VCS-Saturate") || "",
        crop: res.headers.get("X-VCS-Crop") || "full",
      };
      if (diffUrl) URL.revokeObjectURL(diffUrl);
      diffUrl = URL.createObjectURL(blob);
      diffImg.src = diffUrl;
      const cropText = basis.crop === "full" ? "整帧" : `裁剪 ${basis.crop}`;
      const satText = Number(basis.saturate) < 255
        ? `差异 ≥ ${basis.saturate} 的全部变成纯白（饱和，这一档以上不再有区分度）；`
        : "";
      diffNote.className = "hint";
      // The caveats are not decoration: the whole risk of this view is that a bright
      // pixel gets read as "bad picture". State what it does and does not mean.
      diffNote.textContent =
        `口径：两侧都放到共同分辨率 ${basis.common}（缩放 ${basis.flags}），同一时间点、`
        + `同一${cropText}，只比亮度（Y）平面；已放大 ×${basis.gain}。`
        + satText
        + "⚠️ 只作定位，不作判决：差值同时放大噪声与对齐误差，倍数也没有权威取值——"
        + "亮的地方只是“这里不一样”，不等于“这里更差”。"
        + "两侧帧率或帧数不一致时不会出图（那会拿不同时刻的画面相减）。";
    } catch (err) {
      diffNote.className = "hint space-error";
      diffNote.textContent = "差值图生成失败：" + err.message;
    }
  }

  // Every blob URL made for the diff image is released when the comparison view is
  // torn down. Only the "checkbox unticked" path revoked it, so closing the modal with
  // the diff showing leaked the whole decoded PNG until the page was reloaded (and the
  // view can be opened repeatedly).
  function releaseDiffUrl() {
    if (diffUrl) { URL.revokeObjectURL(diffUrl); diffUrl = null; }
  }
  overlay.addEventListener("vcs:teardown", releaseDiffUrl);

  if (diffBox) {
    diffBox.addEventListener("change", () => {
      if (diffBox.checked) {
        loadDiff();
      } else {
        diffPanel.classList.add("hidden");
        diffNote.textContent = "";
        diffNote.className = "hint";
        if (diffUrl) { URL.revokeObjectURL(diffUrl); diffUrl = null; }
        diffImg.removeAttribute("src");
      }
    });
  }
  if (gainSel) gainSel.addEventListener("change", loadDiff);

  function seekTo(seconds) {
    currentTime = seconds;
    try { left.currentTime = seconds; } catch { /* not ready */ }
    try { right.currentTime = seconds; } catch { /* not ready */ }
    loadCrops();
  }

  function renderWorst() {
    if (!analysis || !analysis.ok) {
      worstBox.innerHTML = "";
      return;
    }
    const p = analysis.pooled || {};
    const vif = analysis.vif_scales || [];
    pooledNote.textContent =
      `${analysis.frame_count} 帧 · 均值 ${p.mean} · 最差 ${p.min} · 调和平均 ${p.harmonic_mean}`;
    const vifNote = (vif.length === 2 && vif[0] > 0)
      ? `<div class="worst-hint">细节损失分布：高频尺度 VIF ${vif[0]} ／ 最低频尺度 VIF ${vif[1]}`
        + `　—— 高频明显更低，说明丢的是纹理与锐度（先死的总是高频）</div>`
      : "";
    worstBox.innerHTML = vifNote + analysis.worst.map((f) => `
      <button class="worst-item" data-time="${f.time}">
        <span class="wi-time">${fmtClock(f.time)}</span>
        <span class="wi-score ${f.vmaf < 80 ? "bad" : f.vmaf < 90 ? "mid" : "ok"}">${f.vmaf.toFixed(1)}</span>
        <span class="wi-hint">跳到这一帧看 1:1</span>
      </button>`).join("");
  }

  if (analyzeBtn) {
    analyzeBtn.addEventListener("click", async () => {
      analyzeBtn.disabled = true;
      const old = analyzeBtn.textContent;
      analyzeBtn.textContent = "分析中…（要解码两边，可能要几十秒）";
      try {
        const data = await api("/api/quality/analyze", {
          method: "POST",
          body: JSON.stringify({ out_path: outPath, src_path: srcPath, seconds: 6 }),
        });
        analysis = data.analysis || null;
        if (!analysis || !analysis.ok) {
          toast("分析失败：" + ((analysis && analysis.error) || "未知原因"), 8000);
          worstBox.innerHTML = "";
          return;
        }
        renderWorst();
        // The analyze pass carries CAMBI for free, so show the banding verdict
        // immediately rather than making the user press a second button.
        if (analysis.banding && analysis.banding.pooled) {
          renderBanding(analysis.banding, (analysis.pooled || {}).mean);
        }
        // Land on the single worst frame: that is the answer the user came for.
        const first = (analysis.worst || [])[0];
        if (first) seekTo(first.time);
        stage.classList.remove("hidden");
        toast(`已找出 ${analysis.worst.length} 处最差画面`, 6000);
      } catch (err) {
        toast("分析失败: " + err.message, 8000);
      } finally {
        analyzeBtn.disabled = false;
        analyzeBtn.textContent = old || "开始分析（较慢）";
      }
    });
  }

  if (worstBox) {
    worstBox.addEventListener("click", (ev) => {
      const item = ev.target.closest(".worst-item");
      if (!item) return;
      seekTo(Number(item.dataset.time) || 0);
      worstBox.querySelectorAll(".worst-item").forEach((b) => b.classList.remove("active"));
      item.classList.add("active");
    });
  }

  if (zoomSel) zoomSel.addEventListener("change", loadCrops);

  /**
   * Render the CAMBI verdict, and -- when it disagrees with VMAF -- say so loudly.
   *
   * This contrast is the whole reason the check exists. Measured on our own encoder:
   * a flat gradient at CRF 30-48 scores VMAF 96-97.5 (which reads as "肉眼无损")
   * while CAMBI reports 14-22 (明显到严重色带). A high VMAF next to visible banding
   * is exactly the "数字很好看，画面却有问题" case, so it must not be left for the
   * user to notice by comparing two panels themselves.
   */
  function renderBanding(band, vmafMean) {
    if (!bandingBox) return;
    if (!band || !band.pooled) {
      bandingBox.classList.add("hidden");
      return;
    }
    const worst = band.worst_score || 0;
    const level = worst >= 20 ? "bad" : worst >= 10 ? "mid" : worst >= 5 ? "warn" : "ok";
    const conflict = worst >= 10 && (vmafMean || 0) >= 90;
    const dots = (band.worst || []).length
      ? `<div class="banding-worst">${band.worst.map((f) =>
          `<button class="worst-item" data-time="${f.time}">
             <span class="wi-time">${fmtClock(f.time)}</span>
             <span class="wi-score ${level === "ok" ? "ok" : level === "bad" ? "bad" : "mid"}">${f.cambi.toFixed(1)}</span>
           </button>`).join("")}</div>`
      : "";
    bandingBox.innerHTML =
      `<div class="banding-head">
         <span class="banding-verdict ${level}">渐变色带：${escapeHtml(band.plain)}</span>
         <span class="hint">CAMBI 均值 ${band.pooled.mean} · 最差 ${worst}（论文判据 &lt;5 为无色带）</span>
       </div>
       <div class="hint">${escapeHtml(band.explanation)}</div>`
      + (conflict
          ? `<div class="banding-conflict">⚠ 这里 VMAF 说「${(vmafMean || 0).toFixed(1)}（几乎无损）」，`
            + `CAMBI 却说「${escapeHtml(band.plain)}」。两者不矛盾：<b>VMAF 对平坦渐变里的色带几乎不敏感</b>，`
            + `所以「数字好看」不代表渐变区没问题 —— 这一项正是补它。</div>`
          : "")
      + dots;
    bandingBox.classList.remove("hidden");
  }

  function runBanding() {
    if (!bandingBtn) return;
    bandingBtn.disabled = true;
    const old = bandingBtn.textContent;
    bandingBtn.textContent = "扫描中…";
    api("/api/quality/banding", {
      method: "POST", body: JSON.stringify({ path: outPath, seconds: 8 }),
    }).then((data) => {
      const band = (data && data.banding) || null;
      if (!band || !band.ok) {
        toast("色带扫描失败：" + ((band && band.error) || "未知原因"), 8000);
        return;
      }
      const vmafMean = analysis && analysis.pooled ? analysis.pooled.mean : 0;
      renderBanding(band, vmafMean);
      toast(band.note || "色带扫描完成", 8000);
    }).catch((err) => {
      toast("色带扫描失败: " + err.message, 8000);
    }).finally(() => {
      bandingBtn.disabled = false;
      bandingBtn.textContent = old || "扫描色带（只需成品）";
    });
  }
  if (bandingBtn) bandingBtn.addEventListener("click", runBanding);

  /**
   * Blocking / blur against the source (V5).
   *
   * Verdicts are deliberately relative. `blockdetect` has no accepted absolute
   * threshold and `blurdetect` only has content-dependent empirical anchors, so the
   * source is the baseline and the panel reports the change -- with the numbers shown
   * so the user can judge rather than trust a band.
   */
  function renderArtifacts(data) {
    if (!artifactBox) return;
    if (!data || !data.ok) {
      artifactBox.classList.add("hidden");
      return;
    }
    const levels = { ok: "ok", warn: "mid", bad: "bad", better: "ok", unknown: "warn" };
    const rows = (data.verdicts || []).map((v) =>
      `<div class="artifact-row"><span class="banding-verdict ${levels[v.level] || "warn"}">`
      + `${escapeHtml(v.label)}</span><span class="hint">${escapeHtml(v.detail)}</span></div>`
    ).join("");
    const worst = (data.worst_block_frames || []).length
      ? `<div class="banding-worst">${data.worst_block_frames.map((f) =>
          `<button class="worst-item" data-time="${f.time}">
             <span class="wi-time">${fmtClock(f.time)}</span>
             <span class="wi-score mid">块 ${f.block.toFixed(1)}</span>
           </button>`).join("")}</div>`
      : "";
    artifactBox.innerHTML =
      `<div class="banding-head">
         <span class="banding-verdict">块效应 / 锐度</span>
         <span class="hint">共同分辨率 ${escapeHtml(data.common_size)}</span>
       </div>
       <div class="hint">基准：${escapeHtml(data.basis || "")}</div>${rows}${worst}
       <div class="hint">${escapeHtml(data.note || "").replace(/\*\*/g, "")}</div>`;
    artifactBox.classList.remove("hidden");
  }

  if (artifactBtn) {
    artifactBtn.addEventListener("click", () => {
      artifactBtn.disabled = true;
      const old = artifactBtn.textContent;
      artifactBtn.textContent = "测量中…";
      api("/api/quality/artifacts", {
        method: "POST",
        body: JSON.stringify({ out_path: outPath, src_path: srcPath, seconds: 6 }),
      }).then((data) => {
        renderArtifacts(data && data.artifacts);
        const v = ((data && data.artifacts && data.artifacts.verdicts) || [])
          .map((x) => x.detail).join("；");
        toast(v || "测量完成", 9000);
      }).catch((err) => {
        toast("块效应/锐度测量失败: " + err.message, 8000);
      }).finally(() => {
        artifactBtn.disabled = false;
        artifactBtn.textContent = old || "块效应 / 锐度";
      });
    });
  }
  if (artifactBox) {
    artifactBox.addEventListener("click", (ev) => {
      const item = ev.target.closest(".worst-item");
      if (!item) return;
      seekTo(Number(item.dataset.time) || 0);
    });
  }

  // Banding hot-spots are clickable too (same seek behaviour as VMAF hot-spots).
  if (bandingBox) {
    bandingBox.addEventListener("click", (ev) => {
      const item = ev.target.closest(".worst-item");
      if (!item) return;
      seekTo(Number(item.dataset.time) || 0);
    });
  }

  /**
   * Flicker: the two crops stacked at the SAME position and swapped at 2 Hz.
   *
   * Same position is the whole point -- side-by-side flicker compares two different
   * places on screen and the eye cannot hold them. 2 Hz (500 ms) is slow enough that
   * each image registers; faster than that and the two fuse into one blurry image.
   */
  function applyFlicker() {
    clearInterval(flickerTimer);
    flickerTimer = null;
    const on = !!(flickerBox && flickerBox.checked);
    stage.classList.toggle("flickering", on);
    if (!on) {
      cropSrc.style.visibility = "";
      cropOut.style.visibility = "";
      return;
    }
    let showSource = true;
    const paint = () => {
      cropSrc.style.visibility = showSource ? "visible" : "hidden";
      cropOut.style.visibility = showSource ? "hidden" : "visible";
      showSource = !showSource;
    };
    paint();
    flickerTimer = setInterval(paint, 500);
  }
  if (flickerBox) flickerBox.addEventListener("change", applyFlicker);

  // Tidy up the timer with the modal, or it keeps running against detached nodes.
  // (The dismiss wiring itself is registered near the top of this function, where the
  // overlay is created; this block only had the timer cleanup and a second backdrop
  // handler, which is why it used to leave the modal on screen.)
}

/* ------------------------------------------------------------------ preview */

function openPreview(path, verify, preview = null) {
  const overlay = document.createElement("div");
  overlay.className = "preview-overlay";
  const url = "/media?path=" + encodeURIComponent(path);
  const checks = verify && verify.checks
    ? verify.checks.filter((c) => c.status !== "skipped")
        .map((c) => `<li class="${escapeHtml(c.status)}">${escapeHtml(c.label)}：${escapeHtml(c.detail)}</li>`).join("")
    : "";
  const pv = preview
    ? `<ul class="verify-list">
         <li class="${preview.seekOk ? "ok" : "fail"}">关键帧：${preview.keyframes} 个，最大间隔 ${preview.gap ?? "-"}s ${preview.seekOk ? "（可自由拖动）" : "（拖动可能卡顿）"}</li>
         <li class="ok">样片区间：第 ${preview.start}s 起 ${preview.seconds}s，${fmtBytes(preview.size)}</li>
       </ul>`
    : "";
  overlay.innerHTML = `
    <div class="preview-box">
      <div class="panel-head">
        <h2>预览：${path.split(/[\\/]/).pop()}</h2>
        <button class="ghost" id="pvClose">关闭</button>
      </div>
      <video src="${url}" controls autoplay></video>
      <p class="hint">直接在进度条上多点几下拖动，就能验证「跳转是否顺畅」。</p>
      ${pv}${checks ? `<ul class="verify-list">${checks}</ul>` : ""}
    </div>`;
  overlay.addEventListener("click", (e) => {
    if (e.target.id === "pvClose") overlay.vcsClose();
  });
  wireOverlayDismiss(overlay, () => {
    const v = overlay.querySelector("video");
    if (v) { v.pause(); v.removeAttribute("src"); }
  });
  document.body.appendChild(overlay);
}

/* ------------------------------------------------------------------- browse */

let browserState = { path: "", entries: [], target: null };

// `target` turns the browser into a folder picker: the chosen directory is
// written into that input instead of being added as media.
async function openBrowser(path, target) {
  try {
    const data = await api("/api/browse", {
      method: "POST",
      body: JSON.stringify({ path: path || "" }),
    });
    browserState = { path: data.path, entries: data.entries, target: target || null };
    $("browserPath").value = data.path;
    renderBrowser();
    $("browserPick").textContent = browserState.target
      ? "选这个目录" : "把本目录视频全部加入";
    $("browserModal").classList.remove("hidden");
  } catch (err) {
    toast("无法浏览: " + err.message);
  }
}

function useDirectory(dir) {
  const target = browserState.target;
  if (!target) return false;
  $(target).value = dir;
  $("browserModal").classList.add("hidden");
  if (target === "optDefaultDir") {
    saveDefaultDir(false);
  } else {
    toast("这次将保存到：" + dir);
    refreshPlan();
  }
  return true;
}

function renderBrowser() {
  const box = $("browserList");
  box.innerHTML = "";
  if (!browserState.path) {
    browserState.entries.forEach((e) => {
      const row = document.createElement("div");
      row.className = "modal-row";
      row.innerHTML = `<span>📁 ${e.name}</span>`;
      row.addEventListener("click", () => openBrowser(e.path));
      box.appendChild(row);
    });
    return;
  }
  const up = document.createElement("div");
  up.className = "modal-row";
  up.innerHTML = `<span>⬆ ..</span>`;
  up.addEventListener("click", () => {
    const parent = browserState.path.replace(/[\\/][^\\/]*$/, "");
    openBrowser(parent || "");
  });
  box.appendChild(up);

  for (const e of browserState.entries) {
    const row = document.createElement("div");
    row.className = "modal-row";
    const isVideo = !e.dir && VIDEO_EXT.has(extOf(e.name));
    row.innerHTML = `<span>${e.dir ? "📁" : isVideo ? "🎬" : "📄"} ${e.name}</span>
      <span class="size">${e.dir ? "" : fmtBytes(e.size)}</span>`;
    if (!e.dir && !isVideo) { row.style.opacity = ".4"; }
    row.addEventListener("click", () => {
      if (e.dir) {
        // In folder-picker mode a single click navigates, and the footer button
        // (or a double click) commits the directory.
        openBrowser(e.path, browserState.target);
      } else if (isVideo) {
        addPaths([e.path]);
        toast("已加入：" + e.name);
      }
    });
    if (e.dir && browserState.target) {
      row.addEventListener("dblclick", () => useDirectory(e.path));
    }
    box.appendChild(row);
  }
}

async function addWholeDir() {
  if (browserState.target) {
    if (!browserState.path) { toast("请先进入一个目录"); return; }
    useDirectory(browserState.path);
    return;
  }
  if (!browserState.path) { toast("请先进入一个目录"); return; }
  const vids = browserState.entries.filter((e) => !e.dir && VIDEO_EXT.has(extOf(e.name)));
  if (!vids.length) { toast("该目录没有可识别的视频文件"); return; }
  const n = addPaths(vids.map((e) => e.path));
  toast(`已加入 ${n} 个文件`);
  $("browserModal").classList.add("hidden");
}

/* --------------------------------------------------------------- plan 预览 */

async function makePreview() {
  const entry = state.files.find((f) => f.path === state.activeFile);
  if (!entry) { toast("先选中一个文件"); return; }
  toast("正在生成 6 秒样片…（AV1 档位通常 10-40 秒）", 8000);
  $("previewBtn").disabled = true;
  try {
    const data = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify({
        path: state.activeFile,
        ...fileSelection(entry),
        seconds: 6,
      }),
    });
    openPreview(data.path, null, {
      start: data.start,
      seconds: data.seconds,
      keyframes: data.keyframes,
      gap: data.max_keyframe_gap,
      seekOk: data.seek_ok,
      size: data.size_bytes,
    });
    toast(data.seek_ok ? "样片已生成：拖动进度条试试" : "⚠ 样片关键帧间隔偏大，请检查档位设置", 5000);
  } catch (err) {
    toast("预览失败: " + err.message, 6000);
  } finally {
    $("previewBtn").disabled = false;
  }
}

/* ------------------------------------------------------------------ wiring */

function wire() {
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, () => dz.classList.remove("drag")));

  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    handleDrop(e);
  });

  $("pickBtn").addEventListener("click", async () => {
    if (state.initializing || !state.caps) { toast('编码环境正在准备，请稍后选择文件'); return; }
    // Native dialog: the only way to get a real absolute path.
    if (state.caps.nativeDialogs) {
      const data = await pickDialog("files");
      if (data.cancelled) return;
      if (!data.ok && data.error) { toast(data.error, 8000); return; }
      const n = addPaths(data.paths || [], "选择文件");
      if (!n) toast("没有识别到视频文件（看扩展名）");
      return;
    }
    $("fileInput").click();     // browser fallback: cannot give paths
  });

  $("pickDirBtn").addEventListener("click", async () => {
    if (state.initializing || !state.caps) { toast('编码环境正在准备，请稍后选择文件'); return; }
    if (state.caps.nativeDialogs) {
      const data = await pickDialog("folder");
      if (data.cancelled) return;
      if (!data.ok && data.error) { toast(data.error, 8000); return; }
      if (data.path) await addFolderFromPath(data.path);
      return;
    }
    $("dirInput").click();      // browser fallback (asks for the base path after)
  });

  // ---- inbox ----
  $("inboxOpenBtn").addEventListener("click", () => {
    $("inboxPanel").classList.toggle("hidden");
    if (!$("inboxPanel").classList.contains("hidden")) {
      $("inboxPath").value = state.caps.inboxDir || "";
    }
  });
  $("inboxCloseBtn").addEventListener("click", () => $("inboxPanel").classList.add("hidden"));
  $("inboxRevealBtn").addEventListener("click", async () => {
    const dir = state.caps.inboxDir;
    if (!dir) { toast("未取得收件箱路径"); return; }
    try { await api("/api/reveal", { method: "POST", body: JSON.stringify({ path: dir }) }); }
    catch (err) { toast("无法打开文件夹: " + err.message); }
  });
  $("inboxScanBtn").addEventListener("click", async () => {
    try {
      const data = await api("/api/inbox/scan", { method: "POST", body: "{}" });
      const n = addPaths(data.found || []);
      toast(n ? `已从收件箱加入 ${n} 个文件` : "收件箱里没有新的视频文件");
    } catch (err) { toast("扫描失败: " + err.message); }
  });
  $("inboxClearBtn").addEventListener("click", async () => {
    if (!confirm("确定清空收件箱？\n\n会删除复制进收件箱的文件本身；你拖入时的原文件不受影响。")) return;
    try {
      const data = await api("/api/inbox/clear", { method: "POST", body: JSON.stringify({ confirm: true }) });
      toast(`已清空收件箱（${data.removed} 个文件）`);
    } catch (err) { toast("清空失败: " + err.message); }
  });


  // Folder picker: the browser hides the parent path, so we ask for that ONE
  // folder path and reconstruct every file from its relative path. One paste
  // replaces typing dozens of file paths.
  $("dirInput").addEventListener("change", (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    state.pendingFolderFiles = files;
    $("folderCount").textContent = String(files.length);
    $("folderPanel").classList.remove("hidden");
    $("pathFallback").classList.add("hidden");
    const guessed = state.lastFolderBase || "";
    $("folderBase").value = guessed;
    $("folderBase").focus();
  });

  $("folderCancelBtn").addEventListener("click", () => {
    state.pendingFolderFiles = null;
    $("folderPanel").classList.add("hidden");
  });

  $("serverBrowseBtn").addEventListener("click", () =>
    openBrowser($("folderBase").value.trim() || browserState.path, "folderBase"));

  $("folderAddBtn").addEventListener("click", () => {
    const base = $("folderBase").value.trim().replace(/^"|"$/g, "");
    const files = state.pendingFolderFiles || [];
    if (!base) { toast("请先填写所选文件夹的完整路径"); return; }
    if (!files.length) { toast("没有待加入的文件，请重新选择文件夹"); return; }
    state.lastFolderBase = base;
    const paths = files.map((f) => fileToPath(f, base));
    const n = addPaths(paths);
    toast(n ? `已加入 ${n} 个文件` : "没有识别到视频文件（检查路径或扩展名）");
    if (n) {
      state.pendingFolderFiles = null;
      $("folderPanel").classList.add("hidden");
    }
  });

  $("fileInput").addEventListener("change", (e) => {
    const picked = [];
    for (const f of e.target.files) if (f.path) picked.push(f.path);
    if (picked.length) addPaths(picked);
    else if (e.target.files.length) {
      showPathFallback("浏览器不提供完整路径 —— 请改用「选择文件夹」，"
        + "或把路径粘贴到下面的框里。");
    }
  });

  $("pathAddBtn").addEventListener("click", () => {
    const lines = $("pathText").value.split(/\r?\n/);
    const n = addPaths(lines);
    toast(n ? `已加入 ${n} 个文件` : "没有识别到有效的视频路径");
    if (n) $("pathText").value = "";
  });

  $("serverBrowseBtn").addEventListener("click", () => openBrowser(browserState.path));
  $("browserClose").addEventListener("click", () => $("browserModal").classList.add("hidden"));
  $("browserPick").addEventListener("click", addWholeDir);
  $("browserPath").addEventListener("keydown", (e) => {
    if (e.key === "Enter") openBrowser(e.target.value.trim());
  });

  $("clearFilesBtn").addEventListener("click", () => {
    state.files = [];
    state.activeFile = null;
    renderFiles();
    updateStartButton();
    refreshPlan();
  });

  $("startBtn").addEventListener("click", submitPendingFiles);
  $("queueJumpBtn").addEventListener("click", () => $("queueSection").scrollIntoView({ behavior: "smooth", block: "start" }));

  $("measureBtn").addEventListener("click", measureCost);

  // ---- batch panel (A1 totals / C3 grouping / C1 budget) ----
  if ($("batchEstimateBtn")) $("batchEstimateBtn").addEventListener("click", () => loadBatchEstimate());
  if ($("batchMeasureBtn")) $("batchMeasureBtn").addEventListener("click", measureBatch);
  if ($("budgetApplyBtn")) $("budgetApplyBtn").addEventListener("click", applyBudget);
  if ($("budgetValue")) {
    $("budgetValue").addEventListener("keydown", (event) => {
      if (event.key === "Enter") applyBudget();
    });
  }

  for (const radio of document.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", (e) => {
      if (!e.target.checked) return;
      state.mode = e.target.value;
      state.estimate = null;
      state.repairStrategy = "";
      applyMode();
      refreshPlan();
    });
  }

  $("previewBtn").addEventListener("click", makePreview);

  if ($("cropCheckBtn")) {
    $("cropCheckBtn").addEventListener("click", checkCrop);
  }

  /* ------------------------------------------------ queue control (G5/F12b) --- */

  if ($("pauseAllBtn")) {
    $("pauseAllBtn").addEventListener("click", async () => {
      const running = state.jobs.filter((j) => j.status === "running").length;
      if (!running) { toast("没有正在编码的任务可以暂停"); return; }
      try {
        const data = await api("/api/jobs/pause-all", { method: "POST", body: "{}" });
        const failed = (data.failures || []).length;
        toast(failed
          ? `已暂停 ${data.paused} 个，${failed} 个未能暂停（见任务日志）`
          : `已暂停 ${data.paused} 个任务。要继续请点「▶ 全部继续」`, 8000);
        refreshJobs();
      } catch (err) { toast("暂停失败: " + err.message, 6000); }
    });
  }

  if ($("resumeAllBtn")) {
    $("resumeAllBtn").addEventListener("click", async () => {
      const paused = state.jobs.filter((j) => j.status === "paused").length;
      if (!paused) { toast("没有已暂停的任务"); return; }
      try {
        const data = await api("/api/jobs/resume-all", { method: "POST", body: "{}" });
        const failed = (data.failures || []).length;
        toast(failed
          ? `已继续 ${data.resumed} 个，${failed} 个未能继续`
          : `已继续 ${data.resumed} 个任务`, 7000);
        refreshJobs();
      } catch (err) { toast("继续失败: " + err.message, 6000); }
    });
  }

  if ($("stopAllBtn")) {
    $("stopAllBtn").addEventListener("click", async () => {
      const active = state.jobs.filter((j) => ["running", "verifying", "queued", "paused"].includes(j.status));
      if (!active.length) { toast("没有正在进行的任务"); return; }
      const running = active.filter((j) => j.status !== "queued").length;
      const queued = active.length - running;
      // Two-level stop is the point: this button means "everything", and the
      // per-job 停止 that means "just this one". Saying exactly what will stop keeps
      // them from being confused for each other.
      if (!window.confirm(
        `停止全部？\n\n正在进行 ${running} 个，排队中 ${queued} 个。\n`
        + "排队的也会被取消（不会在你看不见的时候偷偷开始）。\n\n"
        + "只想停当前这一个，请用该任务上的「停止此项」。")) return;
      try {
        const data = await api("/api/jobs/cancel-all", { method: "POST", body: JSON.stringify({ confirm: true }) });
        const s = data.stopped || {};
        toast(`已停止全部：进行中 ${s.running || 0} 个、排队 ${s.queued || 0} 个`, 7000);
        refreshJobs();
      } catch (err) { toast("停止失败: " + err.message, 6000); }
    });
  }

  /* ------------------------------------------------ completion actions (G8) --- */

  const afterIds = ["afterNotify", "afterPower", "afterDelay", "afterOnlyOk"];  afterIds.forEach((id) => {
    if (!$(id)) return;
    $(id).addEventListener("change", async () => {
      syncAfterRows();
      await saveAfterPolicy();
    });
  });
  syncAfterRows();

  $("clearDoneBtn").addEventListener("click", clearSuccessfulRecords);

  $("exportBtn").addEventListener("click", async () => {
    try {
      const data = await api("/api/jobs");
      const rows = [["文件", "档位", "状态", "源体积", "输出体积", "压缩率", "用时(秒)", "平均速度", "编码器", "输出路径", "错误"]];
      for (const j of data.jobs) {
        const ratio = j.src_size && j.total_size ? (j.total_size / j.src_size * 100).toFixed(1) + "%" : "";
        const avg = j.duration && j.elapsed ? (j.duration / j.elapsed).toFixed(2) + "x" : "";
        rows.push([
          j.src_name, j.profile_id, j.status, j.src_size, j.total_size, ratio,
          (j.elapsed || 0).toFixed(1), avg, j.encoder, j.out_path, (j.error || "").replace(/\r?\n/g, " "),
        ]);
      }
      // A spreadsheet treats a cell that starts with = + - @ as a FORMULA. Filenames
      // are user-controlled, so `=cmd|'/c calc'!A1` in a file name becomes an executed
      // formula when the report is opened in Excel. Prefixing with an apostrophe keeps
      // the text visible and inert (the standard CSV-injection mitigation).
      const cell = (c) => {
        let text = String(c ?? "");
        if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
        return `"${text.replace(/"/g, '""')}"`;
      };
      const csv = "\uFEFF" + rows.map((r) => r.map(cell).join(",")).join("\r\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `压缩报告_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { toast("导出失败: " + err.message); }
  });

  $("copyCmdBtn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("cmdText").textContent);
      toast("命令已复制");
    } catch { toast("复制失败，请手动选中"); }
  });

  ["optOutDir", "optTemplate", "optMaxEdge", "optScalePct", "optGop", "optCrf",
   "optAudio", "optSubs", "optContainer", "optDeint", "optDenoise", "optAutocrop",
   "optHdr", "optStrip", "optEffort", "optKeepTime"].forEach((id) => {
    if (!$(id)) return;
    const changed = () => {
      const key = {optMaxEdge:"max_long_edge",optScalePct:"scale_percent",optGop:"gop_seconds",optCrf:"crf",
        optAudio:"audio_mode",optSubs:"subtitle_mode",optContainer:"container",optDeint:"deinterlace",
        optDenoise:"denoise",optAutocrop:"autocrop",optHdr:"hdr_to_sdr",optStrip:"strip_metadata",optEffort:"effort",optKeepTime:"keep_timestamps"}[id];
      if (key) syncManualSeeds([key]);
      refreshPlan();
    };
    $(id).addEventListener("change", changed);
    $(id).addEventListener("input", changed);
  });

  // Loudness normalisation: the rows are hidden until it is switched on, and the
  // command preview must follow immediately -- normalising changes `-af` AND forces an
  // audio re-encode, so a stale preview would show a command that will not be run.
  if ($("optLoudness")) {
    $("optLoudness").addEventListener("change", () => { syncLoudnessRows(); syncManualSeeds(["loudness", "loudness_tp"]); refreshPlan(); });
  }
  if ($("optLoudnessCustom")) {
    $("optLoudnessCustom").addEventListener("input", () => { syncLoudnessRows(); syncManualSeeds(["loudness", "loudness_tp"]); refreshPlan(); });
  }
  if ($("optLoudnessTp")) {
    $("optLoudnessTp").addEventListener("change", () => { syncManualSeeds(["loudness_tp"]); refreshPlan(); });
  }
  syncLoudnessRows();

  // Quality mode: CRF vs target size. Switching clears a stale solved CRF so the
  // wrong value can never leak into a job.
  $("optQualityMode").addEventListener("change", () => {
    state.solvedCrf = null;
    state.targetResult = null;   // the solved answer belongs to the old mode
    applyQualityModeUI();
    refreshPlan();
  });
  $("optTarget").addEventListener("change", () => {
    state.solvedCrf = null;      // the old solution is for the old target
    state.targetResult = null;
    refreshPlan();
  });
  $("targetSolveBtn").addEventListener("click", solveTarget);

  // ---------------------------------------------------- output location ---
  for (const radio of document.querySelectorAll('input[name="location"]')) {
    radio.addEventListener("change", async (e) => {
      if (!e.target.checked) return;
      applyLocationUI();
      // Remember the choice server-side so the next session opens the same way.
      await savePreference({location:e.target.value});
    });
  }

  $("saveDefaultDirBtn").addEventListener("click", () => saveDefaultDir(false));
  $("optDefaultDir").addEventListener("change", () => saveDefaultDir(true));
  $("pickDefaultDirBtn").addEventListener("click", () =>
    openBrowser($("optDefaultDir").value.trim(), "optDefaultDir"));
  $("browseCustomDirBtn").addEventListener("click", () =>
    openBrowser($("optOutDir").value.trim(), "optOutDir"));

  $("expertMode").addEventListener("change", (e) => {
    if (e.target.checked) {
      toast("专家模式：将放宽关键帧安全约束，输出可能无法自由拖动进度条", 5000);
    }
    syncManualSeeds(["allow_unsafe"]);
    refreshPlan();
  });

  $("themeBtn").addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    localStorage.setItem("vcs-theme", next);
  });
  const savedTheme = localStorage.getItem("vcs-theme");
  if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);

  // Poll as a safety net in case SSE is unavailable (e.g. a strict proxy).
  setInterval(() => { if (!window.EventSource) refreshJobs(); }, 3000);

  bindHistory();
  // Lazy: the archive is only read once the panel is actually opened, so startup
  // keeps its current cost no matter how many records have piled up.
  if ($("historyPanel")) {
    $("historyPanel").addEventListener("toggle", () => {
      if ($("historyPanel").open) loadHistory();
    });
  } else {
    loadHistory();
  }
}

wire();
boot();
