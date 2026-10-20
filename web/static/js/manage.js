/* Local maintenance UI. All data is rendered as text; no external assets. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="management-token"]').content;
  const titles = { overview: "运行总览", memory: "长期记忆", cleanup: "数据清理", backups: "备份中心", logs: "运行日志", settings: "高级设置" };
  const state = { page: "overview", profiles: [], profile: null, memoryDirty: false, profileRequest: 0,
    status: null, preview: null, logs: "", settings: null, fields: new Map(), category: "model", policyLoaded: false };
  let noticeTimer;
  let refreshTimer;

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  const count = (value) => Number(value || 0).toLocaleString("zh-CN");
  const date = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false }) : "暂无记录";
  function bytes(value) {
    const n = Number(value || 0);
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function notify(message, kind = "success") {
    clearTimeout(noticeTimer);
    $("notice").textContent = message;
    $("notice").className = "notice " + kind;
    $("notice").hidden = false;
    if (kind === "success") noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 8000);
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetch("/api/manage" + path, {
        method: options.method || "GET", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-Management-Token": token },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      if (options.download && response.ok) return response.blob();
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "操作未完成，请刷新页面后重试。");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("这次操作等待超时，请先刷新查看结果，再决定是否重试。");
      if (error instanceof TypeError) throw new Error("连接不上维护台。请双击启动入口重新打开。");
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function run(button, action) {
    if (button && button.dataset.busy) return;
    const label = button && button.textContent;
    if (button) { button.dataset.busy = "1"; button.disabled = true; button.textContent = "正在处理…"; }
    try { return await action(); }
    catch (error) { notify(error.message || "操作失败，请重试。", "error"); }
    finally {
      if (button) { delete button.dataset.busy; button.disabled = false; button.textContent = label; }
      if (state.settings) updateSettingsBar();
    }
  }
  function confirmAction(title, description, label = "确认", details = [], danger = false) {
    return new Promise((resolve) => {
      const dialog = $("confirmDialog");
      $("dialogTitle").textContent = title;
      $("dialogDescription").textContent = description;
      $("dialogConfirm").textContent = label;
      $("dialogConfirm").className = "button " + (danger ? "danger" : "primary");
      $("dialogDetails").replaceChildren();
      $("dialogDetails").hidden = !details.length;
      details.forEach((change) => {
        const row = node("div", "change-row");
        row.append(node("strong", "", change.label), node("p", "", change.text));
        $("dialogDetails").append(row);
      });
      let done = false;
      function finish(value) {
        if (done) return;
        done = true;
        dialog.close();
        $("dialogCancel").removeEventListener("click", cancel);
        $("dialogConfirm").removeEventListener("click", accept);
        dialog.removeEventListener("cancel", cancelled);
        resolve(value);
      }
      const cancel = () => finish(false);
      const accept = () => finish(true);
      const cancelled = (event) => { event.preventDefault(); finish(false); };
      $("dialogCancel").addEventListener("click", cancel);
      $("dialogConfirm").addEventListener("click", accept);
      dialog.addEventListener("cancel", cancelled);
      dialog.showModal();
      $("dialogCancel").focus();
    });
  }

  function settingsChanges() {
    const changes = {};
    state.fields.forEach(({ spec, input }) => {
      const value = fieldValue(spec, input);
      if (JSON.stringify(value) !== JSON.stringify(spec.value)) changes[spec.key] = value;
    });
    return changes;
  }
  async function navigate(page) {
    if (!titles[page]) page = "overview";
    if (page !== state.page && ((state.page === "memory" && state.memoryDirty) || (state.page === "settings" && Object.keys(settingsChanges()).length))) {
      if (!await confirmAction("有尚未保存的修改", "离开这一页会放弃当前表单的修改。", "放弃并离开")) return;
      if (state.page === "memory") { state.memoryDirty = false; if (state.profile) renderProfile(state.profile); }
      if (state.page === "settings") renderSettings(state.settings);
    }
    if (page !== state.page && $("notice").classList.contains("success")) $("notice").hidden = true;
    state.page = page;
    history.replaceState(null, "", "#" + page);
    Object.keys(titles).forEach((name) => { $("page-" + name).hidden = name !== page; });
    document.querySelectorAll(".nav-item").forEach((link) => {
      link.classList.toggle("active", link.dataset.page === page);
      if (link.dataset.page === page) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    $("pageTitle").textContent = titles[page];
    document.title = titles[page] + " · 鲸鱼维护台";
    await run(null, async () => {
      if (page === "memory") await loadProfiles();
      if (page === "backups") await loadBackups();
      if (page === "logs") await loadLogs();
      if (page === "settings" && !state.settings) await loadSettings();
    });
  }
  document.addEventListener("click", (event) => {
    const anchor = event.target.closest('a[href^="#"]');
    if (anchor) { event.preventDefault(); navigate(anchor.getAttribute("href").slice(1)); }
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.memoryDirty || Object.keys(settingsChanges()).length) { event.preventDefault(); event.returnValue = ""; }
  });

  async function loadStatus() {
    const data = await api("/status");
    state.status = data;
    $("shutdownManage").hidden = !data.can_shutdown;
    $("statReceived").textContent = count(data.stats.received);
    $("statSent").textContent = count(data.delivery_today.confirmed);
    $("statTokens").textContent = count(data.usage_today.total_tokens);
    $("healthFailed").textContent = count(data.delivery_today.failed);
    $("healthUnknown").textContent = count(data.delivery_today.unknown);
    $("healthQueue").textContent = data.average_queue_seconds + " 秒";
    $("healthUpdated").textContent = date(data.updated_at);
    const statusLabels = { active: "最近有运行心跳", stopped: "机器人已停止", stale: "运行心跳已过期", unknown: "暂未收到运行心跳" };
    $("heartbeat").className = "heartbeat " + data.state;
    $("heartbeatText").textContent = statusLabels[data.state] || statusLabels.unknown;
    $("modelInfo").textContent = data.model + " · " + (data.key_configured ? "模型连接已配置" : "模型 Key 尚未配置");
    $("targetCount").textContent = data.targets.length + " 个会话";
    $("targetList").replaceChildren();
    if (!data.targets.length) $("targetList").append(node("div", "empty", "尚未配置自动回复的会话。"));
    const triggerLabels = { all: "每条都回", mention: "被 @ 才回", prefix: "前缀触发" };
    data.targets.forEach((target) => {
      const row = node("div", "target-row");
      const meta = node("div", "target-meta");
      meta.append(node("strong", "", target.name), node("small", "", target.kind + " · " + target.persona));
      row.append(node("span", "target-avatar", target.kind === "群聊" ? "群" : "聊"), meta, node("span", "trigger-label", triggerLabels[target.trigger] || target.trigger));
      $("targetList").append(row);
    });
    if (!state.policyLoaded) {
      $("chatDays").value = data.retention.chat_days;
      $("memoryDays").value = data.retention.memory_days;
      $("mediaDays").value = data.retention.media_days;
      state.policyLoaded = true;
    }
    $("syncTime").textContent = "最近同步 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  async function loadProfiles() {
    const data = await api("/memory");
    state.profiles = data.profiles;
    state.maxFacts = data.max_facts;
    renderProfiles();
  }
  function renderProfiles() {
    const search = $("memorySearch").value.trim().toLocaleLowerCase();
    const profiles = state.profiles.filter((p) => p.room_name.toLocaleLowerCase().includes(search));
    $("profileList").replaceChildren();
    if (!profiles.length) $("profileList").append(node("div", "empty", search ? "没有找到这个会话。" : "有了聊天或记忆后，会话会出现在这里。"));
    profiles.forEach((profile) => {
      const button = node("button", "profile-button" + (state.profile && state.profile.room_id === profile.room_id ? " active" : ""));
      button.type = "button";
      button.append(node("strong", "", profile.room_name), node("small", "", profile.count + " 条记忆"));
      button.addEventListener("click", () => run(null, () => selectProfile(profile.room_id)));
      $("profileList").append(button);
    });
  }
  async function selectProfile(room) {
    if (state.memoryDirty && !await confirmAction("切换会话？", "当前记忆有尚未保存的修改。", "放弃并切换")) return;
    const requestId = ++state.profileRequest;
    const data = await api("/memory/profile?room=" + encodeURIComponent(room));
    if (requestId !== state.profileRequest) return;
    renderProfile(data);
    renderProfiles();
  }
  function facts() { return $("memoryFacts").value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean); }
  function renderProfile(profile) {
    state.profile = profile;
    state.memoryDirty = false;
    $("memoryEmpty").hidden = true;
    $("memoryDetail").hidden = false;
    $("memoryName").textContent = profile.room_name;
    $("memoryMeta").textContent = "最近更新 " + date(profile.updated_at);
    $("memoryFacts").value = profile.facts.join("\n");
    updateMemoryCount();
  }
  function updateMemoryCount() { $("memoryCount").textContent = facts().length + " / " + (state.maxFacts || 24) + " 条"; }
  $("memorySearch").addEventListener("input", renderProfiles);
  $("memoryFacts").addEventListener("input", () => {
    state.memoryDirty = JSON.stringify(facts()) !== JSON.stringify(state.profile.facts);
    updateMemoryCount();
  });
  $("saveMemory").addEventListener("click", () => run($("saveMemory"), async () => {
    const items = facts();
    if (items.length > state.maxFacts || items.some((f) => f.length > 80)) throw new Error("请保持每条记忆不超过 80 字，且不超过 " + state.maxFacts + " 条。");
    const saved = await api("/memory/profile", { method: "PUT", body: { room_id: state.profile.room_id, revision: state.profile.revision, facts: items } });
    renderProfile(saved); await loadProfiles(); notify("记忆已保存，下次生成回复时会读取更新后的内容。");
  }));
  $("clearMemory").addEventListener("click", () => run($("clearMemory"), async () => {
    if (!await confirmAction("忘记这个会话的长期记忆？", "将清空「" + state.profile.room_name + "」的全部长期记忆，聊天记录仍会保留。", "清空记忆", [], true)) return;
    const saved = await api("/memory/profile", { method: "PUT", body: { room_id: state.profile.room_id, revision: state.profile.revision, facts: [] } });
    renderProfile(saved); await loadProfiles(); notify("长期记忆已清空。");
  }));

  function clearPreview() { state.preview = null; $("cleanupResults").hidden = true; }
  ["chatDays", "memoryDays", "mediaDays"].forEach((id) => $(id).addEventListener("input", clearPreview));
  $("previewCleanup").addEventListener("click", () => run($("previewCleanup"), async () => {
    const values = ["chatDays", "memoryDays", "mediaDays"].map((id) => Number($(id).value));
    if (values.some((v) => !Number.isInteger(v) || v < 0 || v > 36500)) throw new Error("保留天数需为 0 到 36500 之间的整数。");
    const data = await api("/cleanup/preview", { method: "POST", body: { chat_days: values[0], memory_days: values[1], media_days: values[2] } });
    state.preview = data;
    $("cleanupResults").hidden = false;
    $("cleanupSummary").textContent = "共 " + data.count + " 个文件，预计释放 " + bytes(data.bytes) + "。预览 5 分钟内有效。";
    $("cleanupFiles").replaceChildren();
    if (!data.count) $("cleanupFiles").append(node("div", "empty", "这里很整洁，没有符合当前策略的文件。"));
    data.files.forEach((file) => {
      const row = node("div", "file-row");
      row.append(node("span", "", file.name), node("span", "", bytes(file.bytes)));
      $("cleanupFiles").append(row);
    });
    $("applyCleanup").disabled = !data.count;
  }));
  $("applyCleanup").addEventListener("click", () => run($("applyCleanup"), async () => {
    if (!state.preview) return;
    const preview = state.preview;
    if (!await confirmAction("确认清理这些文件？", "将删除清单中的 " + preview.count + " 个文件，预计释放 " + bytes(preview.bytes) + "。删除后无法撤销。", "确认清理", [], true)) return;
    const result = await api("/cleanup/apply", { method: "POST", body: { token: preview.token } });
    clearPreview(); notify("已清理 " + result.count + " 项，释放 " + bytes(result.bytes) + "。");
  }));

  async function loadBackups() {
    const data = await api("/backups");
    $("backupCount").textContent = data.backups.length + " 份";
    $("backupList").replaceChildren();
    if (!data.backups.length) $("backupList").append(node("div", "empty", "还没有备份。点击右上角，留下第一份聊天快照。"));
    data.backups.forEach((item) => {
      const row = node("div", "backup-row");
      const meta = node("div", "backup-meta");
      meta.append(node("strong", "", item.name), node("small", "", date(item.created_at) + " · " + bytes(item.bytes)));
      const button = node("button", "button secondary", "下载备份"); button.type = "button";
      button.addEventListener("click", () => run(button, async () => {
        const blob = await api("/backups/" + encodeURIComponent(item.name) + "/download", { download: true });
        const url = URL.createObjectURL(blob);
        const anchor = node("a"); anchor.href = url; anchor.download = item.name; anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      }));
      row.append(node("span", "backup-icon", "ZIP"), meta, button); $("backupList").append(row);
    });
  }
  $("createBackup").addEventListener("click", () => run($("createBackup"), async () => {
    const result = await api("/backups", { method: "POST", body: {} });
    await loadBackups(); notify("备份已创建，共保存 " + result.files + " 份数据文件，可随时下载。");
  }));
  async function loadLogs() { const data = await api("/logs?source=" + encodeURIComponent($("logSource").value)); state.logs = data.text; renderLogs(); }
  function renderLogs() {
    const filter = $("logFilter").value;
    const text = filter ? state.logs.split("\n").filter((line) => line.includes(filter)).join("\n") : state.logs;
    $("logContent").textContent = text || (filter ? "最近记录中没有这一类日志。" : "还没有运行日志。启动机器人后会在这里显示。");
  }
  $("logFilter").addEventListener("change", renderLogs);
  $("logSource").addEventListener("change", () => run(null, loadLogs));
  $("refreshLogs").addEventListener("click", () => run($("refreshLogs"), loadLogs));

  function fieldValue(spec, input) {
    if (spec.kind === "bool") return input.checked;
    if (spec.kind === "lines") return input.value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
    if (spec.kind === "number") return input.value === "" ? null : Number(input.value);
    if (spec.kind === "probability") return input.value === "" ? null : Number((Number(input.value) / 100).toFixed(6));
    return input.value;
  }
  function setField(spec, input, value) {
    if (spec.kind === "bool") { input.checked = !!value; input.previousElementSibling.textContent = value ? "已开启" : "已关闭"; }
    else if (spec.kind === "lines") input.value = value.join("\n");
    else if (spec.kind === "probability") input.value = Number((value * 100).toFixed(4));
    else input.value = value;
  }
  async function loadSettings() { renderSettings(await api("/settings")); }
  function renderSettings(data) {
    state.settings = data;
    state.fields = new Map();
    $("settingsTabs").replaceChildren(); $("settingsBody").replaceChildren();
    data.groups.forEach((group) => {
      const tab = node("button", "settings-tab", group.title); tab.type = "button"; tab.setAttribute("role", "tab"); tab.dataset.category = group.id;
      tab.addEventListener("click", () => { state.category = group.id; $("settingsSearch").value = ""; filterSettings(); });
      $("settingsTabs").append(tab);
      const panel = node("article", "panel settings-group"); panel.dataset.group = group.id;
      const heading = node("div", "panel-heading"); const headingText = node("div");
      headingText.append(node("h2", "", group.title), node("p", "settings-group-description", group.description)); heading.append(headingText); panel.append(heading);
      const resetGroup = node("button", "button ghost compact", "恢复本分类默认值"); resetGroup.type = "button";
      resetGroup.addEventListener("click", () => resetCategory(group.id)); heading.append(resetGroup);
      const fields = node("div", "settings-fields");
      group.fields.forEach((spec) => {
        const row = node("div", "setting-field" + (["textarea", "lines"].includes(spec.kind) ? " long" : ""));
        const id = "setting-" + spec.key.replaceAll(".", "-");
        const label = node("label", "", spec.label + (spec.kind === "probability" ? "（%）" : "")); label.htmlFor = id;
        let input;
        if (spec.kind === "select") {
          input = node("select"); spec.options.forEach(([value, name]) => { const option = node("option", "", name); option.value = value; input.append(option); });
        } else if (["textarea", "lines"].includes(spec.kind)) input = node("textarea");
        else { input = node("input"); input.type = spec.kind === "bool" ? "checkbox" : ["number", "probability"].includes(spec.kind) ? "number" : spec.kind === "time" ? "time" : "text"; }
        input.id = id; input.dataset.key = spec.key;
        if (["number", "probability"].includes(spec.kind)) {
          const multiplier = spec.kind === "probability" ? 100 : 1;
          if (spec.min !== null) input.min = spec.min * multiplier;
          if (spec.max !== null) input.max = spec.max * multiplier;
          input.step = spec.step * multiplier; input.required = true;
        }
        if (spec.kind === "text") input.maxLength = 200;
        if (spec.kind === "textarea") input.maxLength = 12000;
        row.append(label);
        if (spec.kind === "bool") { const wrap = node("div", "toggle-row"); wrap.append(node("span"), input); row.append(wrap); }
        else row.append(input);
        const help = node("small", "", spec.description || " "); help.id = id + "-help"; row.append(help); input.setAttribute("aria-describedby", help.id);
        setField(spec, input, spec.value);
        input.addEventListener("input", () => { if (spec.kind === "bool") input.previousElementSibling.textContent = input.checked ? "已开启" : "已关闭"; updateSettingsBar(); });
        state.fields.set(spec.key, { spec, input, row, group: group.id }); fields.append(row);
      });
      panel.append(fields); $("settingsBody").append(panel);
    });
    filterSettings(); updateSettingsBar();
  }
  function filterSettings() {
    const query = $("settingsSearch").value.trim().toLowerCase();
    document.querySelectorAll(".settings-tab").forEach((tab) => { const active = tab.dataset.category === state.category; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", active ? "true" : "false"); });
    state.settings.groups.forEach((group) => {
      let visible = 0;
      group.fields.forEach((spec) => {
        const item = state.fields.get(spec.key);
        const match = query ? [spec.label, spec.key, spec.description].join(" ").toLowerCase().includes(query) : group.id === state.category;
        item.row.hidden = !match;
        if (match) visible++;
      });
      document.querySelector('[data-group="' + group.id + '"]').hidden = !visible;
    });
  }
  function updateSettingsBar() {
    const changes = settingsChanges(); const n = Object.keys(changes).length;
    $("settingsBar").classList.toggle("has-changes", n > 0);
    $("dirtyCount").textContent = n ? "有 " + n + " 项尚未保存" : "所有设置已保存";
    $("settingsHint").textContent = n ? "修改暂存于表单，确认保存后才会写入配置。" : "机器人相关参数需重启后生效。";
    state.fields.forEach((item, key) => item.row.classList.toggle("changed", Object.hasOwn(changes, key)));
    if (!$("saveSettings").dataset.busy) $("saveSettings").disabled = !n || !state.settings.writable;
    $("discardSettings").disabled = !n;
    $("undoSettings").disabled = !state.settings.can_undo;
  }
  $("settingsSearch").addEventListener("input", () => { if (state.settings) filterSettings(); });
  function resetCategory(category) {
    state.fields.forEach((item) => { if (item.group === category) setField(item.spec, item.input, item.spec.default); });
    updateSettingsBar(); notify("已将本分类的表单恢复为默认值，点击保存后才会生效。", "info");
  }
  $("resetSettings").addEventListener("click", () => resetCategory(state.category));
  $("discardSettings").addEventListener("click", () => renderSettings(state.settings));
  function displayValue(value) { return Array.isArray(value) ? value.join("\n") || "（空）" : typeof value === "boolean" ? value ? "开启" : "关闭" : value === "" ? "（空）" : String(value); }
  $("saveSettings").addEventListener("click", () => run($("saveSettings"), async () => {
    const changes = settingsChanges();
    for (const key of Object.keys(changes)) {
      const item = state.fields.get(key);
      if (!item.input.checkValidity()) { state.category = item.group; $("settingsSearch").value = ""; filterSettings(); item.input.reportValidity(); return; }
    }
    const details = Object.entries(changes).map(([key, value]) => {
      const spec = state.fields.get(key).spec;
      return { label: spec.label, text: displayValue(spec.value) + "\n→ " + displayValue(value) };
    });
    if (!await confirmAction("保存这 " + details.length + " 项修改？", "会更新当前配置文件。机器人相关参数在重启后生效，保存后可恢复上一次设置。", "保存设置", details)) return;
    const result = await api("/settings", { method: "PUT", body: { revision: state.settings.revision, changes } });
    renderSettings(result); state.policyLoaded = false; clearPreview(); await loadStatus();
    notify("设置已保存。请重启机器人，使回复与行为参数生效。", "info");
  }));
  $("undoSettings").addEventListener("click", () => run($("undoSettings"), async () => {
    if (!await confirmAction("恢复上一次保存前的设置？", "会恢复最近一次保存前的配置，并放弃当前表单的修改。外部已经修改过配置时，将停止恢复。", "恢复设置")) return;
    const result = await api("/settings/undo", { method: "POST", body: { revision: state.settings.revision } });
    renderSettings(result); state.policyLoaded = false; clearPreview(); await loadStatus(); notify("已恢复上一次保存前的设置。机器人相关参数需重启生效。", "info");
  }));

  $("refreshAll").addEventListener("click", () => run($("refreshAll"), async () => {
    if (state.page === "settings" && Object.keys(settingsChanges()).length && !await confirmAction("重新读取当前配置？", "会放弃尚未保存的表单修改。", "重新读取")) return;
    await loadStatus();
    if (state.page === "settings") await loadSettings();
    if (state.page === "memory") await loadProfiles();
    if (state.page === "backups") await loadBackups();
    if (state.page === "logs") await loadLogs();
    notify("已读取最新数据。");
  }));
  $("shutdownManage").addEventListener("click", () => run($("shutdownManage"), async () => {
    if (!await confirmAction("关闭本地维护台？", "会停止维护页面的本地服务，机器人进程不受影响。", "关闭维护台")) return;
    await api("/shutdown", { method: "POST", body: {} });
    clearInterval(refreshTimer);
    notify("维护台已关闭，可以关闭此页。下次双击启动入口即可重新打开。", "info");
    $("shutdownManage").hidden = true;
  }));
  run(null, loadStatus);
  navigate(location.hash.slice(1) || "overview");
  refreshTimer = setInterval(() => { if (!document.hidden) loadStatus().catch((error) => { $("syncTime").textContent = error.message; }); }, 10000);
})();
