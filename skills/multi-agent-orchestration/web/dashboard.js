const seatLabels = {
  "1": "Orchestrator",
  "2": "Independent Gatekeeper",
  "3": "Worker A",
  "4": "Worker B",
  "5": "Worker C"
};
const OPEN_STATUSES = new Set(["BACKLOG", "ACTIVE", "REVIEW", "BLOCKED", "PAUSED"]);

let latestSnapshot = null;
let refreshInFlight = false;
let pendingHighlightId = "";

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
}

function statusClass(status) {
  const value = String(status || "").toLowerCase();
  if (["working", "active", "done", "pass"].includes(value)) return "good";
  if (["blocked", "fail", "critical", "urgent"].includes(value)) return "danger";
  if (["review", "ready", "pending", "high", "paused"].includes(value)) return "warn";
  return "neutral";
}

function summaryCard(label, value, hint) {
  return `<article class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(hint)}</small></article>`;
}

function actionButton(label, action, projectId, itemId = "", seatId = "") {
  return `<button class="inline-action" type="button" data-action="${escapeHtml(action)}" data-project="${escapeHtml(projectId)}" data-item="${escapeHtml(itemId)}" data-seat="${escapeHtml(seatId)}">${escapeHtml(label)}</button>`;
}

function emptyValue(value, fallback = "暂无") {
  const text = String(value ?? "").trim();
  return text ? escapeHtml(text) : `<span class="muted">${escapeHtml(fallback)}</span>`;
}

function renderKeyValues(items) {
  return `<dl class="key-grid">${items.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${emptyValue(value)}</dd></div>`).join("")}</dl>`;
}

function currentFilters() {
  // 检索条件走 query string，详情深链继续用 hash，避免互相覆盖。
  const params = new URLSearchParams(location.search);
  return {
    query: (document.getElementById("workSearch")?.value || params.get("q") || "").trim(),
    status: document.getElementById("statusFilter")?.value || params.get("status") || "open"
  };
}

function writeFiltersToUrl() {
  const {query, status} = currentFilters();
  const params = new URLSearchParams(location.search);
  if (query) params.set("q", query); else params.delete("q");
  if (status && status !== "open") params.set("status", status); else params.delete("status");
  const search = params.toString();
  history.replaceState(null, "", `${location.pathname}${search ? `?${search}` : ""}${location.hash}`);
}

function itemMatchesFilters(item, filters) {
  if (filters.status === "open") {
    if (!OPEN_STATUSES.has(item.status)) return false;
  } else if (filters.status !== "all" && item.status !== filters.status) {
    return false;
  }
  if (!filters.query) return true;
  const haystack = [item.id, item.title, item.goal, item.profile, item.priority, item.status].join(" ").toLowerCase();
  return haystack.includes(filters.query.toLowerCase());
}

function renderSeat(agent, projectId) {
  const tasks = agent.assignments?.length
    ? agent.assignments.map(item => `<div class="agent-task"><b>${escapeHtml(item.work_item)}</b><span>${escapeHtml(item.role || item.task || "未命名 Assignment")}</span></div>`).join("")
    : `<div class="agent-idle">${agent.status === "CLOSED" ? "当前未启用" : "当前无 Assignment"}</div>`;
  return `<article class="agent-card ${["IDLE", "CLOSED"].includes(agent.status) ? "is-idle" : ""}">
    <div class="agent-card-top"><span class="agent-number">${escapeHtml(agent.id)}</span><span class="badge ${statusClass(agent.status)}">${escapeHtml(agent.status)}</span></div>
    <h4>${escapeHtml(agent.name || seatLabels[agent.id])}</h4>
    <div class="section-kicker">${escapeHtml(agent.kind)}</div>
    <div class="agent-tasks">${tasks}</div>
    <div class="card-actions">${actionButton("座位详情", "seat", projectId, "", agent.id)}</div>
  </article>`;
}

function renderGate(gate = {}, projectId = "", itemId = "") {
  const checks = (gate.checks || []).slice(0, 4).map(check => `<span class="gate-chip ${statusClass(check.result)}"><b>${escapeHtml(check.id || "check")}</b>${escapeHtml(check.result || "UNKNOWN")}</span>`).join("");
  const profile = `<span class="gate-chip neutral"><b>${escapeHtml(gate.profile || "generic")}</b>${escapeHtml(gate.verdict || "PENDING")}</span>`;
  const detail = projectId && itemId ? actionButton("门禁详情", "gate", projectId, itemId) : "";
  return `<div class="gate-list">${profile}${checks}${detail}</div>`;
}

function renderWorkItem(item, projectId) {
  const activeSeats = (item.seats || []).filter(seat => !["CLOSED", "IDLE"].includes(seat.status));
  const seatDots = activeSeats.length
    ? activeSeats.map(seat => `<span class="role-dot" title="${escapeHtml(seat.role || seatLabels[seat.id])}">${escapeHtml(seat.id)}</span>`).join("")
    : `<span class="muted">仅总管待接管</span>`;
  const highlight = item.id === pendingHighlightId ? " is-new" : "";
  return `<tr class="work-row${highlight}" data-work-item="${escapeHtml(item.id)}">
    <td><strong class="wi-id">${escapeHtml(item.id)}</strong></td>
    <td><div class="work-title">${actionButton(item.title, "work-item", projectId, item.id)}</div></td>
    <td><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status)}</span></td>
    <td><span class="badge ${statusClass(item.priority)}">${escapeHtml(item.priority)}</span></td>
    <td>${escapeHtml(item.profile || "generic")}</td>
    <td>${renderGate(item.gate, projectId, item.id)}</td>
    <td><div class="role-dots">${seatDots}</div></td>
  </tr>`;
}

function renderWorkCard(item, projectId) {
  const activeSeats = (item.seats || []).filter(seat => !["CLOSED", "IDLE"].includes(seat.status));
  const seatDots = activeSeats.length
    ? activeSeats.map(seat => `<span class="role-dot" title="${escapeHtml(seat.role || seatLabels[seat.id])}">${escapeHtml(seat.id)}</span>`).join("")
    : `<span class="muted">仅总管待接管</span>`;
  const highlight = item.id === pendingHighlightId ? " is-new" : "";
  return `<article class="work-card${highlight}" data-work-item="${escapeHtml(item.id)}">
    <div class="work-card-top"><strong class="wi-id">${escapeHtml(item.id)}</strong><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status)}</span></div>
    <h3>${actionButton(item.title, "work-item", projectId, item.id)}</h3>
    <div class="work-card-meta"><span class="badge ${statusClass(item.priority)}">${escapeHtml(item.priority)}</span><span>${escapeHtml(item.profile || "generic")}</span><div class="role-dots">${seatDots}</div></div>
    ${renderGate(item.gate, projectId, item.id)}
  </article>`;
}

function renderProject(project, filters) {
  const matched = project.work_items.filter(item => itemMatchesFilters(item, filters));
  const emptyText = project.work_items.length
    ? "没有匹配当前搜索或状态筛选的 Work Item"
    : "当前没有 Work Item";
  const rows = matched.length ? matched.map(item => renderWorkItem(item, project.id)).join("") : `<tr><td colspan="7"><div class="empty-table">${emptyText}</div></td></tr>`;
  const cards = matched.length ? matched.map(item => renderWorkCard(item, project.id)).join("") : `<div class="empty-table">${emptyText}</div>`;
  const stateBadge = project.initialized ? `<span class="badge good">${escapeHtml(project.schema || "STATE READY")}</span>` : `<span class="badge neutral">等待初始化</span>`;
  return `<article class="project-panel" data-project="${escapeHtml(project.id)}">
    <div class="project-header">
      <div><div class="section-kicker">PROJECT · ${escapeHtml(project.profile || "generic")}</div><h2>${escapeHtml(project.name)}</h2></div>
      <div class="project-meta">${stateBadge}<span>${project.counts.total} 个 Work Item</span>${actionButton("Project 详情", "project", project.id)}<button class="inline-action project-create" type="button" data-compose-project="${escapeHtml(project.id)}">＋ 在此项目新建</button></div>
    </div>
    <div class="project-body">
      <section>
        <div class="subhead"><h3>Work Items</h3><span>${matched.length} 显示 · ${project.counts.active} active · ${project.counts.blocked} blocked · ${project.counts.review} review</span></div>
        <div class="table-wrap desktop-table"><table><thead><tr><th>ID</th><th>任务</th><th>状态</th><th>优先级</th><th>Profile</th><th>Gate</th><th>Seats</th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="work-cards mobile-cards">${cards}</div>
      </section>
      <section>
        <div class="subhead"><h3>1 / 2 + 3 / 4 / 5</h3><span>1 总管 · 2 门禁 · 最多 3 个动态 Worker</span></div>
        <div class="agent-grid">${project.agents.map(agent => renderSeat(agent, project.id)).join("")}</div>
      </section>
    </div>
  </article>`;
}

function findProject(projectId) {
  return latestSnapshot?.projects.find(project => project.id === projectId);
}

function findWorkItem(project, itemId) {
  return project?.work_items.find(item => item.id === itemId);
}

function renderRecord(record) {
  const title = record.title || record.kind || record.verdict || record.body || record.role || "";
  return `<li><code>${escapeHtml(record.type || "record")}</code><span><b>${escapeHtml(record.from || "?")} → ${escapeHtml(record.to || "board")}</b> ${escapeHtml(title)}</span><small>${escapeHtml(record.id || "")}</small></li>`;
}

function renderGit(project) {
  const git = project.git || {};
  if (!project.needs_business_git) {
    return `<section class="detail-section"><div class="section-kicker">BUSINESS GIT</div><div class="diagnostic">当前 Project 不要求业务 Git；协作事实仍由 orchestration Git 持久化。</div></section>`;
  }
  if (!git.available) {
    return `<section class="detail-section"><div class="section-kicker">BUSINESS GIT</div><div class="diagnostic danger-text">${escapeHtml(git.error || "业务 Git 不可用")}</div></section>`;
  }
  const commits = (git.recent_commits || []).map(commit => `<li><code>${escapeHtml(commit.short)}</code><span>${escapeHtml(commit.subject)}</span></li>`).join("") || `<li class="muted">暂无提交</li>`;
  return `<section class="detail-section"><div class="section-kicker">BUSINESS GIT</div>${renderKeyValues([["Branch", git.branch], ["HEAD", git.head], ["Dirty", git.dirty ? "YES" : "NO"], ["Upstream", git.tracking?.upstream], ["Repository", git.repository_identity]])}<h4>Recent commits</h4><ul class="commit-list">${commits}</ul></section>`;
}

function renderProjectDetail(project) {
  return `<div class="detail-grid">
    <section class="detail-card"><div class="section-kicker">PROJECT</div>${renderKeyValues([["Name", project.name], ["ID", project.id], ["Schema", project.schema], ["Profile", project.profile], ["Business Git", project.needs_business_git ? "required" : "not required"], ["Orchestration", project.orchestration_path]])}</section>
    <section class="detail-card"><div class="section-kicker">COUNTS</div>${renderKeyValues([["Work Items", project.counts.total], ["Active", project.counts.active], ["Blocked", project.counts.blocked], ["Review", project.counts.review], ["Working seats", project.counts.working_agents]])}</section>
    <div class="detail-card detail-span">${renderGit(project)}</div>
  </div>`;
}

function renderWorkItemDetail(project, item) {
  const seats = (item.seats || []).map(seat => `<article class="role-detail-card"><div class="agent-card-top"><span class="agent-number">${escapeHtml(seat.id)}</span><span class="badge ${statusClass(seat.status)}">${escapeHtml(seat.status)}</span></div><h3>${escapeHtml(seat.role || seatLabels[seat.id])}</h3><p class="detail-task">${escapeHtml(seat.task || "暂无 Assignment")}</p>${renderKeyValues([["Kind", seat.kind], ["Assignment", seat.assignment_id], ["Blockers", seat.blockers], ["Last event", seat.last_event]])}</article>`).join("");
  const records = (item.records || []).slice().reverse().map(renderRecord).join("") || `<li class="muted">暂无 Record</li>`;
  return `<div class="detail-grid">
    <section class="detail-card"><div class="section-kicker">WORK ITEM</div><h2>${escapeHtml(item.id)} · ${escapeHtml(item.title)}</h2>${renderKeyValues([["Status", item.status], ["Priority", item.priority], ["Owner", item.owner], ["Profile", item.profile], ["Records head", item.records_head]])}</section>
    <section class="detail-card">${renderGate(item.gate, project.id, item.id)}</section>
    <section class="detail-card detail-span"><div class="section-kicker">GOAL</div><p class="detail-copy">${escapeHtml(item.goal || "暂无")}</p><div class="section-kicker">ACCEPTANCE</div><p class="detail-copy">${escapeHtml(item.acceptance || "暂无")}</p></section>
    <section class="detail-card detail-span"><div class="section-kicker">SEAT PROJECTION</div><div class="role-detail-grid">${seats}</div></section>
    <section class="detail-card detail-span"><div class="section-kicker">IMMUTABLE RECORD TIMELINE</div><ul class="commit-list">${records}</ul></section>
  </div>`;
}

function renderSeatDetail(project, seatId) {
  const agent = project.agents.find(item => item.id === seatId);
  const assignments = agent?.assignments || [];
  const cards = assignments.length ? assignments.map(item => `<section class="detail-card"><div class="detail-card-title"><span class="wi-id">${escapeHtml(item.work_item)}</span><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status)}</span></div><h3>${escapeHtml(item.role || item.task)}</h3>${renderKeyValues([["Assignment", item.assignment_id], ["Blockers", item.blockers], ["Last event", item.last_event]])}</section>`).join("") : `<div class="empty-panel">当前没有 Assignment</div>`;
  return `<div class="detail-grid"><section class="detail-card"><div class="section-kicker">SEAT</div><div class="role-hero"><span class="agent-number big">${escapeHtml(seatId)}</span><div><h2>${escapeHtml(agent?.name || seatLabels[seatId])}</h2><span class="badge ${statusClass(agent?.status)}">${escapeHtml(agent?.status || "IDLE")}</span></div></div><p>Role 是当前 Assignment 的工作重点，不是工具或能力权限。</p></section><div class="detail-span role-history">${cards}</div></div>`;
}

function renderGateDetail(item) {
  const gate = item.gate || {};
  const checks = (gate.checks || []).map(check => `<article><span>${escapeHtml(check.id || "check")}</span><strong class="${statusClass(check.result)}-text">${escapeHtml(check.result || "UNKNOWN")}</strong><small>${escapeHtml(check.evidence || "")}</small></article>`).join("") || `<div class="empty-panel">尚无 Gate Record</div>`;
  return `<div class="detail-grid"><section class="detail-card detail-span"><div class="section-kicker">INDEPENDENT GATE</div><h2>${escapeHtml(item.id)} · ${escapeHtml(item.title)}</h2>${renderKeyValues([["Profile", gate.profile], ["Verdict", gate.verdict], ["Gate record", gate.record_id], ["Waived", (gate.waived || []).join(", ")]])}<div class="gate-detail-grid">${checks}</div></section><section class="detail-card detail-span"><div class="section-kicker">EVIDENCE</div><p class="detail-copy">${escapeHtml(gate.evidence || "暂无")}</p></section></div>`;
}

function setDetailHash(action, projectId, itemId = "", seatId = "") {
  const params = new URLSearchParams({view: action, project: projectId});
  if (itemId) params.set("item", itemId);
  if (seatId) params.set("seat", seatId);
  history.replaceState(null, "", `${location.pathname}${location.search}#${params.toString()}`);
}

function showDetail(action, projectId, itemId = "", seatId = "", updateHash = true) {
  const project = findProject(projectId);
  if (!project) return;
  const item = itemId ? findWorkItem(project, itemId) : null;
  let html = "";
  let breadcrumb = project.name;
  if (action === "project") html = renderProjectDetail(project);
  if (action === "work-item" && item) { html = renderWorkItemDetail(project, item); breadcrumb += ` / ${item.id}`; }
  if (action === "seat") { html = renderSeatDetail(project, seatId); breadcrumb += ` / Seat ${seatId}`; }
  if (action === "gate" && item) { html = renderGateDetail(item); breadcrumb += ` / ${item.id} / Gate`; }
  if (!html) return;
  document.getElementById("projects").hidden = true;
  document.getElementById("detailView").hidden = false;
  document.getElementById("detailBreadcrumb").textContent = breadcrumb;
  document.getElementById("detailContent").innerHTML = html;
  if (updateHash) setDetailHash(action, projectId, itemId, seatId);
  document.getElementById("detailView").scrollIntoView({behavior: "smooth", block: "start"});
}

function closeDetail() {
  document.getElementById("detailView").hidden = true;
  document.getElementById("projects").hidden = false;
  history.replaceState(null, "", `${location.pathname}${location.search}`);
}

function restoreDetailFromHash() {
  if (!location.hash || !latestSnapshot) return;
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get("view") && params.get("project")) showDetail(params.get("view"), params.get("project"), params.get("item") || "", params.get("seat") || "", false);
}

function syncComposerProjects(preferredId = "") {
  const select = document.getElementById("composerProject");
  const previous = preferredId || select.value;
  select.replaceChildren();
  for (const project of latestSnapshot?.projects || []) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    if (project.id === previous) option.selected = true;
    select.append(option);
  }
  const hasProjects = Boolean(latestSnapshot?.projects?.length);
  document.getElementById("openComposer").disabled = !hasProjects;
  document.querySelector("#overviewTaskForm .submit-button").disabled = !hasProjects;
  if (!hasProjects) {
    select.innerHTML = `<option value="">没有可用项目</option>`;
  }
  const fullIntake = document.getElementById("fullIntakeLink");
  const selected = select.value;
  fullIntake.href = selected ? `/add?project=${encodeURIComponent(selected)}` : "/add";
}

function setComposerMessage(text, kind, linkHref = "", linkText = "") {
  const node = document.getElementById("composerMessage");
  node.hidden = false;
  node.className = `form-message ${kind}`;
  node.replaceChildren(document.createTextNode(text));
  if (linkHref && linkText) {
    node.append(document.createTextNode(" "));
    const link = document.createElement("a");
    link.href = linkHref;
    link.textContent = linkText;
    node.append(link);
  }
}

function openComposer(projectId = "") {
  const panel = document.getElementById("composer");
  panel.hidden = false;
  syncComposerProjects(projectId);
  if (projectId) {
    document.getElementById("composerProject").value = projectId;
    const project = findProject(projectId);
    if (project?.profile) document.getElementById("composerProfile").value = project.profile;
  }
  document.getElementById("composerTitle").focus();
  panel.scrollIntoView({behavior: "smooth", block: "start"});
}

function closeComposer() {
  document.getElementById("composer").hidden = true;
  document.getElementById("composerMessage").hidden = true;
}

function setRefreshStatus(text, {busy = false, error = false} = {}) {
  const label = document.getElementById("updatedAt");
  const pulse = document.getElementById("refreshPulse");
  const button = document.getElementById("refreshButton");
  label.textContent = text;
  label.classList.toggle("danger-text", error);
  pulse.classList.toggle("is-refreshing", busy);
  button.disabled = busy;
  button.textContent = busy ? "刷新中…" : "刷新";
}

function updateFilterHint(filters, visibleCount, totalCount) {
  const statusLabel = {
    open: "进行中",
    all: "全部",
    ACTIVE: "Active",
    REVIEW: "Review",
    BLOCKED: "Blocked",
    PAUSED: "Paused",
    BACKLOG: "Backlog",
    DONE: "Done",
    CANCELLED: "Cancelled"
  }[filters.status] || filters.status;
  const queryText = filters.query ? `，关键词 “${filters.query}”` : "";
  document.getElementById("filterHint").textContent = `状态：${statusLabel}${queryText} · 显示 ${visibleCount}/${totalCount}`;
}

function renderBoard() {
  if (!latestSnapshot) return;
  const filters = currentFilters();
  const {totals, projects, generated_at} = latestSnapshot;
  document.getElementById("summary").innerHTML = [
    summaryCard("项目", totals.projects, "已接入控制台"),
    summaryCard("进行中", totals.active_work_items, "Active / Review"),
    summaryCard("参与座位", totals.working_agents, "含总管与动态 Worker"),
    summaryCard("阻塞", totals.blocked_work_items, "需要 1 号收口")
  ].join("");
  const visibleCount = projects.reduce((count, project) => count + project.work_items.filter(item => itemMatchesFilters(item, filters)).length, 0);
  const totalCount = projects.reduce((count, project) => count + project.work_items.length, 0);
  updateFilterHint(filters, visibleCount, totalCount);
  document.getElementById("projects").innerHTML = projects.length ? projects.map(project => renderProject(project, filters)).join("") : `<div class="loading">没有配置 Project</div>`;
  syncComposerProjects();
  if (!document.getElementById("detailView").hidden) restoreDetailFromHash();
  const stamp = generated_at ? new Date(generated_at).toLocaleTimeString("zh-CN", {hour12: false}) : "";
  if (!refreshInFlight) setRefreshStatus(stamp ? `本机持久化状态 · ${stamp} 更新` : "本机持久化状态");
}

async function refresh(reason = "auto") {
  // 刷新只重绘总览投影，不重建 composer，避免输入中的创建表单被 5 秒轮询清掉。
  if (refreshInFlight) return;
  refreshInFlight = true;
  if (reason === "manual") setRefreshStatus("正在手动刷新…", {busy: true});
  else document.getElementById("refreshPulse").classList.add("is-refreshing");
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store"});
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error?.message || "读取失败");
    latestSnapshot = payload.data;
    renderBoard();
    const stamp = new Date(latestSnapshot.generated_at).toLocaleTimeString("zh-CN", {hour12: false});
    setRefreshStatus(reason === "manual" ? `已刷新 · ${stamp}` : `本机持久化状态 · ${stamp} 更新`);
  } catch (error) {
    if (!latestSnapshot) {
      document.getElementById("projects").innerHTML = `<div class="error-panel">无法读取状态：${escapeHtml(error.message)}</div>`;
    }
    setRefreshStatus(`刷新失败：${error.message}`, {error: true});
  } finally {
    refreshInFlight = false;
    document.getElementById("refreshPulse").classList.remove("is-refreshing");
  }
}

function restoreFiltersFromUrl() {
  const params = new URLSearchParams(location.search);
  const search = document.getElementById("workSearch");
  const status = document.getElementById("statusFilter");
  if (params.get("q")) search.value = params.get("q");
  if (params.get("status") && [...status.options].some(option => option.value === params.get("status"))) {
    status.value = params.get("status");
  }
}

document.getElementById("projects").addEventListener("click", event => {
  const compose = event.target.closest("[data-compose-project]");
  if (compose) {
    openComposer(compose.dataset.composeProject);
    return;
  }
  const button = event.target.closest("[data-action]");
  if (button) showDetail(button.dataset.action, button.dataset.project, button.dataset.item || "", button.dataset.seat || "");
});

document.getElementById("detailContent").addEventListener("click", event => {
  const button = event.target.closest("[data-action]");
  if (button) showDetail(button.dataset.action, button.dataset.project, button.dataset.item || "", button.dataset.seat || "");
});

document.getElementById("detailClose").addEventListener("click", closeDetail);
document.getElementById("openComposer").addEventListener("click", () => openComposer(document.getElementById("composerProject").value));
document.getElementById("closeComposer").addEventListener("click", closeComposer);
document.getElementById("refreshButton").addEventListener("click", () => refresh("manual"));
document.getElementById("workSearch").addEventListener("input", () => { writeFiltersToUrl(); renderBoard(); });
document.getElementById("statusFilter").addEventListener("change", () => { writeFiltersToUrl(); renderBoard(); });
document.getElementById("composerProject").addEventListener("change", () => {
  const projectId = document.getElementById("composerProject").value;
  const project = findProject(projectId);
  const profile = document.getElementById("composerProfile");
  if (project?.profile && [...profile.options].some(option => option.value === project.profile)) {
    profile.value = project.profile;
  }
  document.getElementById("fullIntakeLink").href = projectId ? `/add?project=${encodeURIComponent(projectId)}` : "/add";
});
window.addEventListener("hashchange", restoreDetailFromHash);

document.getElementById("overviewTaskForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "正在创建…";
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.sync_git = document.getElementById("composerSyncGit").checked;
  try {
    const response = await fetch("/api/work-items", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!result.ok) throw new Error(result.error?.message || "创建失败");
    const sync = result.git_sync || {status: "skipped", reason: "unknown"};
    const syncText = sync.status === "synced"
      ? `Git 已同步${sync.commit ? ` · ${sync.commit}` : ""}`
      : sync.status === "committed_not_pushed"
        ? (sync.reason === "push_not_requested"
          ? `已本地提交 ${sync.commit || ""}，未请求远端 push`
          : `已提交 ${sync.commit || ""}，但 push 未完成（${sync.reason}）`)
        : sync.status === "failed"
          ? `Git 同步失败（${sync.reason}）`
          : `未同步 Git（${sync.reason}）`;
    const kind = ["synced", "skipped"].includes(sync.status) || (sync.status === "committed_not_pushed" && sync.reason === "push_not_requested") ? "success" : "warning";
    pendingHighlightId = result.data.id;
    setComposerMessage(`已创建 ${result.data.id} · ${result.data.title}。${syncText}。`, kind);
    document.getElementById("composerTitle").value = "";
    document.getElementById("composerGoal").value = "";
    document.getElementById("composerAcceptance").value = "";
    await refresh("manual");
  } catch (error) {
    setComposerMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "创建 Work Item";
  }
});

restoreFiltersFromUrl();
refresh();
setInterval(() => refresh("auto"), 5000);
