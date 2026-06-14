/* SanityProxy Dashboard */
const API = "/dashboard/api";

let totalTokens = 0;
let totalReqs = 0;
let totalHits = 0;
let currentMode = "desensitize";

// ── Theme (light/dark) ───────────────────────────────────────────────────────
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light") root.setAttribute("data-theme", "light");
  else root.removeAttribute("data-theme");
}

(function initTheme() {
  const override = new URLSearchParams(location.search).get("theme");
  let theme = (override === "light" || override === "dark")
    ? override
    : localStorage.getItem("sanity-theme");
  if (!theme) {
    theme = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  applyTheme(theme);
})();

document.getElementById("btn-theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  localStorage.setItem("sanity-theme", next);
  applyTheme(next);
});

// ── Stat counters ────────────────────────────────────────────────────────────
function updateStats(entry) {
  totalReqs++;
  totalTokens += (entry.input_tokens || 0) + (entry.output_tokens || 0);
  totalHits += entry.hits || 0;
  document.getElementById("stat-tokens").textContent = totalTokens.toLocaleString();
  document.getElementById("stat-reqs").textContent = totalReqs;
  document.getElementById("stat-hits").textContent = totalHits;
}

// ── Log table ────────────────────────────────────────────────────────────────
function appendLog(entry) {
  updateStats(entry);
  const tbody = document.getElementById("log-body");
  const tr = document.createElement("tr");
  const statusClass = entry.status >= 200 && entry.status < 300 ? "badge-green" : "badge-red";
  const modeClass = entry.mode === "desensitize" ? "badge-blue" : "badge-yellow";
  const modeLabel = entry.mode === "desensitize" ? "脱敏" : "透明";
  tr.innerHTML = `
    <td>${entry.timestamp}</td>
    <td class="mono"><span class="truncate">${entry.path || "messages"}</span></td>
    <td>${entry.upstream ? `<span class="badge">${escapeHtml(entry.upstream)}</span>` : "-"}</td>
    <td>${entry.latency_ms}</td>
    <td>${entry.input_tokens || 0}</td>
    <td>${entry.output_tokens || 0}</td>
    <td>${entry.hits || 0}</td>
    <td><span class="badge ${statusClass}">${entry.status}</span></td>
    <td><span class="badge ${modeClass}">${modeLabel}</span></td>
  `;
  tbody.prepend(tr);
  // cap at 200 rows
  while (tbody.children.length > 200) tbody.removeChild(tbody.lastChild);
  document.getElementById("log-count").textContent = tbody.children.length;
}

document.getElementById("btn-clear-log").addEventListener("click", () => {
  document.getElementById("log-body").innerHTML = "";
  document.getElementById("log-count").textContent = "0";
  totalReqs = totalTokens = totalHits = 0;
  document.getElementById("stat-tokens").textContent = "0";
  document.getElementById("stat-reqs").textContent = "0";
  document.getElementById("stat-hits").textContent = "0";
});

// ── SSE log stream ───────────────────────────────────────────────────────────
function connectLogs() {
  const es = new EventSource(`${API}/logs`);
  es.onmessage = (e) => {
    try { appendLog(JSON.parse(e.data)); } catch (_) {}
  };
  es.onerror = () => { setTimeout(connectLogs, 3000); es.close(); };
}
connectLogs();

// ── Status + mode ────────────────────────────────────────────────────────────
async function fetchStatus() {
  const res = await fetch(`${API}/status`);
  const data = await res.json();
  applyMode(data.mode);
  if (data.selfcheck) document.getElementById("selfcheck-policy").value = data.selfcheck;
  applyNameDetection(data);
}

function applyNameDetection(data) {
  const toggle = document.getElementById("name-detection-toggle");
  const ctrl = document.getElementById("name-detection-control");
  if (!toggle) return;
  toggle.checked = !!data.name_detection;
  // jieba 未安装时禁用开关并提示
  if (data.name_detection_available === false) {
    toggle.checked = false;
    toggle.disabled = true;
    if (ctrl) {
      ctrl.style.opacity = "0.5";
      ctrl.title = "未安装 jieba，姓名识别不可用（pip install jieba 后重启代理）";
    }
  }
}

document.getElementById("selfcheck-policy").addEventListener("change", async (e) => {
  await fetch(`${API}/selfcheck`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ policy: e.target.value }),
  });
});

document.getElementById("name-detection-toggle").addEventListener("change", async (e) => {
  await fetch(`${API}/name-detection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked }),
  });
});

function applyMode(mode) {
  currentMode = mode;
  const label = document.getElementById("mode-label");
  const dot = document.querySelector(".stat-pill .dot");
  const toggleBtn = document.getElementById("btn-mode-toggle");
  const modeText = toggleBtn.querySelector(".mode-text");
  if (mode === "desensitize") {
    label.textContent = "脱敏模式";
    dot.className = "dot dot-green";
    toggleBtn.dataset.mode = "desensitize";
    modeText.textContent = "脱敏模式";
  } else {
    label.textContent = "透明模式";
    dot.className = "dot dot-yellow";
    toggleBtn.dataset.mode = "transparent";
    modeText.textContent = "透明模式";
  }
}

document.getElementById("btn-mode-toggle").addEventListener("click", async () => {
  const newMode = currentMode === "desensitize" ? "transparent" : "desensitize";
  const res = await fetch(`${API}/mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: newMode }),
  });
  const data = await res.json();
  applyMode(data.mode);
});

fetchStatus();

// ── Outbound audit snapshots ─────────────────────────────────────────────────
let snapItems = [];

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function loadSnapshots() {
  const res = await fetch(`${API}/snapshots`);
  const data = await res.json();
  snapItems = data.items || [];
  const sel = document.getElementById("snap-capacity");
  if (sel.value !== data.capacity) sel.value = data.capacity;
  renderSnapshots();
}

function renderSnapshots() {
  const tbody = document.getElementById("snap-body");
  tbody.innerHTML = "";
  snapItems.forEach((s, idx) => {
    const tr = document.createElement("tr");
    const n = s.leaks ? s.leaks.length : 0;
    let checkBadge;
    if (s.status === "blocked") checkBadge = `<span class="badge badge-red">拦截 ${n}</span>`;
    else if (s.status === "remasked") checkBadge = `<span class="badge badge-blue">补脱 ${n}</span>`;
    else if (s.status === "warned") checkBadge = `<span class="badge badge-yellow">告警 ${n}</span>`;
    else checkBadge = `<span class="badge badge-green">通过</span>`;
    tr.innerHTML = `
      <td class="mono">${s.timestamp}</td>
      <td class="mono"><span class="truncate">${escapeHtml(s.path)}</span></td>
      <td>${s.upstream ? `<span class="badge">${escapeHtml(s.upstream)}</span>` : "-"}</td>
      <td>${s.size}</td>
      <td>${s.hits}</td>
      <td>${checkBadge}</td>
      <td><button class="btn btn-sm" data-snap="${idx}">查看</button></td>
    `;
    tbody.appendChild(tr);
  });
}

document.getElementById("snap-body").addEventListener("click", (e) => {
  const idx = e.target.dataset.snap;
  if (idx === undefined) return;
  const s = snapItems[idx];
  const leaksBlock = document.getElementById("snap-leaks");
  if (s.leaks && s.leaks.length) {
    const labels = {
      blocked: "⚠️ 自检命中（该请求已被拦截，未发送）",
      remasked: "🛠 自检命中（已就地补脱后发送，原文未外泄）",
      warned: "⚠️ 自检命中（按「仅告警」策略已放行，未补脱）",
    };
    leaksBlock.style.display = "block";
    document.getElementById("snap-leaks-label").textContent = labels[s.status] || "⚠️ 自检命中";
    document.getElementById("snap-leaks-pre").textContent = JSON.stringify(s.leaks, null, 2);
  } else {
    leaksBlock.style.display = "none";
  }
  document.getElementById("snap-body-pre").textContent = s.body || "（空）";
  document.getElementById("snap-modal-backdrop").style.display = "flex";
});

document.getElementById("btn-snap-close").addEventListener("click", () => {
  document.getElementById("snap-modal-backdrop").style.display = "none";
});
document.getElementById("snap-modal-backdrop").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.style.display = "none";
});

document.getElementById("btn-snap-refresh").addEventListener("click", loadSnapshots);

document.getElementById("snap-capacity").addEventListener("change", async (e) => {
  await fetch(`${API}/snapshot-capacity`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ capacity: e.target.value }),
  });
  await loadSnapshots();
});

loadSnapshots();
// 出站快照随新请求增长，定时轻量刷新
setInterval(loadSnapshots, 5000);

// ── Rule test ────────────────────────────────────────────────────────────────
document.getElementById("btn-test").addEventListener("click", async () => {
  const text = document.getElementById("test-input").value.trim();
  if (!text) return;
  const res = await fetch(`${API}/rules/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const data = await res.json();
  document.getElementById("test-masked").textContent = data.masked;
  document.getElementById("test-mapping").textContent = JSON.stringify(data.mapping, null, 2);
  document.getElementById("test-results").style.display = "grid";
});

// ── Rules modal ──────────────────────────────────────────────────────────────
let rulesData = [];

async function loadRules() {
  const res = await fetch(`${API}/rules`);
  rulesData = await res.json();
  renderRules();
}

function renderRules() {
  const tbody = document.getElementById("rules-body");
  tbody.innerHTML = "";
  for (const rule of rulesData) {
    const tr = document.createElement("tr");
    const sourceLabel = rule.builtin ? '<span class="badge badge-blue">内置</span>' : '<span class="badge">自定义</span>';
    const deleteBtn = rule.builtin ? "" : `<button class="btn btn-sm btn-danger" data-id="${rule.id}" data-action="delete">删除</button>`;
    tr.innerHTML = `
      <td>
        <label class="toggle">
          <input type="checkbox" ${rule.enabled ? "checked" : ""} data-id="${rule.id}" data-action="toggle"/>
          <span class="toggle-slider"></span>
        </label>
      </td>
      <td>${rule.name}</td>
      <td>${rule.category}</td>
      <td class="mono" title="${rule.pattern}"><span class="truncate">${rule.pattern}</span></td>
      <td>${rule.preserve_prefix}</td>
      <td>${sourceLabel}</td>
      <td>${deleteBtn}</td>
    `;
    tbody.appendChild(tr);
  }
}

document.getElementById("rules-body").addEventListener("click", async (e) => {
  const action = e.target.dataset.action;
  const id = e.target.dataset.id;
  if (action === "delete") {
    await fetch(`${API}/rules/${id}`, { method: "DELETE" });
    await loadRules();
  }
});

document.getElementById("rules-body").addEventListener("change", async (e) => {
  if (e.target.dataset.action === "toggle") {
    const id = e.target.dataset.id;
    await fetch(`${API}/rules/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: e.target.checked ? 1 : 0 }),
    });
  }
});

document.getElementById("btn-rule-add").addEventListener("click", async () => {
  const name = document.getElementById("rf-name").value.trim();
  const category = document.getElementById("rf-category").value.trim();
  const pattern = document.getElementById("rf-pattern").value.trim();
  const preserve_prefix = parseInt(document.getElementById("rf-prefix").value) || 0;
  if (!name || !category || !pattern) return alert("请填写名称、分类和正则表达式");
  await fetch(`${API}/rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, category, pattern, preserve_prefix }),
  });
  ["rf-name", "rf-category", "rf-pattern"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("rf-prefix").value = "0";
  await loadRules();
});

// Open/close modal
document.getElementById("btn-rules").addEventListener("click", () => {
  document.getElementById("modal-backdrop").style.display = "flex";
  loadRules();
});
document.getElementById("btn-modal-close").addEventListener("click", () => {
  document.getElementById("modal-backdrop").style.display = "none";
});
document.getElementById("modal-backdrop").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) document.getElementById("modal-backdrop").style.display = "none";
});

// Export rules
document.getElementById("btn-export").addEventListener("click", () => {
  const custom = rulesData.filter(r => !r.builtin);
  const blob = new Blob([JSON.stringify(custom, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "sanity-rules.json";
  a.click();
});

// Import rules
document.getElementById("import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const text = await file.text();
  try {
    const rules = JSON.parse(text);
    for (const r of rules) {
      await fetch(`${API}/rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(r),
      });
    }
    await loadRules();
  } catch {
    alert("导入失败：JSON 格式错误");
  }
  e.target.value = "";
});

// ── Upstream routing ───────────────────────────────────────────────────────────
let routingData = { upstreams: [], routes: [], default_upstream: "" };

async function loadRouting() {
  const res = await fetch(`${API}/routing`);
  routingData = await res.json();
  renderUpstreams();
  renderRoutes();
  renderDefaultUpstream();
}

function renderUpstreams() {
  const tbody = document.getElementById("upstreams-body");
  tbody.innerHTML = "";
  for (const u of routingData.upstreams) {
    const ident = u.builtin ? `builtin:${u.name}` : u.id;
    const src = u.builtin ? '<span class="badge badge-blue">内置</span>' : '<span class="badge">自定义</span>';
    let key;
    if (!u.token_env) key = '<span class="key-no">无(透传)</span>';
    else if (u.key_present) key = '<span class="key-ok">✓ 就绪</span>';
    else key = '<span class="key-no">✗ 未设</span>';
    const del = u.builtin ? "" : `<button class="btn btn-sm btn-danger" data-ident="${u.id}" data-action="delete">删除</button>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><label class="toggle"><input type="checkbox" ${u.enabled ? "checked" : ""} data-ident="${ident}" data-action="toggle"/><span class="toggle-slider"></span></label></td>
      <td>${escapeHtml(u.name)}</td>
      <td class="mono"><span class="truncate" title="${escapeHtml(u.base_url)}">${escapeHtml(u.base_url)}</span></td>
      <td>${escapeHtml(u.auth_scheme)}</td>
      <td class="mono">${u.token_env ? escapeHtml(u.token_env) : "-"}</td>
      <td>${key}</td>
      <td>${u.supports_count_tokens ? "✓" : "—"}</td>
      <td>${src}</td>
      <td>${del}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderRoutes() {
  const tbody = document.getElementById("routes-body");
  tbody.innerHTML = "";
  for (const r of routingData.routes) {
    const ident = r.builtin ? `builtin:${r.name}` : r.id;
    const src = r.builtin ? '<span class="badge badge-blue">内置</span>' : '<span class="badge">自定义</span>';
    const del = r.builtin ? "" : `<button class="btn btn-sm btn-danger" data-ident="${r.id}" data-action="delete">删除</button>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><label class="toggle"><input type="checkbox" ${r.enabled ? "checked" : ""} data-ident="${ident}" data-action="toggle"/><span class="toggle-slider"></span></label></td>
      <td>${escapeHtml(r.name)}</td>
      <td class="mono">${escapeHtml(r.match)}</td>
      <td>${escapeHtml(r.upstream)}</td>
      <td class="mono">${r.model_rewrite ? escapeHtml(r.model_rewrite) : "-"}</td>
      <td>${r.priority}</td>
      <td>${src}</td>
      <td>${del}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderDefaultUpstream() {
  const sel = document.getElementById("default-upstream");
  sel.innerHTML = "";
  for (const u of routingData.upstreams) {
    const opt = document.createElement("option");
    opt.value = u.name;
    opt.textContent = u.name;
    sel.appendChild(opt);
  }
  sel.value = routingData.default_upstream;
}

// Upstream toggle / delete (event delegation)
document.getElementById("upstreams-body").addEventListener("change", async (e) => {
  if (e.target.dataset.action !== "toggle") return;
  await fetch(`${API}/upstreams/${encodeURIComponent(e.target.dataset.ident)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked ? 1 : 0 }),
  });
  await loadRouting();
});
document.getElementById("upstreams-body").addEventListener("click", async (e) => {
  if (e.target.dataset.action !== "delete") return;
  await fetch(`${API}/upstreams/${encodeURIComponent(e.target.dataset.ident)}`, { method: "DELETE" });
  await loadRouting();
});

// Route toggle / delete
document.getElementById("routes-body").addEventListener("change", async (e) => {
  if (e.target.dataset.action !== "toggle") return;
  await fetch(`${API}/routes/${encodeURIComponent(e.target.dataset.ident)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked ? 1 : 0 }),
  });
  await loadRouting();
});
document.getElementById("routes-body").addEventListener("click", async (e) => {
  if (e.target.dataset.action !== "delete") return;
  await fetch(`${API}/routes/${encodeURIComponent(e.target.dataset.ident)}`, { method: "DELETE" });
  await loadRouting();
});

// Add upstream
document.getElementById("btn-upstream-add").addEventListener("click", async () => {
  const name = document.getElementById("uf-name").value.trim();
  const base_url = document.getElementById("uf-base").value.trim();
  const auth_scheme = document.getElementById("uf-scheme").value;
  const token_env = document.getElementById("uf-tokenenv").value.trim();
  const supports_count_tokens = document.getElementById("uf-count").checked;
  if (!name || !base_url) return alert("请填写名称和 base_url");
  await fetch(`${API}/upstreams`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, base_url, auth_scheme, token_env, supports_count_tokens }),
  });
  ["uf-name", "uf-base", "uf-tokenenv"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("uf-count").checked = false;
  await loadRouting();
});

// Add route
document.getElementById("btn-route-add").addEventListener("click", async () => {
  const name = document.getElementById("rtf-name").value.trim();
  const match = document.getElementById("rtf-match").value.trim();
  const upstream = document.getElementById("rtf-upstream").value.trim();
  const model_rewrite = document.getElementById("rtf-rewrite").value.trim() || null;
  const priority = parseInt(document.getElementById("rtf-priority").value) || 0;
  if (!name || !match || !upstream) return alert("请填写名称、匹配和目标上游");
  await fetch(`${API}/routes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, match, upstream, model_rewrite, priority }),
  });
  ["rtf-name", "rtf-match", "rtf-upstream", "rtf-rewrite"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("rtf-priority").value = "0";
  await loadRouting();
});

// Default upstream
document.getElementById("default-upstream").addEventListener("change", async (e) => {
  await fetch(`${API}/default-upstream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: e.target.value }),
  });
});

// Open/close routing modal
document.getElementById("btn-routing").addEventListener("click", () => {
  document.getElementById("routing-modal-backdrop").style.display = "flex";
  loadRouting();
});
document.getElementById("btn-routing-close").addEventListener("click", () => {
  document.getElementById("routing-modal-backdrop").style.display = "none";
});
document.getElementById("routing-modal-backdrop").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.style.display = "none";
});
