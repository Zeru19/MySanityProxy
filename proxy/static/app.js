/* SanityProxy Dashboard */
const API = "/dashboard/api";

let totalTokens = 0;
let totalReqs = 0;
let totalHits = 0;
let currentMode = "desensitize";

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
    <td class="mono truncate">${entry.path || "messages"}</td>
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
}

document.getElementById("selfcheck-policy").addEventListener("change", async (e) => {
  await fetch(`${API}/selfcheck`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ policy: e.target.value }),
  });
});

function applyMode(mode) {
  currentMode = mode;
  const label = document.getElementById("mode-label");
  const dot = document.querySelector(".dot");
  const toggleBtn = document.getElementById("btn-mode-toggle");
  if (mode === "desensitize") {
    label.textContent = "脱敏模式";
    dot.className = "dot dot-green";
    toggleBtn.textContent = "切换透明模式";
  } else {
    label.textContent = "透明模式";
    dot.className = "dot dot-yellow";
    toggleBtn.textContent = "切换脱敏模式";
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
      <td class="mono truncate">${escapeHtml(s.path)}</td>
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
      <td class="mono truncate" title="${rule.pattern}">${rule.pattern}</td>
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
