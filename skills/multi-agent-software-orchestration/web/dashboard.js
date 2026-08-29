const roleLabels = {
  "0": "总控与集成", "1": "核心系统", "2": "Web Backend", "3": "Web Frontend",
  "4": "QA 独立审核", "5": "DevX / 文档 / 工具链", "6": "Confirmatory Prior",
  "7": "Security Red Team", "8": "Evaluation Benchmark"
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
}

function statusClass(status) {
  const normalized = String(status || "").toLowerCase();
  if (["working", "active", "in_progress", "done"].includes(normalized)) return "good";
  if (["blocked", "urgent"].includes(normalized)) return "danger";
  if (["review", "ready", "high"].includes(normalized)) return "warn";
  return "neutral";
}

function summaryCard(label, value, hint) {
  return `<article class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(hint)}</small></article>`;
}

function renderAgent(agent) {
  const assignments = agent.assignments.length
    ? agent.assignments.map(item => `<div class="agent-task"><b>${escapeHtml(item.work_item)}</b><span>${escapeHtml(item.task)}</span></div>`).join("")
    : `<div class="agent-idle">当前无任务</div>`;
  return `<article class="agent-card ${agent.status === "IDLE" ? "is-idle" : ""}">
    <div class="agent-card-top"><span class="agent-number">${escapeHtml(agent.id)}</span><span class="badge ${statusClass(agent.status)}">${escapeHtml(agent.status)}</span></div>
    <h4>${escapeHtml(roleLabels[agent.id] || agent.name)}</h4>
    <div class="agent-tasks">${assignments}</div>
  </article>`;
}

function renderWorkItem(item) {
  const roleIds = item.roles.map(role => role.role);
  const roleDots = roleIds.length
    ? roleIds.map(id => `<span class="role-dot" title="${escapeHtml(roleLabels[id] || id)}">${escapeHtml(id)}</span>`).join("")
    : `<span class="muted">未分派</span>`;
  return `<tr>
    <td><strong class="wi-id">${escapeHtml(item.id)}</strong></td>
    <td><div class="work-title">${escapeHtml(item.title)}</div></td>
    <td><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status)}</span></td>
    <td><span class="badge ${statusClass(item.priority)}">${escapeHtml(item.priority)}</span></td>
    <td><span class="owner-chip">${escapeHtml(item.owner)}</span></td>
    <td><div class="role-dots">${roleDots}</div></td>
  </tr>`;
}

function renderProject(project) {
  const activeItems = project.work_items.filter(item => !["DONE", "CANCELLED"].includes(item.status));
  const workRows = activeItems.length
    ? activeItems.map(renderWorkItem).join("")
    : `<tr><td colspan="6"><div class="empty-table">当前没有进行中的 Work Item</div></td></tr>`;
  const initBadge = project.initialized
    ? `<span class="badge good">STATE READY</span>`
    : `<span class="badge neutral">等待首个任务初始化</span>`;

  return `<article class="project-panel">
    <div class="project-header">
      <div><div class="section-kicker">PROJECT</div><h2>${escapeHtml(project.name)}</h2></div>
      <div class="project-meta">${initBadge}<span>${project.counts.total} 个 Work Item</span></div>
    </div>
    <div class="project-body">
      <section>
        <div class="subhead"><h3>Work Items</h3><span>${project.counts.active} active · ${project.counts.blocked} blocked · ${project.counts.review} review</span></div>
        <div class="table-wrap"><table><thead><tr><th>ID</th><th>任务</th><th>状态</th><th>优先级</th><th>Owner</th><th>Agents</th></tr></thead><tbody>${workRows}</tbody></table></div>
      </section>
      <section>
        <div class="subhead"><h3>0–8 号 Agent</h3><span>${project.counts.working_agents} 个正在参与</span></div>
        <div class="agent-grid">${project.agents.map(renderAgent).join("")}</div>
      </section>
    </div>
  </article>`;
}

async function refresh() {
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store"});
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error?.message || "读取失败");
    const {totals, projects, generated_at} = payload.data;
    document.getElementById("summary").innerHTML = [
      summaryCard("项目", totals.projects, "已接入控制台"),
      summaryCard("进行中", totals.active_work_items, "Active / Review"),
      summaryCard("参与 Agent", totals.working_agents, "跨项目角色占用"),
      summaryCard("阻塞", totals.blocked_work_items, "需要关注")
    ].join("");
    document.getElementById("projects").innerHTML = projects.length ? projects.map(renderProject).join("") : `<div class="loading">没有配置项目</div>`;
    document.getElementById("updatedAt").textContent = `· ${new Date(generated_at).toLocaleTimeString("zh-CN", {hour12:false})} 更新`;
  } catch (error) {
    document.getElementById("projects").innerHTML = `<div class="error-panel">无法读取状态：${escapeHtml(error.message)}</div>`;
  }
}

refresh();
setInterval(refresh, 5000);
