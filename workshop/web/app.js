const DEMO = `雨夜，末班火车即将进站。成年男性沈川在雨棚下等了很久，旧帆布包里装着一支银色录音笔——里面是当年未能说出口的话。成年女性林岚快步走来，米色大衣，红色围巾被风吹起。多年误会之后，他们在湿润站台上重新见面。

沈川先看见林岚的围巾，再看见她的眼睛，犹豫后从包里取出录音笔交给她。林岚按下播放，听完后眉心展开，轻轻点头。列车灯光从两人身后掠过。沈川侧身让出路，两人步伐逐渐同步，并肩离开站台，走入暖色灯光。

对白只有一句：你终于来了。
不要路人，不要拥抱，不要哭喊。`;

const SPLIT_DEMO = `主要人物：沈川 / 林岚
沈川，三十出头，旧帆布包，银色录音笔。林岚，米色大衣，红色围巾。

【第1集：末班站台】
雨夜，末班火车即将进站。沈川在雨棚下等了很久。林岚快步走来，围巾被风吹起。两人隔着雨水对视，谁也没先开口。
对白：你怎么还在这。
【切黑】

【第2集：那支录音笔】
沈川从包里取出银色录音笔，递到林岚手里。林岚按下播放，听完后眉心展开，轻轻点头。列车灯光从两人身后掠过。
对白：我一直想给你听。
【切黑】

【第3集：并肩离开】
沈川侧身让出路。林岚把录音笔收回大衣口袋。两人步伐逐渐同步，并肩离开站台，走入暖色灯光。
对白：走吧。`;

const LS_SERIES = "chaifen.seriesId";
const LS_EPISODE = "chaifen.episodeId";

const $ = (id) => document.getElementById(id);
const state = {
  project: null,
  timer: null,
  source: null,
  series: [],
  seriesId: localStorage.getItem(LS_SERIES) || "",
  episodeId: localStorage.getItem(LS_EPISODE) || "",
  composing: false,
  holdStart: false,
  promptDrafts: {},
  compileTimer: null,
  episodeCache: {},
  imageVersions: {},
  renderCtx: {
    projectId: "",
    shotsKey: "",
    picturesLayout: "",
    logLen: 0,
    activeTab: "shots",
    sidebarKey: "",
    editKey: "",
    compileKey: "",
    clipsMetaKey: "",
    clipsLayoutKey: "",
  },
  sidebarBound: false,
  scriptCollapsed: localStorage.getItem("chaifen.scriptCollapsed") === "1",
  renderQueued: false,
  pendingProject: null,
  episodeFetchCtrl: null,
};

const STAGE_LABEL = {
  queued: "排队",
  analyzing: "拆解剧本",
  storyboard: "生成分镜",
  imaging: "生成参考图",
  prompting: "撰写提示词",
  generating_video: "出片中",
  editing: "自动剪辑",
  done: "完成",
  error: "失败",
  retry: "重出单图",
  unknown: "未知",
  missing: "缺失",
};

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".tab-body").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    $("tab-" + btn.dataset.tab).classList.add("on");
    state.renderCtx.activeTab = btn.dataset.tab;
    if (state.project) renderTabPanel(state.project, btn.dataset.tab, { force: true });
  });
});

function activeTab() {
  return state.renderCtx.activeTab || "shots";
}

function sidebarLayoutKey(series = state.series) {
  return series
    .map((s) => {
      const eps = (s.episodes || []).map((e) => `${e.id}:${e.status}:${e.label || ""}`).join(",");
      return `${s.id}|${s.name}|${eps}`;
    })
    .join(";;");
}

function bindSidebarOnce() {
  if (state.sidebarBound) return;
  state.sidebarBound = true;
  const list = $("project-list");
  list.addEventListener("click", (ev) => {
    const delEp = ev.target.closest("[data-del-ep]");
    if (delEp) {
      ev.stopPropagation();
      deleteEpisode(delEp.dataset.delEp);
      return;
    }
    const delSeries = ev.target.closest("[data-del-series]");
    if (delSeries) {
      ev.stopPropagation();
      deleteSeries(delSeries.dataset.delSeries);
      return;
    }
    const addEp = ev.target.closest("[data-add-ep]");
    if (addEp) {
      ev.stopPropagation();
      state.seriesId = addEp.dataset.addEp;
      void composeEpisode();
      return;
    }
    const epBtn = ev.target.closest("[data-episode]");
    if (epBtn) {
      openEpisode(epBtn.dataset.episode);
      return;
    }
    const seriesBtn = ev.target.closest("[data-series]");
    if (seriesBtn) selectSeries(seriesBtn.dataset.series);
  });
  list.addEventListener(
    "mouseover",
    (ev) => {
      const btn = ev.target.closest("[data-episode]");
      if (!btn) return;
      const id = btn.dataset.episode;
      if (state.episodeCache[id] || id === state.episodeId) return;
      fetch(`/api/projects/${id}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (data) state.episodeCache[id] = data;
        })
        .catch(() => {});
    },
    { passive: true }
  );
}

function syncScriptPanelLayout() {
  const workspace = $("workspace");
  const expand = $("expand-script");
  if (!workspace) return;
  workspace.classList.toggle("script-collapsed", state.scriptCollapsed);
  if (expand) expand.classList.toggle("hidden", !state.scriptCollapsed);
  localStorage.setItem("chaifen.scriptCollapsed", state.scriptCollapsed ? "1" : "0");
}

function updatePipeline(p) {
  const pipe = $("pipeline");
  if (!pipe || !p) return;
  const busyStages = {
    queued: "script",
    analyzing: "storyboard",
    storyboard: "storyboard",
    imaging: "pictures",
    prompting: "prompt",
    generating_video: "videos",
    editing: "videos",
  };
  let current = busyStages[p.status] || "";
  if (p.status === "imaging" && !picturesPanelBusy(p)) {
    current = (p.clips || []).some((c) => c.h3_prompt || c.seedance_prompt) ? "" : "prompt";
  }
  const hasScript = !!(p.script || $("script")?.value?.trim());
  const hasStory = !!p.analysis;
  const pics = p.pictures || [];
  const picsReady = pics.some((x) => x.status === "ready");
  const hasPrompt = (p.clips || []).some((c) => c.h3_prompt || c.seedance_prompt);
  const hasVideo = (p.clips || []).some((c) => c.video_filename);
  const doneMap = {
    script: hasScript,
    storyboard: hasStory,
    pictures: picsReady,
    prompt: hasPrompt,
    videos: hasVideo || p.status === "done",
  };
  pipe.querySelectorAll("li").forEach((li) => {
    const step = li.dataset.step;
    li.classList.remove("done", "active", "busy");
    if (doneMap[step]) li.classList.add("done");
    if (step === current && p.status !== "done" && p.status !== "error") li.classList.add("busy");
    else if (step === current) li.classList.add("active");
  });
}

function queueRender(p, opts = {}) {
  state.pendingProject = { p, opts };
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    const job = state.pendingProject;
    state.pendingProject = null;
    if (job) renderNow(job.p, job.opts);
  });
}

$("new-project").addEventListener("click", async () => {
  if (await createProject()) await composeEpisode();
});
$("fill-demo").addEventListener("click", fillDemo);
$("start").addEventListener("click", start);
$("estimate-duration").addEventListener("click", estimateDuration);
$("duration-auto").addEventListener("change", syncDurationMode);
$("split-series").addEventListener("change", syncSplitMode);
$("script").addEventListener("input", syncSplitMode);
$("copy-prompt").addEventListener("click", async () => {
  const text = $("prompt").innerText;
  await navigator.clipboard.writeText(text);
  $("copy-prompt").textContent = "已复制";
  setTimeout(() => ($("copy-prompt").textContent = "复制全部提示词"), 1500);
});
$("generate-all").addEventListener("click", () => requestVideos());
$("auto-edit").addEventListener("click", () => requestEdit());
$("compile-series").addEventListener("click", requestCompile);
$("video-provider")?.addEventListener("change", () => {
  syncSeedanceModelVisibility();
  syncResolutionOptions();
  if (state.project) {
    state.project.video_provider = $("video-provider").value === "seedance" ? "seedance" : "minimax";
    state.renderCtx.clipsLayoutKey = "";
    const tab = activeTab();
    renderTabPanel(state.project, "prompt", { force: true });
    renderTabPanel(state.project, "videos", { force: true });
    if (tab !== "prompt" && tab !== "videos") {
      $("prompt").textContent =
        episodePromptText(state.project) || state.project.video_prompt || $("prompt").textContent;
    }
  }
});
$("seedance-model")?.addEventListener("change", () => {
  syncResolutionOptions();
  state.renderCtx.clipsLayoutKey = "";
  if (state.project && activeTab() === "videos") {
    renderTabPanel(state.project, "videos", { force: true });
  }
});
$("resolution")?.addEventListener("change", () => syncResolutionOptions($("resolution").value));
$("video-resolution")?.addEventListener("change", () => syncResolutionOptions($("video-resolution").value));
$("collapse-script")?.addEventListener("click", () => {
  state.scriptCollapsed = true;
  syncScriptPanelLayout();
});
$("expand-script")?.addEventListener("click", () => {
  state.scriptCollapsed = false;
  syncScriptPanelLayout();
});

const SEEDANCE_MODEL_LABELS = {
  "seedance-2": "标准版",
  "seedance-2-fast": "Fast",
  "seedance-2-mini": "Mini",
  "doubao-seedance-2-0-260128": "标准版",
  "doubao-seedance-2-0-fast-260128": "Fast",
  "doubao-seedance-2-0-mini-260615": "Mini",
};

function mapSeedanceModel(model) {
  const aliases = {
    "doubao-seedance-2-0-260128": "seedance-2",
    "doubao-seedance-2-0-fast-260128": "seedance-2-fast",
    "doubao-seedance-2-0-mini-260615": "seedance-2-mini",
    "dreamina-seedance-2.0": "seedance-2",
    "dreamina-seedance-2.0-fast": "seedance-2-fast",
    "dreamina-seedance-2.5": "seedance-2",
  };
  return aliases[model] || model || "seedance-2-fast";
}

function seedanceModelLabel(model) {
  return SEEDANCE_MODEL_LABELS[mapSeedanceModel(model)] || SEEDANCE_MODEL_LABELS[model] || "Fast";
}

function syncSeedanceModelVisibility() {
  const wrap = $("seedance-model-wrap");
  if (!wrap) return;
  wrap.style.display = $("video-provider")?.value === "seedance" ? "" : "none";
  const wipe = $("edit-wipe");
  if (wipe) wipe.checked = $("video-provider")?.value !== "seedance";
}

const RESOLUTION_BY_PROVIDER = {
  minimax: [
    ["768P", "768P"],
    ["1080P", "1080P"],
    ["2K", "2K"],
  ],
  seedance: [
    ["480P", "480P 省钱"],
    ["720P", "720P"],
    ["1080P", "1080P"],
  ],
};

function selectedVideoProvider() {
  return $("video-provider")?.value === "seedance" ? "seedance" : "minimax";
}

function mapResolutionForProvider(provider, value) {
  const v = String(value || "").toUpperCase();
  if (provider === "seedance") {
    if (v === "480P" || v === "480") return "480P";
    if (v === "720P") return "720P";
    if (v === "1080P") return "1080P";
    if (v === "2K") return "1080P";
    return "480P";
  }
  if (v === "1080P") return "1080P";
  if (v === "2K") return "2K";
  return "768P";
}

function selectedResolution() {
  return $("video-resolution")?.value || $("resolution")?.value || "";
}

function syncResolutionOptions(preferred) {
  const provider = selectedVideoProvider();
  let options = RESOLUTION_BY_PROVIDER[provider];
  if (provider === "seedance" && ($("seedance-model")?.value === "seedance-2-fast" || $("seedance-model")?.value === "seedance-2-mini")) {
    options = options.filter(([value]) => value !== "1080P");
  }
  const mapped = mapResolutionForProvider(provider, preferred || selectedResolution());
  const next = options.some(([value]) => value === mapped)
    ? mapped
    : options.find(([value]) => value === "720P")?.[0] || options[0][0];
  for (const id of ["resolution", "video-resolution"]) {
    const el = $(id);
    if (!el) continue;
    const html = options.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
    if (el.innerHTML !== html) el.innerHTML = html;
    el.value = next;
  }
  if (state.project) state.project.video_resolution = next;
}

function currentSeries() {
  return state.series.find((s) => s.id === state.seriesId) || null;
}

function persist() {
  if (state.seriesId) localStorage.setItem(LS_SERIES, state.seriesId);
  else localStorage.removeItem(LS_SERIES);
  if (state.episodeId) localStorage.setItem(LS_EPISODE, state.episodeId);
  else localStorage.removeItem(LS_EPISODE);
  const hash = [];
  if (state.seriesId) hash.push("s=" + state.seriesId);
  if (state.episodeId) hash.push("e=" + state.episodeId);
  history.replaceState(null, "", hash.length ? "#" + hash.join("&") : location.pathname);
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  if (!raw) return;
  const params = new URLSearchParams(raw.replace(/&/g, "&"));
  const s = params.get("s");
  const e = params.get("e");
  if (s) state.seriesId = s;
  if (e) state.episodeId = e;
}

function updateContext() {
  const series = currentSeries();
  if (!series) {
    $("context-line").textContent = "请先在左侧新建或选择一个项目。项目里面再写第一集、第二集。";
    $("form-title").textContent = "本集剧本";
    return;
  }
  const eps = series.episodes || [];
  const idx = eps.findIndex((ep) => ep.id === state.episodeId);
  const label = state.composing
    ? episodeLabel(eps.length + 1)
    : idx >= 0
      ? eps[idx].label || episodeLabel(eps[idx].index || idx + 1)
      : "本集剧本";
  const splitOn = $("split-series")?.checked;
  $("form-title").textContent = state.composing && splitOn ? "全本剧本" : label;
  $("context-line").textContent = state.composing
    ? splitOn
      ? `《${series.name}》。一次贴完整剧本，将建成第一集、第二集…，按顺序出片，设定图沿用。`
      : `《${series.name}》· ${label}。已有角色和道具会沿用，不会覆盖前面的剧集。`
    : `《${series.name}》· ${label}。点项目下的第一集、第二集可回看。`;
}

function episodeLabel(n) {
  const chars = "一二三四五六七八九十";
  const i = Number(n) || 1;
  if (i <= 10) return `第${chars[i - 1]}集`;
  if (i < 20) return `第十${chars[i - 11]}集`;
  const tens = Math.floor(i / 10);
  const ones = i % 10;
  return `第${chars[tens - 1]}十${ones ? chars[ones - 1] : ""}集`;
}

function patchSidebarActive() {
  document.querySelectorAll("[data-episode]").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.episode === state.episodeId && !state.composing);
  });
}

function renderSidebar() {
  bindSidebarOnce();
  const list = $("project-list");
  if (!state.series.length) {
    list.innerHTML = `<div class="side-empty">还没有项目。先新建一个，再在项目里写第一集。</div>`;
    state.renderCtx.sidebarKey = "";
    renderRoster();
    updateContext();
    return;
  }
  list.innerHTML = state.series
    .map((s) => {
      const open = s.id === state.seriesId;
      const eps = s.episodes || [];
      const epHtml = open
        ? `<div class="tree-eps">
            ${
              eps.length
                ? eps
                    .map((ep, i) => {
                      const n = ep.index || i + 1;
                      const label = ep.label || episodeLabel(n);
                      const sub = ep.title && ep.title !== label ? ep.title : "";
                      const on = ep.id === state.episodeId && !state.composing;
                      return `<div class="side-row">
                      <button type="button" class="side-item ep${on ? " on" : ""}" data-episode="${ep.id}">
                        ${escapeHtml(label)}
                        <small>${sub ? escapeHtml(sub) + " · " : ""}${STAGE_LABEL[ep.status] || ep.status}</small>
                      </button>
                      <button type="button" class="side-del" data-del-ep="${ep.id}" title="删除这一集">×</button>
                    </div>`;
                    })
                    .join("")
                : `<div class="side-empty">还没有剧集</div>`
            }
            <button type="button" class="side-item add-ep" data-add-ep="${s.id}">＋ 新建剧集</button>
          </div>`
        : "";
      return `<div class="tree-project">
        <div class="side-row">
          <button type="button" class="side-item${open ? " on" : ""}" data-series="${s.id}">
            ${escapeHtml(s.name)}
            <small>${eps.length} 集 · ${(s.assets || []).length} 张设定</small>
          </button>
          <button type="button" class="side-del" data-del-series="${s.id}" title="删除整个项目">×</button>
        </div>
        ${epHtml}
      </div>`;
    })
    .join("");
  state.renderCtx.sidebarKey = sidebarLayoutKey();
  renderRoster();
  updateContext();
}

function patchSidebarStatus() {
  bindSidebarOnce();
  document.querySelectorAll("[data-episode]").forEach((btn) => {
    const id = btn.dataset.episode;
    const ep = currentSeries()?.episodes?.find((e) => e.id === id);
    if (!ep) return;
    const on = id === state.episodeId && !state.composing;
    btn.classList.toggle("on", on);
    const small = btn.querySelector("small");
    if (small) {
      const n = ep.index || 0;
      const label = ep.label || episodeLabel(n);
      const sub = ep.title && ep.title !== label ? ep.title : "";
      small.textContent = `${sub ? sub + " · " : ""}${STAGE_LABEL[ep.status] || ep.status}`;
    }
  });
  document.querySelectorAll("[data-series]").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.series === state.seriesId);
  });
}

function renderRoster() {
  const box = $("series-roster");
  const series = currentSeries();
  if (!series || !series.assets || !series.assets.length) {
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  box.classList.remove("hidden");
  const groups = { character: "角色", scene: "场景", prop: "道具" };
  const parts = Object.entries(groups)
    .map(([kind, label]) => {
      const names = series.assets.filter((a) => a.kind === kind).map((a) => a.name);
      return names.length ? `${label}：${names.join("、")}` : "";
    })
    .filter(Boolean);
  box.innerHTML = `本剧已锁定，新剧集只出增量图。<br/>${parts.join("<br/>")}`;
}

async function loadSeries(keepEpisode = true) {
  const r = await fetch("/api/series");
  const next = await r.json();
  const key = sidebarLayoutKey(next);
  state.series = next;
  if (state.seriesId && !currentSeries() && state.series.length) {
    state.seriesId = state.series[0].id;
  }
  if (!keepEpisode) state.episodeId = "";
  persist();
  if (key === state.renderCtx.sidebarKey) patchSidebarStatus();
  else renderSidebar();
  renderCompilePlayer();
}

async function openNewProjectModal(defaultName = "") {
  const modal = $("new-project-modal");
  const input = $("new-project-name");
  const confirmBtn = $("new-project-confirm");
  const cancelBtn = $("new-project-cancel");
  const dialog = modal?.querySelector(".modal");
  if (!modal || !input || !confirmBtn || !cancelBtn) return null;

  input.value = defaultName || "未命名项目";
  confirmBtn.disabled = false;
  confirmBtn.textContent = "创建";
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  input.focus();
  input.select();

  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      document.removeEventListener("keydown", onKey);
      confirmBtn.onclick = null;
      cancelBtn.onclick = null;
      modal.onclick = null;
      if (dialog) dialog.onclick = null;
    };
    const finish = (result, hide = true) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (hide) {
        modal.classList.add("hidden");
        modal.setAttribute("aria-hidden", "true");
        confirmBtn.disabled = false;
        confirmBtn.textContent = "创建";
      }
      resolve(result);
    };

    const onKey = (e) => {
      if (e.key === "Escape") finish(null);
      if (e.key === "Enter") {
        e.preventDefault();
        const name = input.value.trim();
        if (name) finish(name, false);
      }
    };

    confirmBtn.onclick = (e) => {
      e.stopPropagation();
      const name = input.value.trim();
      if (!name) {
        input.focus();
        return;
      }
      finish(name, false);
    };
    cancelBtn.onclick = (e) => {
      e.stopPropagation();
      finish(null);
    };
    modal.onclick = (e) => {
      if (e.target === modal) finish(null);
    };
    if (dialog) {
      dialog.onclick = (e) => e.stopPropagation();
    }
    document.addEventListener("keydown", onKey);
  });
}

function closeNewProjectModal() {
  const modal = $("new-project-modal");
  const confirmBtn = $("new-project-confirm");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  if (confirmBtn) {
    confirmBtn.disabled = false;
    confirmBtn.textContent = "创建";
  }
}

async function createProject() {
  const name = await openNewProjectModal("未命名项目");
  if (!name) return false;

  const confirmBtn = $("new-project-confirm");
  confirmBtn.disabled = true;
  confirmBtn.textContent = "创建中…";
  try {
    const r = await fetch("/api/series", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!r.ok) {
      alert(await r.text());
      confirmBtn.disabled = false;
      confirmBtn.textContent = "创建";
      return false;
    }
    const series = await r.json();
    state.seriesId = series.id;
    await loadSeries(false);
    closeNewProjectModal();
    return true;
  } catch (err) {
    alert("创建失败，请稍后重试");
    confirmBtn.disabled = false;
    confirmBtn.textContent = "创建";
    return false;
  }
}

async function composeEpisode() {
  if (!currentSeries()) {
    const ok = await createProject();
    if (!ok || !currentSeries()) return;
  }
  stopWatch();
  state.composing = true;
  state.episodeId = "";
  state.project = null;
  persist();
  $("script").value = "";
  $("episode-title").value = "";
  $("notes").value = "";
  $("duration-auto").checked = true;
  delete $("duration-hint").dataset.locked;
  $("duration-hint").textContent = "拆解时按节拍估算，一拍约 8–15 秒。也可点「先估一估」。";
  syncDurationMode();
  $("progress-wrap").classList.add("hidden");
  clearResults();
  renderSidebar();
  syncSplitMode();
}

function nextEpisodeTitle() {
  return episodeLabel((currentSeries()?.episodes || []).length + 1);
}

async function deleteEpisode(id) {
  const series = currentSeries();
  const ep = series?.episodes?.find((item) => item.id === id);
  const label = ep
    ? `${ep.label || "这一集"}${ep.title ? " · " + ep.title : ""}`
    : "这一集";
  if (!confirm(`确定删除「${label}」？成片、参考图和提示词会一起删掉，无法恢复。`)) return;
  stopWatch();
  const r = await fetch(`/api/projects/${id}`, { method: "DELETE" });
  if (!r.ok) {
    alert("删除失败");
    return;
  }
  delete state.episodeCache[id];
  const wasCurrent = state.episodeId === id;
  await loadSeries(true);
  if (wasCurrent) {
    const eps = currentSeries()?.episodes || [];
    if (eps.length) await openEpisode(eps[0].id);
    else await composeEpisode();
  } else {
    renderSidebar();
  }
}

async function deleteSeries(id) {
  const series = state.series.find((s) => s.id === id);
  const name = series?.name || "这个项目";
  if (!confirm(`确定删除项目「${name}」以及里面的全部剧集？无法恢复。`)) return;
  if (state.seriesId === id) stopWatch();
  const r = await fetch(`/api/series/${id}`, { method: "DELETE" });
  if (!r.ok) {
    alert("删除失败");
    return;
  }
  const wasCurrent = state.seriesId === id;
  for (const ep of series?.episodes || []) delete state.episodeCache[ep.id];
  state.series = state.series.filter((s) => s.id !== id);
  if (wasCurrent) {
    state.episodeCache = {};
    state.seriesId = state.series[0]?.id || "";
    state.episodeId = "";
  }
  persist();
  await loadSeries(true);
  if (wasCurrent) {
    if (currentSeries()?.episodes?.length) await openEpisode(currentSeries().episodes[0].id);
    else if (currentSeries()) await composeEpisode();
    else {
      state.episodeId = "";
      persist();
      renderSidebar();
      clearResults();
      $("progress-wrap").classList.add("hidden");
    }
  } else {
    renderSidebar();
  }
}

async function selectSeries(id) {
  state.seriesId = id;
  const series = currentSeries();
  const eps = series?.episodes || [];
  const still = eps.find((ep) => ep.id === state.episodeId);
  persist();
  renderSidebar();
  if (still) await openEpisode(still.id);
  else if (eps.length) await openEpisode(eps[0].id);
  else await composeEpisode();
}

async function openEpisode(id) {
  const prevId = state.episodeId;
  const sameEpisode = id === prevId && state.project?.id === id && !state.composing;
  if (!sameEpisode) {
    stopWatch();
    state.composing = false;
    state.episodeId = id;
    persist();
    patchSidebarActive();
    $("download-zip").href = `/api/projects/${id}/export.zip`;
    $("progress-wrap").classList.remove("hidden");

    const cached = state.episodeCache[id];
    if (cached) {
      fillForm(cached, { deferScript: true });
      render(cached, { full: prevId !== id });
    } else {
      $("progress-stage").textContent = "加载中…";
      $("progress-msg").textContent = "";
      clearResults();
    }
  }

  state.episodeFetchCtrl?.abort();
  state.episodeFetchCtrl = new AbortController();
  const signal = state.episodeFetchCtrl.signal;
  try {
    const r = await fetch(`/api/projects/${id}`, { signal });
    if (!r.ok) return;
    const data = await r.json();
    state.episodeCache[id] = data;
    if (state.episodeId !== id) return;
    fillForm(data);
    render(data, { full: true });
    if (episodeBusy(data) || data.running) watch(id);
  } catch (err) {
    if (err && err.name === "AbortError") return;
  }
}

function countEpisodeMarks(script) {
  const re = /^[ \t]*[#＃]*[ \t]*[【\[]?[ \t]*第[ \t]*[0-9一二三四五六七八九十两零〇百]+[ \t]*集/gm;
  return (script.match(re) || []).length;
}

function syncSplitMode() {
  const split = $("split-series").checked;
  const marks = countEpisodeMarks($("script").value || "");
  const hint = $("split-hint");
  $("episode-title").disabled = split && state.composing;
  $("start").textContent = split || marks >= 2 ? "拆集并生成" : "生成本集分镜";
  if (split && state.composing) $("duration-auto").checked = true;
  if (split && state.composing) {
    $("form-title").textContent = "全本剧本";
    hint.textContent =
      marks >= 2
        ? `已识别 ${marks} 个「第N集」标记，将按顺序建成剧集，每集自己估秒再出片。`
        : "没有集标记时，会按剧情拆成多集。每集自己估秒数，必须按顺序生成，后面的集才能沿用设定图。";
  } else if (marks >= 2) {
    hint.textContent = `剧本里有 ${marks} 个「第N集」标记，即使未勾选也会按集拆开。`;
  } else {
    hint.textContent =
      "有「第1集」「第2集」标记时会自动按集拆开；勾选后没有标记也会按剧情拆。每集自己估秒数，按顺序出片。";
  }
  syncDurationMode();
  if (state.composing) updateContext();
}

function syncDurationMode() {
  const auto = $("duration-auto").checked || $("split-series").checked;
  $("duration").disabled = auto;
  if (auto && !$("duration-hint").dataset.locked) {
    $("duration-hint").textContent = $("split-series").checked
      ? "拆集后每一集单独按节拍估秒，再切成 4–15 秒出片。"
      : "拆解时按节拍估算，一拍约 8–15 秒。也可点「先估一估」。";
  }
}

async function estimateDuration() {
  const script = $("script").value.trim();
  if (script.length < 8) {
    alert("请先输入剧本");
    return;
  }
  const r = await fetch("/api/duration-estimate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ script }),
  });
  if (!r.ok) {
    alert(await r.text());
    return;
  }
  const data = await r.json();
  $("duration").value = data.seconds;
  $("duration-hint").textContent = data.reason || "";
  $("duration-hint").dataset.locked = "1";
}

function fillForm(data, opts = {}) {
  const script = data.script || "";
  if (opts.deferScript) {
    requestAnimationFrame(() => {
      if (state.episodeId === data.id) $("script").value = script;
    });
  } else {
    $("script").value = script;
  }
  $("duration").value = data.duration_seconds || 30;
  $("duration-auto").checked = data.duration_auto !== false;
  $("duration-hint").textContent = data.duration_reason || (data.analysis && data.analysis.duration_reason) || "拆解时按节拍估算，一拍约 8–15 秒。";
  if (data.duration_reason || (data.analysis && data.analysis.duration_reason)) {
    $("duration-hint").dataset.locked = "1";
  } else {
    delete $("duration-hint").dataset.locked;
  }
  syncDurationMode();
  if (data.style) $("style").value = data.style;
  let title = data.episode_title || "";
  title = title.replace(/^第\s*[0-9一二三四五六七八九十]+\s*集\s*[·\-—:：]?\s*/, "");
  $("episode-title").value = title;
  $("notes").value = data.extra_notes || "";
  $("skip-images").checked = !!data.skip_images;
  $("high-quality").checked = !!data.high_quality;
  $("auto-video").checked = data.auto_generate_video !== false;
  if (data.video_ratio) $("ratio").value = data.video_ratio;
  if ($("video-provider")) {
    $("video-provider").value = data.video_provider === "seedance" ? "seedance" : "minimax";
  }
  if ($("seedance-model")) {
    const model = mapSeedanceModel(data.seedance_model || "seedance-2-fast");
    if ([...$("seedance-model").options].some((o) => o.value === model)) {
      $("seedance-model").value = model;
    }
  }
  syncSeedanceModelVisibility();
  syncResolutionOptions(data.video_resolution || $("resolution")?.value);
  syncSplitMode();
}

function clearResults() {
  $("shots-empty").style.display = "";
  $("shots-meta").innerHTML = "";
  $("shots").innerHTML = "";
  $("pictures-empty").style.display = "";
  $("pictures-meta").innerHTML = "";
  $("pictures").innerHTML = "";
  $("prompt").textContent = "打开一集即可看到已保存的分段 H3 提示词。";
  $("videos-empty").style.display = "";
  $("videos-meta").innerHTML = "";
  $("clips").innerHTML = "";
  $("log").innerHTML = "";
  state.renderCtx = {
    projectId: "",
    shotsKey: "",
    picturesLayout: "",
    logLen: 0,
    activeTab: state.renderCtx.activeTab || "shots",
    sidebarKey: state.renderCtx.sidebarKey || "",
    editKey: "",
    compileKey: "",
    clipsMetaKey: "",
  };
}

async function fillDemo() {
  if (!currentSeries()) {
    const r = await fetch("/api/series", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "雨夜重逢" }),
    });
    const series = await r.json();
    state.seriesId = series.id;
    await loadSeries(false);
  }
  await composeEpisode();
  $("duration-auto").checked = true;
  delete $("duration-hint").dataset.locked;
  $("duration-hint").textContent = "拆解时按节拍估算，一拍约 8–15 秒。也可点「先估一估」。";
  $("style").value = "竖屏短剧 · 电影级写实";
  $("ratio").value = "9:16";
  $("auto-video").checked = true;
  $("script").value = $("split-series").checked ? SPLIT_DEMO : DEMO;
  $("episode-title").value = $("split-series").checked ? "" : "末班站台";
  $("notes").value = "按本集剧本拆事件弧，不要套固定模板。表演写到微表情和呼吸。";
  syncSplitMode();
  syncResolutionOptions(selectedVideoProvider() === "seedance" ? "480P" : "768P");
}

async function start() {
  if (!currentSeries()) {
    await createProject();
    if (!currentSeries()) return;
  }
  const script = $("script").value.trim();
  if (script.length < 8) {
    alert("请先输入剧本");
    return;
  }
  const current = state.project;
  const staleIdle =
    current &&
    !current.running &&
    (current.status === "error" || ["queued", "analyzing"].includes(current.status));
  if (state.episodeId && current?.id === state.episodeId && staleIdle) {
    if (!confirm("重新拆解本集？会用当前剧本覆盖现有结果。")) return;
    $("start").disabled = true;
    $("progress-wrap").classList.remove("hidden");
    delete state.episodeCache[state.episodeId];
    const resp = await fetch(`/api/projects/${state.episodeId}/retry`, { method: "POST" });
    if (!resp.ok) {
      alert(await resp.text());
      $("start").disabled = false;
      return;
    }
    watch(state.episodeId);
    return;
  }
  const marks = countEpisodeMarks(script);
  const splitFull = $("split-series").checked;
  const shouldSplit = splitFull || marks >= 2;
  $("start").disabled = true;
  $("progress-wrap").classList.remove("hidden");
  $("log").innerHTML = "";
  const payload = {
    script,
    duration_seconds: Number($("duration").value || 30),
    duration_auto: shouldSplit ? true : $("duration-auto").checked,
    style: $("style").value,
    extra_notes: $("notes").value,
    skip_images: $("skip-images").checked,
    high_quality: $("high-quality").checked,
    auto_generate_video: $("auto-video").checked,
    video_resolution: selectedResolution() || $("resolution").value,
    video_ratio: $("ratio").value,
    series_id: state.seriesId,
    series_name: currentSeries()?.name || "",
    episode_title: $("episode-title").value.trim(),
    allow_llm: splitFull && marks < 2,
  };
  try {
    if (shouldSplit) {
      const resp = await fetch("/api/series/split", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      const episodes = data.episodes || [];
      if (!episodes.length) throw new Error("没有拆出剧集");
      state.composing = false;
      state.holdStart = false;
      $("start").disabled = false;
      await loadSeries(true);
      for (let i = 0; i < episodes.length; i += 1) {
        const ep = episodes[i];
        state.episodeId = ep.id;
        persist();
        renderSidebar();
        watch(ep.id);
        await waitUntilIdle(ep.id);
        await loadSeries(true);
        const snap = await fetch(`/api/projects/${ep.id}`);
        if (snap.ok) {
          const data = await snap.json();
          if (data.status === "error") {
            appendLog("error", `${ep.episode_title || "这一集"}失败，后面的集仍会在后台继续。按钮已解开，可改完再点。`);
            break;
          }
        }
      }
      return;
    }
    const resp = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const project = await resp.json();
    state.composing = false;
    state.episodeId = project.id;
    persist();
    watch(project.id);
  } catch (err) {
    appendLog("error", String(err));
    state.holdStart = false;
    $("start").disabled = false;
  }
}

function episodeBusy(data) {
  if (data.running) return true;
  if (data.edit_status === "running" || data.status === "editing") return true;
  const pics = data.pictures || [];
  const busyPics = pics.some((pic) => pic.status === "generating");
  const busyClips = (data.clips || []).some((c) =>
    ["submitting", "queued", "running"].includes(c.video_status)
  );
  return busyPics || busyClips;
}

function panelStatusMessage(p) {
  if (p.status === "error") {
    const msg = p.message || p.error || "生成失败";
    return `本集分镜未生成：${msg}。请点「重新生成本集分镜」。`;
  }
  if (episodeBusy(p) && !p.analysis) {
    return "本集正在生成中，请稍候…";
  }
  if (!p.analysis) {
    return "本集还没有分镜。选中左侧这一集后，点「生成本集分镜」。";
  }
  return "本集还没有分镜。选中左侧这一集后，点「生成本集分镜」。";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitUntilIdle(id) {
  while (true) {
    try {
      const r = await fetch(`/api/projects/${id}`);
      if (!r.ok) {
        await sleep(2500);
        continue;
      }
      const data = await r.json();
      if (state.episodeId === id) {
        state.episodeCache[id] = data;
        render(data, { full: false });
      }
      if (data.status === "error") return;
      if (!episodeBusy(data)) return;
    } catch (_) {}
    await sleep(2500);
  }
}

function stopWatch() {
  if (state.source) state.source.close();
  if (state.timer) clearInterval(state.timer);
  state.source = null;
  state.timer = null;
}

function watch(id) {
  stopWatch();
  $("download-zip").href = `/api/projects/${id}/export.zip`;

  const apply = (data) => {
    state.episodeCache[id] = data;
    if (state.episodeId !== id) return;
    state.project = data;
    queueRender(data, { full: false });
    if (data.status === "error") {
      state.holdStart = false;
      $("start").disabled = false;
      stopWatch();
      loadSeries(true);
      return;
    }
    if (!episodeBusy(data)) {
      if (!state.holdStart) $("start").disabled = false;
      stopWatch();
      loadSeries(true);
    }
  };

  try {
    const es = new EventSource(`/api/projects/${id}/events`);
    state.source = es;
    es.onmessage = (ev) => apply(JSON.parse(ev.data));
    es.onerror = () => {};
  } catch (_) {}

  state.timer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const r = await fetch(`/api/projects/${id}`);
      if (r.ok) apply(await r.json());
    } catch (_) {}
  }, state.source ? 6000 : 4000);
}

function renderProgress(p) {
  $("progress-bar").style.width = (p.progress || 0) + "%";
  $("progress-stage").textContent = STAGE_LABEL[p.status] || p.status;
  $("progress-msg").textContent = p.message || "";
}

function renderLog(p) {
  const events = p.events || [];
  const log = $("log");
  if (state.renderCtx.projectId === p.id && state.renderCtx.logLen === events.length) return;
  if (state.renderCtx.projectId === p.id && state.renderCtx.logLen > 0 && state.renderCtx.logLen < events.length) {
    for (let i = state.renderCtx.logLen; i < events.length; i += 1) {
      const li = document.createElement("li");
      li.textContent = events[i].message;
      log.appendChild(li);
    }
  } else {
    log.innerHTML = events.map((e) => `<li>${escapeHtml(e.message)}</li>`).join("");
  }
  state.renderCtx.logLen = events.length;
  log.scrollTop = log.scrollHeight;
}

function render(p, opts = {}) {
  queueRender(p, opts);
}

function renderNow(p, opts = {}) {
  const switchingEpisode = state.renderCtx.projectId !== p.id;
  const full = opts.full !== false && switchingEpisode;
  state.project = p;

  if (full) {
    state.renderCtx.projectId = p.id;
    state.renderCtx.shotsKey = "";
    state.renderCtx.picturesLayout = "";
    state.renderCtx.logLen = 0;
    state.renderCtx.clipsMetaKey = "";
    if (p.duration_seconds) $("duration").value = p.duration_seconds;
    if (p.duration_reason) {
      $("duration-hint").textContent = p.duration_reason;
      $("duration-hint").dataset.locked = "1";
    }
    $("prompt").textContent =
      episodePromptText(p) || p.video_prompt || (p.status === "error" ? p.error : "生成中…");
  } else if (switchingEpisode) {
    state.renderCtx.projectId = p.id;
  }

  renderProgress(p);
  renderLog(p);
  updatePipeline(p);
  syncStartButton(p);

  if (full) {
    renderTabPanel(p, "shots", { force: true });
    renderTabPanel(p, "pictures", { force: true });
    renderTabPanel(p, "prompt", { force: true });
    renderTabPanel(p, "videos", { force: true });
    return;
  }

  renderTabPanel(p, activeTab(), { force: false });
  if (episodeBusy(p)) {
    if (activeTab() !== "videos" && (p.clips || []).some((c) => ["submitting", "queued", "running"].includes(c.video_status))) {
      renderTabPanel(p, "videos", { force: false, light: true });
    }
    if (activeTab() !== "pictures" && (p.pictures || []).some((x) => x.status === "generating")) {
      renderTabPanel(p, "pictures", { force: false });
    }
  }
}

function renderTabPanel(p, tab, opts = {}) {
  if (tab === "shots") {
    if (p.analysis) renderShots(p, !!opts.force);
    else renderShotsEmpty(p);
    return;
  }
  if (tab === "pictures") {
    if (p.pictures && p.pictures.length) renderPictures(p, !!opts.force);
    else renderPicturesEmpty(p);
    return;
  }
  if (tab === "prompt") {
    const label = isSeedanceProvider(p) ? "Seedance 分段提示词" : "H3 分段提示词";
    $("prompt-label") && ($("prompt-label").textContent = label);
    $("prompt").textContent =
      episodePromptText(p) || (isSeedanceProvider(p) ? "" : p.video_prompt) || (p.status === "error" ? p.error : episodeBusy(p) ? "生成中…" : "还没有提示词。");
    return;
  }
  if (tab === "videos") {
    renderClips(p, { light: !!opts.light });
  }
}

function renderShotsEmpty(p) {
  state.renderCtx.shotsKey = `${p.id}:none`;
  $("shots-empty").style.display = "";
  $("shots-meta").innerHTML = `<div class="meta-line">${escapeHtml(panelStatusMessage(p))}</div>`;
  $("shots").innerHTML = "";
}

function picturesPanelBusy(p) {
  if (p.running) return !p.pictures || !p.pictures.length;
  const pics = p.pictures || [];
  if (pics.some((x) => x.status === "generating")) return true;
  return false;
}

function renderPicturesEmpty(p) {
  state.renderCtx.picturesLayout = `${p.id}:none`;
  $("pictures-empty").style.display = "";
  const msg =
    p.status === "error"
      ? "参考图未生成（本集拆解失败）。"
      : picturesPanelBusy(p)
        ? "参考图排队生成中…"
        : p.analysis
          ? "参考图列表为空，请重新生成本集分镜。"
          : "还没有参考图。";
  $("pictures-meta").innerHTML = `<div class="meta-line">${escapeHtml(msg)}</div>`;
  $("pictures").innerHTML = "";
}

function syncStartButton(p) {
  const btn = $("start");
  if (!btn || !p || p.id !== state.episodeId || state.composing) return;
  if (p.status === "error" || (!p.running && !p.analysis && ["queued", "analyzing"].includes(p.status))) {
    btn.textContent = "重新生成本集分镜";
    btn.disabled = false;
    return;
  }
  const marks = countEpisodeMarks($("script").value || "");
  const split = $("split-series").checked;
  btn.textContent = split || marks >= 2 ? "拆集并生成" : "生成本集分镜";
}

function renderShots(p, full) {
  const a = p.analysis;
  const beatSig = (a.shots || []).map((s) => s.beat || "").join("|");
  const key = `${p.id}:${a.title || ""}:${beatSig}:${a.duration_seconds || 0}`;
  if (!full && state.renderCtx.shotsKey === key) return;
  state.renderCtx.shotsKey = key;
  $("shots-empty").style.display = "none";
  const spatial = (a.visual && a.visual.spatial_lock) ? `<br/>站位锁：${escapeHtml(a.visual.spatial_lock)}` : "";
  const entity = a.entity_lock ? `<br/>人数锁：${escapeHtml(a.entity_lock)}` : "";
  $("shots-meta").innerHTML = `<div class="meta-line"><b>${escapeHtml(a.title || "未命名")}</b> · 整集 ${a.duration_seconds}s · ${(p.clips || []).length} 段出片 · ${escapeHtml(p.video_ratio || a.aspect_ratio || "9:16")} · ${escapeHtml(a.style || "")}<br/>${escapeHtml(a.duration_reason || p.duration_reason || "")}<br/>${escapeHtml(a.logline || "")}<br/>完整事件：${(a.complete_event || []).join(" → ")}${spatial}${entity}</div>`;
  $("shots").innerHTML = (a.shots || [])
    .map((s) => {
      const block = s.blocking ? `<p><small>站位</small> ${escapeHtml(s.blocking)}</p>` : "";
      const marks = (s.marks || []).length
        ? `<p><small>位置</small> ${escapeHtml((s.marks || []).map((m) => [m.name, m.side, m.facing, m.anchor, m.posture].filter(Boolean).join("·")).join("；"))}</p>`
        : "";
      const cont = s.continuity ? `<p><small>衔接</small> ${escapeHtml(s.continuity)}</p>` : "";
      return `<article class="shot">
        <div>
          <b>${fmtTime(s.start)}–${fmtTime(s.end)}</b>
          <small>CLIP ${s.clip_index || 1} · ${escapeHtml(s.shot_size || "")}</small>
        </div>
        <div>
          <b>${escapeHtml(s.beat || "")}</b>
          <p>${escapeHtml(s.camera || "")}</p>
          ${block}
          ${marks}
          <p><small>起始</small> ${escapeHtml(s.start_state || "")}</p>
          <p><small>过程</small> ${escapeHtml(s.process || "")}</p>
          <p><small>结果</small> ${escapeHtml(s.result_state || "")}</p>
          ${cont}
        </div>
      </article>`;
    })
    .join("");
}

const KIND_LABEL = { character: "角色", scene: "场景", prop: "道具" };

function isExtraPic(p, pic) {
  if (!pic || pic.kind !== "character") return false;
  const chars = (p.analysis && p.analysis.characters) || [];
  const ch = chars.find((c) => c.name === pic.name || c.id === pic.ref_id);
  const blob = `${(ch && ch.role) || ""} ${pic.name || ""} ${pic.lock_text || ""}`;
  return /群演|路人|群众|士兵|保镖|龙套/.test(blob);
}

function pictureKindLabel(p, pic) {
  if (isExtraPic(p, pic)) return "群演";
  return KIND_LABEL[pic.kind] || pic.kind;
}

function clipLockLabel(clip) {
  const bits = [];
  if (clip.camera_lock) bits.push(`机位 ${clip.camera_lock}`);
  if (clip.headcount_lock) bits.push(`人数 ${clip.headcount_lock}`);
  if (clip.scene_lock) bits.push(`场景 ${clip.scene_lock}`);
  if (clip.lighting_lock) bits.push(`灯光 ${clip.lighting_lock}`);
  if (clip.position_lock) bits.push(`位置 ${clip.position_lock}`);
  return bits.join(" · ");
}

function clipRefLabel(p, clip) {
  const byIdx = Object.fromEntries((p.pictures || []).map((x) => [x.picture_index, x]));
  return (clip.picture_indices || [])
    .map((n, i) => {
      const pic = byIdx[n];
      if (!pic) return `图${i + 1}`;
      const kind = pictureKindLabel(p, pic);
      return `图${i + 1}·${kind}·${pic.name}`;
    })
    .join("、");
}

function pictureDraftKey(projectId, index) {
  return `${projectId}:${index}`;
}

function picturePromptText(p, pic) {
  const key = pictureDraftKey(p.id, pic.picture_index);
  const active = document.activeElement;
  if (
    active &&
    active.matches("textarea[data-prompt]") &&
    active.dataset.project === p.id &&
    active.dataset.prompt === String(pic.picture_index)
  ) {
    return active.value;
  }
  if (pic.status === "ready") {
    delete state.promptDrafts[key];
    return pic.prompt || "";
  }
  return state.promptDrafts[key] ?? pic.prompt ?? "";
}

function pictureVerifyBlock(pic) {
  const parts = [];
  if (pic.verify_warning) {
    parts.push(`<div class="warn">${escapeHtml(pic.verify_warning)}</div>`);
  }
  if (pic.generated_prompt && pic.prompt && pic.generated_prompt.trim() !== pic.prompt.trim()) {
    parts.push(
      `<details><summary>平台实际出图提示词</summary><p class="warn">${escapeHtml(pic.generated_prompt)}</p></details>`
    );
  }
  return parts.join("");
}

function pictureAssetRow(p, pic) {
  if (!isSeedanceProvider(p) || pic.kind !== "character") return "";
  const bound = pic.seedance_asset_id
    ? `<small>已绑定 ${escapeHtml(pic.seedance_asset_id)}</small>`
    : `<small>写实角色图会先入中转素材库；也可把 asset:// 粘到这里</small>`;
  const voice = pic.voice_source === "extracted"
    ? `<small>已从第 ${pic.voice_from_clip || "?"} 段成片锁定声线，后面开口会跟这个声音</small>`
    : pic.seedance_audio_asset_id
    ? `<small>声线已锁定 ${escapeHtml(pic.seedance_audio_asset_id)}</small>`
    : pic.voice_filename
      ? `<small>已上传声线样例，出片时入库</small>`
      : `<small>可上传 2–15 秒 MP3；不传的话，第一次开口出片后会自动抽音轨锁定</small>`;
  return `<div class="seedance-asset">
    <input data-seedance-asset="${pic.picture_index}" data-project="${p.id}" placeholder="asset://asset-xxxx" value="${escapeHtml(pic.seedance_asset_id || "")}" />
    <button type="button" class="ghost sm" data-save-asset="${pic.picture_index}">绑定素材库</button>
  </div>${bound}
  <div class="seedance-asset">
    <label class="ghost file-btn">上传声线 MP3<input type="file" accept="audio/mpeg,audio/wav,audio/mp4,audio/x-m4a,.mp3,.wav,.m4a" data-upload-voice="${pic.picture_index}" hidden /></label>
  </div>${voice}`;
}

function pictureActionRow(p, pic) {
  const canAct = pic.status !== "generating";
  const refetch =
    pic.prompt && (pic.status === "ready" || pic.status === "failed")
      ? `<button type="button" class="ghost sm" data-refetch="${pic.picture_index}">重新拉取</button>`
      : "";
  const regenLabel = pic.status === "failed" || pic.status === "skipped" ? "按此提示词重出" : "保存并重出";
  const regen = `<button type="button" class="ghost sm" data-regen="${pic.picture_index}" ${canAct ? "" : "disabled"}>${regenLabel}</button>`;
  return `<div class="row pic-actions">
          <label class="ghost file-btn">换本地图<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" data-upload="${pic.picture_index}" hidden /></label>
          ${refetch}
          ${regen}
        </div>`;
}

function clipDraftKey(projectId, index) {
  return `${projectId}:clip-${index}`;
}

function clipSeedanceDraftKey(projectId, index) {
  return `${projectId}:seedance-${index}`;
}

function isSeedanceProvider(p) {
  const sel = $("video-provider")?.value;
  if (sel === "seedance") return true;
  if (sel === "minimax") return false;
  return p?.video_provider === "seedance";
}

function clipPromptFor(p, clip) {
  if (isSeedanceProvider(p)) {
    const draft = state.promptDrafts[clipSeedanceDraftKey(p.id, clip.index)];
    if (draft != null) return draft;
    if (clip.seedance_prompt) return clip.seedance_prompt;
    return "（Seedance 提示词尚未生成。请重新打开本集，或重新生成分镜。）";
  }
  const draft = state.promptDrafts[clipDraftKey(p.id, clip.index)];
  if (draft != null) return draft;
  return clip.h3_prompt || "";
}

function episodePromptText(p) {
  if (isSeedanceProvider(p)) {
    if (p.video_seedance_prompt) return p.video_seedance_prompt;
    const clips = p.clips || [];
    if (!clips.length) return "";
    const blocks = clips
      .map((clip) => {
        const head = `========== CLIP ${clip.index} · ${clip.title || "片段"} · ${clip.duration_seconds}s · Seedance ==========`;
        const body = (clip.seedance_prompt || "").trim();
        return body ? `${head}\n${body}` : "";
      })
      .filter(Boolean);
    return blocks.length
      ? blocks.join("\n\n")
      : "（尚无 Seedance 提示词。重新打开本集后会自动补全，或重新生成分镜。）";
  }
  return p.video_prompt || "";
}

function harvestPictureDrafts() {
  document.querySelectorAll("textarea[data-prompt]").forEach((el) => {
    const pid = el.dataset.project;
    if (!pid) return;
    state.promptDrafts[pictureDraftKey(pid, el.dataset.prompt)] = el.value;
  });
}

function harvestClipDrafts() {
  document.querySelectorAll("textarea[data-clip-prompt]").forEach((el) => {
    const pid = el.dataset.project;
    if (!pid) return;
    const key =
      el.dataset.promptKind === "seedance"
        ? clipSeedanceDraftKey(pid, el.dataset.clipPrompt)
        : clipDraftKey(pid, el.dataset.clipPrompt);
    state.promptDrafts[key] = el.value;
  });
}

function pictureImgSrc(p, pic) {
  if (pic.status !== "ready") return "";
  const key = pictureDraftKey(p.id, pic.picture_index);
  const stamp = pic.image_stamp || state.imageVersions[key] || 1;
  return `/api/projects/${p.id}/images/${encodeURIComponent(pic.filename)}?v=${encodeURIComponent(stamp)}`;
}

function bumpPictureImage(p, index) {
  const key = pictureDraftKey(p.id, index);
  state.imageVersions[key] = (state.imageVersions[key] || 0) + 1;
}

function picturesLayoutKey(p) {
  return `${p.id}|${(p.pictures || [])
    .map((pic) => `${pic.picture_index}:${pic.kind}:${pic.name}`)
    .join(",")}`;
}

function patchPictureCard(p, pic) {
  const card = document.querySelector(`[data-pic="${pic.picture_index}"][data-project="${p.id}"]`);
  if (!card) return;
  card.classList.toggle("failed", pic.status === "failed");
  const ph = card.querySelector(".ph");
  let img = card.querySelector("img");
  if (pic.status === "ready") {
    const src = pictureImgSrc(p, pic);
    if (!img) {
      img = document.createElement("img");
      img.src = src;
      img.alt = pic.name;
      if (ph) ph.replaceWith(img);
      else card.prepend(img);
    } else if (img.getAttribute("src") !== src) {
      img.src = src;
    } else if (pic.image_stamp && !img.getAttribute("src").includes(encodeURIComponent(pic.image_stamp))) {
      img.src = src;
    }
  } else if (!ph && img) {
    const holder = document.createElement("div");
    holder.className = "ph";
    holder.textContent = statusText(pic, p);
    img.replaceWith(holder);
  } else if (ph) {
    ph.textContent = statusText(pic, p);
  }
  const statusEl = card.querySelector("[data-pic-status]");
  if (statusEl) statusEl.textContent = pic.status === "generating" ? "出图中…" : "";
  const errEl = card.querySelector(".err");
  if (pic.error) {
    if (errEl) errEl.textContent = pic.error;
    else {
      const div = document.createElement("div");
      div.className = "err";
      div.textContent = pic.error;
      card.appendChild(div);
    }
  } else if (errEl) {
    errEl.remove();
  }
  const regenBtn = card.querySelector("[data-regen]");
  if (regenBtn) {
    regenBtn.disabled = pic.status === "generating";
    regenBtn.textContent = pic.status === "failed" || pic.status === "skipped" ? "按此提示词重出" : "保存并重出";
  }
  const refetchBtn = card.querySelector("[data-refetch]");
  if (refetchBtn) {
    refetchBtn.disabled = pic.status === "generating";
    refetchBtn.style.display =
      pic.prompt && (pic.status === "ready" || pic.status === "failed") ? "" : "none";
  } else if (pic.prompt && (pic.status === "ready" || pic.status === "failed")) {
    const row = card.querySelector(".row:has([data-upload])") || card.querySelector(".row");
    if (row) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost sm";
      btn.dataset.refetch = String(pic.picture_index);
      btn.textContent = "重新拉取";
      row.appendChild(btn);
      bindPictureRefetch(p, btn);
    }
  }
  let verifyEl = card.querySelector("[data-pic-verify]");
  const verifyHtml = pictureVerifyBlock(pic);
  if (verifyHtml) {
    if (verifyEl) verifyEl.innerHTML = verifyHtml;
    else {
      verifyEl = document.createElement("div");
      verifyEl.dataset.picVerify = "1";
      verifyEl.innerHTML = verifyHtml;
      const lockP = card.querySelector("p");
      if (lockP) lockP.after(verifyEl);
      else card.appendChild(verifyEl);
    }
  } else if (verifyEl) {
    verifyEl.remove();
  }
  const assetInput = card.querySelector("input[data-seedance-asset]");
  if (assetInput && document.activeElement !== assetInput) {
    const next = pic.seedance_asset_id || "";
    if (assetInput.value !== next) assetInput.value = next;
  }
  const ta = card.querySelector("textarea[data-prompt]");
  if (ta && document.activeElement !== ta) {
    const text = picturePromptText(p, pic);
    if (ta.value !== text) ta.value = text;
  }
}

function bindPictureAsset(p, btn) {
  btn.addEventListener("click", async () => {
    const index = btn.dataset.saveAsset;
    const input = btn.closest("article")?.querySelector("input[data-seedance-asset]");
    const assetId = input ? input.value.trim() : "";
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "绑定中…";
    const r = await fetch(`/api/projects/${p.id}/pictures/${index}/seedance-asset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_id: assetId }),
    });
    btn.disabled = false;
    btn.textContent = old;
    if (!r.ok) {
      alert(await r.text());
      return;
    }
    const data = await r.json();
    state.episodeCache[p.id] = data;
    render(data, { full: false });
    watch(p.id);
  });
}

function bindPictureRefetch(p, btn) {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "拉取中…";
    await fetch(`/api/projects/${p.id}/pictures/${btn.dataset.refetch}/refetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ generate_if_missing: false }),
    });
    watch(p.id);
    btn.disabled = false;
    btn.textContent = old;
  });
}

function renderPictures(p, full) {
  if (!p.pictures || !p.pictures.length) return;
  const active = document.activeElement;
  if (active && active.matches && active.matches("textarea[data-prompt]")) {
    const pid = active.dataset.project || p.id;
    state.promptDrafts[pictureDraftKey(pid, active.dataset.prompt)] = active.value;
    return;
  }
  if (active && active.matches && active.matches("input[data-seedance-asset]")) {
    return;
  }
  harvestPictureDrafts();
  $("pictures-empty").style.display = "none";
  const nChar = p.pictures.filter((x) => x.kind === "character" && !isExtraPic(p, x)).length;
  const nExtra = p.pictures.filter((x) => isExtraPic(p, x)).length;
  const nScene = p.pictures.filter((x) => x.kind === "scene").length;
  const nProp = p.pictures.filter((x) => x.kind === "prop").length;
  const reused = p.pictures.filter((x) => x.reused).length;
  const showCancel = picturesPanelBusy(p);
  const showIngest =
    isSeedanceProvider(p) &&
    (p.pictures || []).some((x) => x.kind === "character" && x.status === "ready");
  $("pictures-meta").innerHTML =
    `共 ${p.pictures.length} 张参考图 · 角色 ${nChar}` +
    (nExtra ? ` · 群演 ${nExtra}` : "") +
    ` · 场景 ${nScene} · 道具 ${nProp}` +
    (reused ? ` · 沿用 ${reused}` : "") +
    (p.series_name ? ` · 项目《${escapeHtml(p.series_name)}》` : "") +
    (showCancel
      ? ` · <button type="button" class="btn ghost sm" id="cancel-imaging">停止出图队列</button>`
      : "") +
    (showIngest
      ? ` · <button type="button" class="btn ghost sm" id="ingest-seedance-assets">一键入库角色图</button>`
      : "");
  $("cancel-imaging")?.addEventListener("click", () => cancelImaging(p.id));
  $("ingest-seedance-assets")?.addEventListener("click", () => ingestSeedanceAssets(p.id));

  const layoutKey = picturesLayoutKey(p);
  const box = $("pictures");
  const cards = box.querySelectorAll("[data-pic]");
  const canPatch =
    !full &&
    state.renderCtx.picturesLayout === layoutKey &&
    cards.length === p.pictures.length &&
    [...cards].every((c) => c.dataset.project === p.id);
  if (canPatch) {
    p.pictures.forEach((pic) => patchPictureCard(p, pic));
    return;
  }
  state.renderCtx.picturesLayout = layoutKey;

  box.innerHTML = p.pictures
    .map((pic) => {
      const src = pictureImgSrc(p, pic);
      const media = src
        ? `<img src="${src}" alt="${escapeHtml(pic.name)}" />`
        : `<div class="ph">${escapeHtml(statusText(pic, p))}</div>`;
      const promptText = picturePromptText(p, pic);
      const failed = pic.status === "failed" || pic.status === "skipped";
      const verifyHtml = pictureVerifyBlock(pic);
      const editor = `<textarea data-project="${p.id}" data-prompt="${pic.picture_index}">${escapeHtml(promptText)}</textarea>
        ${pic.error ? `<div class="err">${escapeHtml(pic.error)}</div>` : ""}`;
      const promptBlock = `<details ${failed ? "open" : ""}><summary>编辑提示词</summary>${editor}</details>`;
      return `<article class="pic${pic.status === "failed" ? " failed" : ""}" data-pic="${pic.picture_index}" data-project="${p.id}">
        ${media}
        <div class="row"><span class="tag">&lt;Picture ${pic.picture_index}&gt;</span><small>${escapeHtml(pictureKindLabel(p, pic))}${pic.reused ? '<span class="badge">沿用</span>' : ""}</small></div>
        <h3>${escapeHtml(pic.name)}</h3>
        <p>${escapeHtml(pic.lock_text)}</p>
        ${verifyHtml ? `<div data-pic-verify>${verifyHtml}</div>` : ""}
        ${pictureActionRow(p, pic)}
        ${pictureAssetRow(p, pic)}
        <small data-pic-status>${pic.status === "generating" ? "出图中…" : ""}</small>
        ${promptBlock}
      </article>`;
    })
    .join("");
  box.querySelectorAll("[data-refetch]").forEach((btn) => bindPictureRefetch(p, btn));
  box.querySelectorAll("[data-save-asset]").forEach((btn) => bindPictureAsset(p, btn));
  box.querySelectorAll("[data-regen]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const index = btn.dataset.regen;
      const ta = btn.closest("article").querySelector("textarea[data-prompt]");
      const prompt = ta ? ta.value.trim() : "";
      state.promptDrafts[pictureDraftKey(p.id, index)] = prompt;
      btn.disabled = true;
      btn.textContent = "出图中…";
      await fetch(`/api/projects/${p.id}/pictures/${index}/regenerate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      watch(p.id);
    });
  });
  box.querySelectorAll("input[data-upload]").forEach((input) => {
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      const index = input.dataset.upload;
      const label = input.closest("label");
      const textNode = label && [...label.childNodes].find((n) => n.nodeType === 3);
      if (textNode) textNode.textContent = "上传中…";
      input.disabled = true;
      const body = new FormData();
      body.append("file", file);
      const r = await fetch(`/api/projects/${p.id}/pictures/${index}/upload`, {
        method: "POST",
        body,
      });
      if (!r.ok) {
        alert(await r.text());
        if (textNode) textNode.textContent = "换本地图";
        input.disabled = false;
        return;
      }
      const data = await r.json();
      state.episodeCache[p.id] = data;
      bumpPictureImage(p, index);
      render(data, { full: false });
      watch(p.id);
    });
  });
  box.querySelectorAll("input[data-upload-voice]").forEach((input) => {
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      const index = input.dataset.uploadVoice;
      const label = input.closest("label");
      const textNode = label && [...label.childNodes].find((n) => n.nodeType === 3);
      if (textNode) textNode.textContent = "上传中…";
      input.disabled = true;
      const body = new FormData();
      body.append("file", file);
      const r = await fetch(`/api/projects/${p.id}/pictures/${index}/voice`, {
        method: "POST",
        body,
      });
      if (!r.ok) {
        alert(await r.text());
        if (textNode) textNode.textContent = "上传声线 MP3";
        input.disabled = false;
        return;
      }
      const data = await r.json();
      state.episodeCache[p.id] = data;
      render(data, { full: false });
      watch(p.id);
    });
  });
}

const CLIP_STATUS = {
  idle: "待出片",
  submitting: "提交中",
  queued: "排队中",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

async function requestVideos(clipIndex, force, prompt) {
  let p = state.project;
  if (!p?.id && state.episodeId) {
    const loaded = await fetch(`/api/projects/${state.episodeId}`);
    if (loaded.ok) {
      p = await loaded.json();
      render(p);
    }
  }
  if (!p?.id) {
    alert("请先在左侧打开已经生成过的剧集，或先点「生成分镜」。");
    return;
  }
  const failed = (p.pictures || []).filter((x) => x.status === "failed");
  const ready = (p.pictures || []).filter((x) => x.status === "ready");
  if (failed.length) {
    alert(`还有失败参考图：${failed.map((x) => x.name).join("、")}。请先在「参考图」里改提示词重出，成功后再出片。`);
    return;
  }
  if (!ready.length) {
    alert("还没有成功的参考图。请先出图或重出失败图，不会改走纯文生视频。");
    return;
  }
  const body = {
    clip_index: clipIndex == null ? null : Number(clipIndex),
    resolution: selectedResolution() || $("resolution").value,
    ratio: $("ratio").value,
    force: !!force,
    provider: $("video-provider")?.value || "minimax",
  };
  if (body.provider === "seedance" && $("seedance-model")) {
    body.seedance_model = $("seedance-model").value;
  }
  if (prompt) body.prompt = prompt;
  const r = await fetch(`/api/projects/${p.id}/videos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    alert(await r.text());
    return;
  }
  watch(p.id);
}

async function requestEdit() {
  let p = state.project;
  if (!p?.id && state.episodeId) {
    const loaded = await fetch(`/api/projects/${state.episodeId}`);
    if (loaded.ok) {
      p = await loaded.json();
      render(p);
    }
  }
  if (!p?.id) {
    alert("请先打开一集，并确保已经出过片。");
    return;
  }
  const ok = (p.clips || []).filter((c) => c.video_filename).length;
  if (!ok) {
    alert("还没有成功的成片段，请先出片。");
    return;
  }
  const r = await fetch(`/api/projects/${p.id}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dub: $("edit-dub").checked,
      subtitles: $("edit-subs").checked,
      ambience: $("edit-ambience") ? $("edit-ambience").checked : true,
      remove_burned_subs: $("video-provider")?.value === "seedance"
        ? false
        : ($("edit-wipe") ? $("edit-wipe").checked : true),
    }),
  });
  if (!r.ok) {
    alert(await r.text());
    return;
  }
  watch(p.id);
}

async function requestCompile() {
  const series = currentSeries();
  if (!series?.id) {
    alert("请先在左侧选择一个项目。");
    return;
  }
  const ready = (series.episodes || []).filter((ep) => ep.has_video);
  if (!ready.length) {
    alert("这个项目还没有可拼接的成片。请先把各集出片或自动剪辑。");
    return;
  }
  const r = await fetch(`/api/series/${series.id}/compile`, { method: "POST" });
  if (!r.ok) {
    alert(await r.text());
    return;
  }
  const payload = await r.json();
  const idx = state.series.findIndex((s) => s.id === payload.id);
  if (idx >= 0) state.series[idx] = payload;
  else state.series.push(payload);
  renderCompilePlayer();
  watchCompile(payload.id);
}

function stopCompileWatch() {
  if (state.compileTimer) {
    clearInterval(state.compileTimer);
    state.compileTimer = null;
  }
}

function watchCompile(seriesId) {
  stopCompileWatch();
  state.compileTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/series/${seriesId}`);
      if (!r.ok) return;
      const payload = await r.json();
      const idx = state.series.findIndex((s) => s.id === payload.id);
      if (idx >= 0) state.series[idx] = payload;
      renderCompilePlayer();
      if (payload.compile_status !== "running") stopCompileWatch();
    } catch {
      /* ignore */
    }
  }, 2000);
}

function renderCompilePlayer() {
  const box = $("compile-player");
  const btn = $("compile-series");
  if (!box) return;
  const series = currentSeries();
  const eps = series?.episodes || [];
  const ready = eps.filter((ep) => ep.has_video);
  if (btn) btn.disabled = !series || !ready.length;
  const key = series
    ? `${series.id}:${series.compile_status}:${series.compile_filename}:${(series.compile_episodes || []).join(",")}`
    : "";
  if (key && state.renderCtx.compileKey === key) return;
  state.renderCtx.compileKey = key;
  if (!series) {
    box.innerHTML = "";
    return;
  }
  const file = series.compile_filename;
  const src = file ? `/api/series/${series.id}/videos/${encodeURIComponent(file)}` : "";
  const status = series.compile_status && series.compile_status !== "idle"
    ? `<small>${series.compile_status === "running" ? "合集剪辑中…" : series.compile_status === "succeeded" ? "合集完成" : "合集失败"}</small>`
    : "";
  const names = (series.compile_episodes || []).join("、");
  const hint = names
    ? `<p><small>已拼接 ${escapeHtml(names)}</small></p>`
    : ready.length
      ? `<p><small>可将 ${ready.length} 集按顺序拼成一条完整成片</small></p>`
      : `<p><small>各集出片或自动剪辑后，可一键合成全集</small></p>`;
  const err = series.compile_error ? `<div class="err">${escapeHtml(series.compile_error)}</div>` : "";
  const dl = src
    ? `<p><small><a class="ghost" href="${src}" target="_blank">下载全集</a></small></p>`
    : "";
  const video = src && series.compile_status === "succeeded" ? `<video controls src="${src}"></video>` : "";
  box.innerHTML = `<article class="clip-card compile-card ${series.compile_status || ""}">${video}
    <div class="row"><span class="tag">全集合集</span>${status}</div>
    ${hint}${dl}${err}</article>`;
}

function clipVideoSrc(p, clip) {
  if (!clip.video_filename) return "";
  const bust = encodeURIComponent(clip.task_id || clip.video_provider || "1");
  return `/api/projects/${p.id}/videos/${encodeURIComponent(clip.video_filename)}?v=${bust}`;
}

function clipPromptKind(p) {
  return isSeedanceProvider(p) ? "seedance" : "h3";
}

function clipsLayoutKey(p, clips) {
  const statuses = clips.map((c) => `${c.index}:${c.video_status}:${c.video_filename || ""}:${c.task_id || ""}:${c.video_provider || ""}`).join(",");
  return `${p.id}:${clips.length}:${statuses}:${clipPromptKind(p)}:${$("seedance-model")?.value || ""}`;
}

function patchClipCard(p, clip) {
  const card = document.querySelector(`[data-clip="${clip.index}"][data-project="${p.id}"]`);
  if (!card) return false;
  card.className = `clip-card ${clip.video_status}`;
  const statusEl = card.querySelector(".clip-status");
  if (statusEl) statusEl.textContent = CLIP_STATUS[clip.video_status] || clip.video_status;
  const refs = card.querySelector(".clip-refs");
  if (refs) refs.textContent = `参考 ${clipRefLabel(p, clip) || "无参考图"}`;
  const lockEl = card.querySelector(".clip-lock");
  if (lockEl) {
    const lock = clipLockLabel(clip);
    lockEl.textContent = lock;
    lockEl.style.display = lock ? "" : "none";
  }
  const errEl = card.querySelector(".err");
  if (clip.error) {
    if (errEl) errEl.textContent = clip.error;
    else {
      const div = document.createElement("div");
      div.className = "err";
      div.textContent = clip.error;
      const details = card.querySelector("details");
      if (details) card.insertBefore(div, details);
    }
  } else if (errEl) errEl.remove();
  const runBtn = card.querySelector("[data-run-clip]");
  if (runBtn) {
    runBtn.disabled = false;
    runBtn.textContent = clip.video_status === "succeeded" ? "重出此段" : "出此段";
  }
  const src = clipVideoSrc(p, clip);
  let videoEl = card.querySelector("video");
  const ph = card.querySelector(".ph");
  if (src) {
    if (!videoEl) {
      videoEl = document.createElement("video");
      videoEl.controls = true;
      if (ph) ph.replaceWith(videoEl);
      else card.prepend(videoEl);
    }
    if (videoEl.getAttribute("src") !== src) videoEl.src = src;
    if (ph) ph.remove();
  } else {
    const label = CLIP_STATUS[clip.video_status] || clip.video_status;
    if (videoEl) {
      const holder = document.createElement("div");
      holder.className = "ph";
      holder.textContent = label;
      videoEl.replaceWith(holder);
    } else if (ph && ph.textContent !== label) {
      ph.textContent = label;
    }
  }
  const useSeedance = isSeedanceProvider(p);
  const details = card.querySelector("details");
  if (details) {
    const summary = details.querySelector("summary");
    if (summary) summary.textContent = useSeedance ? "Seedance 提示词" : "H3 提示词";
    const ta = details.querySelector("textarea[data-clip-prompt]");
    if (ta && document.activeElement !== ta) {
      const kind = useSeedance ? "seedance" : "h3";
      const text = clipPromptFor(p, clip);
      if (ta.dataset.promptKind !== kind) {
        ta.dataset.promptKind = kind;
        ta.value = text;
      } else if (ta.value !== text) {
        ta.value = text;
      }
    }
  }
  return true;
}

function renderEditPlayer(p) {
  const box = $("edit-player");
  if (!box) return;
  const key = `${p.id}:${p.edit_status}:${p.edit_filename}:${p.subtitle_filename}:${p.edit_error || ""}`;
  if (state.renderCtx.editKey === key) return;
  state.renderCtx.editKey = key;
  const file = p.edit_filename;
  const src = file ? `/api/projects/${p.id}/videos/${encodeURIComponent(file)}` : "";
  const srt = p.subtitle_filename
    ? `<a class="ghost sm" href="/api/projects/${p.id}/videos/${encodeURIComponent(p.subtitle_filename)}" target="_blank">字幕 SRT</a>`
    : "";
  const voice = p.voiceover_filename
    ? `<a class="ghost sm" href="/api/projects/${p.id}/videos/${encodeURIComponent(p.voiceover_filename)}" target="_blank">配音音轨</a>`
    : "";
  const status = p.edit_status && p.edit_status !== "idle"
    ? `<small>${p.edit_status === "running" ? "剪辑中…" : p.edit_status === "succeeded" ? "剪辑完成" : "剪辑失败"}</small>`
    : "";
  const err = p.edit_error ? `<div class="err">${escapeHtml(p.edit_error)}</div>` : "";
  if (!src && !status && !err) {
    box.innerHTML = "";
    return;
  }
  const video = src ? `<video controls src="${src}"></video>` : "";
  box.innerHTML = `<article class="clip-card edit-card ${p.edit_status || ""}">${video}
    <div class="row"><span class="tag">自动剪辑成片</span>${status}</div>
    <p><small>${srt} ${voice}</small></p>${err}</article>`;
}

function clipsMetaKey(p, clips) {
  const ok = clips.filter((c) => c.video_filename).length;
  const statuses = clips.map((c) => `${c.index}:${c.video_status}`).join(",");
  return `${p.id}:${clips.length}:${ok}:${statuses}:${clipPromptKind(p)}:${p.episode_video_filename}:${p.edit_filename}`;
}

function renderClips(p, opts = {}) {
  const clips = p.clips || [];
  const box = $("clips");
  const empty = $("videos-empty");
  if (!clips.length) {
    empty.style.display = "";
    $("videos-meta").innerHTML = "";
    box.innerHTML = "";
    $("generate-all").disabled = true;
    $("auto-edit").disabled = true;
    state.renderCtx.clipsMetaKey = "";
    if (!opts.light) renderEditPlayer(p);
    if (!opts.light) renderCompilePlayer();
    return;
  }
  empty.style.display = "none";
  const ok = clips.filter((c) => c.video_filename).length;
  const metaKey = clipsMetaKey(p, clips);
  if (metaKey !== state.renderCtx.clipsMetaKey) {
    state.renderCtx.clipsMetaKey = metaKey;
    const ep = p.episode_video_filename
      ? `<a class="ghost sm" href="/api/projects/${p.id}/videos/${encodeURIComponent(p.episode_video_filename)}" target="_blank">下载整集拼接</a>`
      : "";
    const edited = p.edit_filename
      ? `<a class="ghost sm" href="/api/projects/${p.id}/videos/${encodeURIComponent(p.edit_filename)}" target="_blank">下载剪辑成片</a>`
      : "";
    const failedPics = (p.pictures || []).filter((x) => x.status === "failed");
    const readyPics = (p.pictures || []).filter((x) => x.status === "ready");
    const canVideo = readyPics.length && !failedPics.length;
    const warn = failedPics.length
      ? `还有失败参考图：${failedPics.map((x) => x.name).join("、")}。改提示词重出后再出片。`
      : readyPics.length
        ? ""
        : "还没有成功参考图，请先出图。";
    const providerLabel = isSeedanceProvider(p)
      ? `Seedance ${seedanceModelLabel(p.seedance_model || $("seedance-model")?.value)}`
      : "MiniMax H3";
    const recoverable = clips.filter((c) => c.task_id && c.video_status !== "succeeded" && !c.video_filename);
    const recoverBtn = recoverable.length
      ? ` · <button type="button" class="btn ghost sm" id="recover-videos">拉取已生成（${recoverable.length}）</button>`
      : "";
    $("videos-meta").innerHTML =
      `<div class="meta-line">共 ${clips.length} 段 · 已完成 ${ok} · ${escapeHtml(providerLabel)} · ${escapeHtml(p.video_ratio || "9:16")} · ${escapeHtml(p.video_resolution || "768P")} ${ep} ${edited}${recoverBtn}${warn ? `<br/>${escapeHtml(warn)}` : ""}</div>`;
    $("recover-videos")?.addEventListener("click", async () => {
      const btn = $("recover-videos");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "拉取中…";
      }
      const r = await fetch(`/api/projects/${p.id}/videos/recover`, { method: "POST" });
      if (!r.ok) {
        alert(await r.text());
        if (btn) {
          btn.disabled = false;
          btn.textContent = `拉取已生成（${recoverable.length}）`;
        }
        return;
      }
      watch(p.id);
    });
    $("generate-all").disabled = !canVideo;
    $("auto-edit").disabled = !ok;
  }
  if (!opts.light) {
    renderEditPlayer(p);
    renderCompilePlayer();
  }
  const active = document.activeElement;
  const editingPrompt = active && active.matches && active.matches("textarea[data-clip-prompt]");
  if (editingPrompt) {
    const pid = active.dataset.project || p.id;
    const key =
      active.dataset.promptKind === "seedance"
        ? clipSeedanceDraftKey(pid, active.dataset.clipPrompt)
        : clipDraftKey(pid, active.dataset.clipPrompt);
    state.promptDrafts[key] = active.value;
  }
  harvestClipDrafts();
  const layoutKey = clipsLayoutKey(p, clips);
  const cards = box.querySelectorAll("[data-clip]");
  const sameProject = [...cards].every((c) => c.dataset.project === p.id);
  const canPatch =
    !opts.force &&
    sameProject &&
    state.renderCtx.clipsLayoutKey === layoutKey &&
    cards.length === clips.length &&
    clips.length;
  if (canPatch) {
    let allPatched = true;
    clips.forEach((clip) => {
      if (!patchClipCard(p, clip)) allPatched = false;
    });
    if (allPatched) return;
  }
  state.renderCtx.clipsLayoutKey = layoutKey;
  const useSeedance = isSeedanceProvider(p);
  box.innerHTML = clips
    .map((clip) => {
      const src = clipVideoSrc(p, clip);
      const media = src
        ? `<video controls src="${src}"></video>`
        : `<div class="ph">${escapeHtml(CLIP_STATUS[clip.video_status] || clip.video_status)}</div>`;
      const promptText = clipPromptFor(p, clip);
      const pics = clipRefLabel(p, clip);
      return `<article class="clip-card ${clip.video_status}" data-clip="${clip.index}" data-project="${p.id}">
        ${media}
        <div class="row">
          <span class="tag">CLIP ${clip.index} · ${clip.duration_seconds}s</span>
          <small class="clip-status">${escapeHtml(CLIP_STATUS[clip.video_status] || clip.video_status)}</small>
        </div>
        <h3>${escapeHtml(clip.title || "片段")}</h3>
        <p>${escapeHtml(clip.hook || "")}</p>
        <p class="clip-lock"${clipLockLabel(clip) ? "" : ' style="display:none"'}><small>${escapeHtml(clipLockLabel(clip))}</small></p>
        <p><small class="clip-refs">参考 ${escapeHtml(pics || "无参考图")}</small></p>
        ${clip.error ? `<div class="err">${escapeHtml(clip.error)}</div>` : ""}
        <details>
          <summary>${useSeedance ? "Seedance 提示词" : "H3 提示词"}</summary>
          <textarea data-project="${p.id}" data-clip-prompt="${clip.index}" data-prompt-kind="${useSeedance ? "seedance" : "h3"}">${escapeHtml(promptText)}</textarea>
        </details>
        <div class="row">
          <button class="ghost" data-copy-clip="${clip.index}">复制</button>
          ${clip.task_id && clip.video_status !== "succeeded" ? `<button class="ghost" data-recover-clip="${clip.index}">拉取已生成</button>` : ""}
          <button class="ghost" data-run-clip="${clip.index}">${clip.video_status === "succeeded" ? "重出此段" : "出此段"}</button>
        </div>
      </article>`;
    })
    .join("");
  box.querySelectorAll("[data-copy-clip]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const ta = btn.closest("article").querySelector("textarea[data-clip-prompt]");
      await navigator.clipboard.writeText(ta ? ta.value : "");
      btn.textContent = "已复制";
      setTimeout(() => (btn.textContent = "复制"), 1200);
    });
  });
  box.querySelectorAll("[data-recover-clip]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "拉取中…";
      const index = btn.dataset.recoverClip;
      const r = await fetch(`/api/projects/${p.id}/videos/recover?clip_index=${index}`, { method: "POST" });
      if (!r.ok) {
        alert(await r.text());
        btn.disabled = false;
        btn.textContent = "拉取已生成";
        return;
      }
      watch(p.id);
    });
  });
  box.querySelectorAll("[data-run-clip]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const index = btn.dataset.runClip;
      const ta = btn.closest("article").querySelector("textarea[data-clip-prompt]");
      const prompt = ta ? ta.value.trim() : "";
      const kind = ta?.dataset.promptKind || (isSeedanceProvider(p) ? "seedance" : "h3");
      if (kind === "seedance") {
        state.promptDrafts[clipSeedanceDraftKey(p.id, index)] = prompt;
      } else {
        state.promptDrafts[clipDraftKey(p.id, index)] = prompt;
      }
      btn.disabled = true;
      await requestVideos(index, true, prompt);
    });
  });
}

function statusText(pic, p) {
  if (pic.reused && pic.status === "ready") return "沿用本剧";
  if (pic.status === "generating") return "出图中…";
  if (pic.status === "failed") return "出图失败";
  if (pic.status === "skipped") return "已跳过出图";
  if (pic.status === "pending") {
    const live =
      p &&
      p.status === "imaging" &&
      (p.pictures || []).some((x) => x.status === "generating");
    return live ? "排队中" : "未出图";
  }
  return "未出图";
}

async function ingestSeedanceAssets(projectId) {
  const btn = $("ingest-seedance-assets");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "入库中…";
  }
  try {
    const r = await fetch(`/api/projects/${projectId}/seedance-assets`, { method: "POST" });
    if (!r.ok) {
      alert(await r.text());
      return;
    }
    watch(projectId);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "一键入库角色图";
    }
  }
}

async function cancelImaging(projectId) {
  const r = await fetch(`/api/projects/${projectId}/pictures/cancel`, { method: "POST" });
  if (!r.ok) {
    alert(await r.text());
    return;
  }
  const data = await r.json();
  if (state.episodeId === projectId) {
    state.project = data;
    state.episodeCache[projectId] = data;
    render(data, { full: true });
  }
}

function fmtTime(n) {
  const v = Number(n);
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const h = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${m}-${day} ${h}:${min}`;
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function appendLog(stage, msg) {
  $("log").insertAdjacentHTML("beforeend", `<li>${escapeHtml(msg)}</li>`);
}

async function loadHealth() {
  try {
    const r = await fetch("/api/health");
    const h = await r.json();
    const ds = h.deepseek_configured ? "DeepSeek 已配置" : "DeepSeek 未配置";
    const img = h.image_api && h.image_api.status === "ok" ? "出图服务在线" : "出图服务未启动";
    let mm = "MiniMax 未配置";
    if (h.minimax_configured && h.minimax && h.minimax.status === "ok") {
      mm = `H3 可用 ${h.minimax.available_points ?? "?"} 积分`;
    } else if (h.minimax_configured) {
      mm = "MiniMax 密钥异常";
    }
    let sd = "Seedance 未配置";
    if (h.seedance_configured && h.seedance && h.seedance.status === "ok") {
      const credits = h.seedance.remain_credits;
      const balance = h.seedance.remain_balance;
      if (typeof credits === "number" && credits > 0) {
        sd = `Seedance ToAPIs 可用 ${credits}`;
      } else if (typeof balance === "number" && balance > 0) {
        sd = `Seedance ToAPIs 余额 ${balance}`;
      } else {
        sd = "Seedance ToAPIs 已配置";
      }
    } else if (h.seedance_configured) {
      sd = "Seedance 密钥异常";
    }
    const voice = h.doubao_configured ? "配音 OK" : "配音 —";
    const ff = h.ffmpeg ? "ffmpeg OK" : "ffmpeg —";
    $("health").textContent = `${img} · ${mm} · ${sd} · ${voice} · ${ff}`;
    const ok =
      h.image_api &&
      h.image_api.status === "ok" &&
      ((h.minimax && h.minimax.status === "ok") || (h.seedance && h.seedance.status === "ok"));
    $("health").style.color = ok ? "var(--success)" : "var(--warn)";
  } catch {
    $("health").textContent = "后端未响应";
    $("health").style.color = "var(--danger)";
  }
}

async function boot() {
  bindSidebarOnce();
  syncScriptPanelLayout();
  parseHash();
  await loadSeries(true);
  if (state.episodeId) {
    const series = currentSeries();
    const exists = series?.episodes?.some((ep) => ep.id === state.episodeId);
    if (exists) await openEpisode(state.episodeId);
  } else if (currentSeries()?.episodes?.length) {
    await openEpisode(currentSeries().episodes[0].id);
  }
  updateContext();
  syncSplitMode();
}

loadHealth();
syncSeedanceModelVisibility();
syncResolutionOptions();
boot();
setInterval(loadHealth, 30000);
setInterval(() => {
  if (!document.hidden && !state.timer) loadSeries(true);
}, 60000);

