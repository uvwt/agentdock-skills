function setMessage(text, kind) {
  const node = document.getElementById("formMessage");
  node.hidden = false;
  node.className = `form-message ${kind}`;
  node.innerHTML = text;
}

async function loadProjects() {
  const select = document.getElementById("project");
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store"});
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error?.message || "读取失败");
    select.replaceChildren();
    for (const project of payload.data.projects) {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = project.name;
      select.append(option);
    }
    if (!payload.data.projects.length) {
      select.innerHTML = `<option value="">没有可用项目</option>`;
      document.querySelector(".submit-button").disabled = true;
    }
  } catch (error) {
    select.innerHTML = `<option value="">读取项目失败</option>`;
    setMessage(`读取项目失败：${error.message}`, "error");
  }
}

document.getElementById("taskForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "正在创建…";
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.sync_git = document.getElementById("syncGit").checked;
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
        ? `已提交 ${sync.commit || ""}，但 push 失败（${sync.reason}）`
        : sync.status === "failed"
          ? `Git 同步失败（${sync.reason}）`
          : `未同步 Git（${sync.reason}）`;
    const kind = ["synced", "skipped"].includes(sync.status) ? "success" : "warning";
    setMessage(`已创建 <strong>${result.data.id}</strong> · ${result.data.title}。${syncText}。<a href="/">回到总览查看</a>`, kind);
    document.getElementById("title").value = "";
    document.getElementById("goal").value = "";
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "创建 Work Item";
  }
});

loadProjects();
