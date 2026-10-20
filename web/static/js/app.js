/* =====================================================================
   网页群聊测试台 · 原生 JS（无框架、无 CDN、离线可用）
   职责：拉配置摘要 / 渲染仿微信气泡 / 发送消息 / 重置演示
   ===================================================================== */
(function () {
  "use strict";

  var els = {
    phone: document.getElementById("phone"),
    groupName: document.getElementById("groupName"),
    headerSub: document.getElementById("headerSub"),
    resetBtn: document.getElementById("resetBtn"),
    chipModel: document.getElementById("chipModel"),
    chipTrigger: document.getElementById("chipTrigger"),
    chipKey: document.getElementById("chipKey"),
    sideBotName: document.getElementById("sideBotName"),
    sideTrigger: document.getElementById("sideTrigger"),
    sideContext: document.getElementById("sideContext"),
    sideTokens: document.getElementById("sideTokens"),
    chatBody: document.getElementById("chatBody"),
    messages: document.getElementById("messages"),
    emptyState: document.getElementById("emptyState"),
    noticeBar: document.getElementById("noticeBar"),
    noticeText: document.getElementById("noticeText"),
    errorBar: document.getElementById("errorBar"),
    errorText: document.getElementById("errorText"),
    retryBtn: document.getElementById("retryBtn"),
    errorClose: document.getElementById("errorClose"),
    speakerSelect: document.getElementById("speakerSelect"),
    atBtn: document.getElementById("atBtn"),
    input: document.getElementById("messageInput"),
    sendBtn: document.getElementById("sendBtn"),
  };

  var state = {
    status: null,
    messages: [],
    sending: false,
    lastRetry: null,
    noticeTimer: null,
    resetTimer: null,
    typingEl: null,
    baseSub: "3 人 · 离线模拟",
  };

  /* ------------------------------------------------------------ 网络请求 */
  function ApiError(message, status, data) {
    this.name = "ApiError";
    this.message = message;
    this.status = status;
    this.data = data;
  }
  ApiError.prototype = Object.create(Error.prototype);

  async function api(path, options) {
    var opts = options || {};
    var res;
    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 240000);
    try {
      res = await fetch(path, {
        method: opts.method || "GET",
        headers: { "Content-Type": "application/json" },
        body: opts.body,
        cache: "no-store",
        signal: controller.signal,
      });
    } catch (err) {
      if (err.name === "AbortError") {
        throw new ApiError("本次等待超时，请先刷新查看回复，避免重复发送", 0, null);
      }
      throw new ApiError("连不上本地服务，请确认网页测试台还在运行", 0, null);
    } finally {
      clearTimeout(timeoutId);
    }
    var data = null;
    try {
      data = await res.json();
    } catch (err) {
      data = null;
    }
    if (!res.ok) {
      throw new ApiError(
        (data && data.error) || "请求失败（HTTP " + res.status + "）",
        res.status,
        data
      );
    }
    return data || {};
  }

  /* ------------------------------------------------------------ 渲染：配置摘要 */
  function setChip(el, label, value, tone) {
    el.className = "chip" + (tone ? " " + tone : "");
    el.textContent = "";
    el.appendChild(document.createTextNode(label + " · "));
    var b = document.createElement("b");
    b.textContent = value;
    el.appendChild(b);
  }

  function renderStatus(status) {
    state.status = status;
    var room = status.room || {};
    var bot = status.bot || {};
    var trigger = status.trigger || {};
    var ctx = status.context || {};
    if (els.sideTokens) els.sideTokens.textContent = Number((status.usage_today || {}).total_tokens || 0).toLocaleString();
    var members = Array.isArray(room.members) ? room.members : ["我", "朋友", "DeepSeek"];

    var roomName = room.name || "我、朋友和 DeepSeek";
    els.groupName.textContent = roomName;
    document.title = roomName + " · 网页群聊测试台";
    els.sideBotName.textContent = bot.name || "DeepSeek";
    state.baseSub = members.length + " 人 · 离线模拟";
    setSubtitle(false);

    setChip(els.chipModel, "模型", bot.model || "未配置");
    els.chipModel.title = "接口地址：" + (bot.base_url || "-");
    var triggerValue = trigger.label || trigger.mode || "未配置";
    setChip(els.chipTrigger, "触发", triggerValue);
    els.chipTrigger.title = "模式：" + (trigger.mode || "-");
    setChip(
      els.chipKey,
      "API Key",
      status.api_key_configured ? "已配置" : "未配置",
      status.api_key_configured ? "ok" : "warn"
    );
    if (status.api_key_configured && status.api_key_hint) {
      els.chipKey.title = "已检测到 Key：" + status.api_key_hint;
    } else {
      els.chipKey.title = "未检测到 DeepSeek API Key，发送消息会失败";
    }

    els.sideTrigger.textContent = triggerValue;
    els.sideContext.textContent = ctx.window_messages
      ? "最近 " + ctx.window_messages + " 条对话"
      : "按默认窗口";
    els.sideTrigger.title = trigger.mode || "";
  }

  function setSubtitle(typing) {
    if (!els.headerSub) return;
    els.headerSub.textContent = typing ? "对方正在输入…" : state.baseSub;
  }

  /* ------------------------------------------------------------ 渲染：消息 */
  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function clockNow(date) {
    var d = date || new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function formatDivider(ts) {
    var d = new Date(ts * 1000);
    var now = new Date();
    var hm = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    if (
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    ) {
      return hm;
    }
    return d.getMonth() + 1 + "月" + d.getDate() + "日 " + hm;
  }

  function makeAvatar(kind) {
    var avatar = document.createElement("div");
    avatar.className =
      "avatar " + (kind === "bot" ? "avatar-bot" : kind === "me" ? "avatar-me" : "avatar-friend");
    avatar.textContent = kind === "bot" ? "DS" : kind === "me" ? "我" : "友";
    avatar.setAttribute("aria-hidden", "true");
    return avatar;
  }

  function makeName(kind, speaker) {
    var name = document.createElement("div");
    name.className = "msg-name";
    name.appendChild(document.createTextNode(speaker || (kind === "bot" ? "DeepSeek" : "朋友")));
    if (kind === "bot") {
      var tag = document.createElement("span");
      tag.className = "ai-tag";
      tag.textContent = "AI";
      name.appendChild(tag);
    }
    return name;
  }

  function renderRow(m) {
    var kind = m.role === "bot" ? "bot" : m.speaker === "我" ? "me" : "friend";
    var row = document.createElement("div");
    row.className = "msg-row " + kind;
    row.dataset.id = String(m.id);
    if (m.failed) row.classList.add("failed-row");

    var col = document.createElement("div");
    col.className = "msg-col";

    if (kind !== "me") {
      col.appendChild(makeName(kind, m.speaker));
    }

    var bubble = document.createElement("div");
    bubble.className = "bubble" + (m.failed ? " failed" : "");
    if (kind === "bot" && m.at) {
      var mention = document.createElement("span");
      mention.className = "mention";
      mention.textContent = m.at;
      bubble.appendChild(mention);
      bubble.appendChild(document.createTextNode(" "));
    }
    bubble.appendChild(document.createTextNode(m.text || ""));
    bubble.title = m.time || "";
    col.appendChild(bubble);

    var ts = document.createElement("div");
    ts.className = "msg-ts";
    ts.textContent = m.time || "";
    col.appendChild(ts);

    if (m.failed) {
      var mark = document.createElement("span");
      mark.className = "fail-mark";
      mark.textContent = "!";
      mark.title = "这条消息没有发送成功";
      col.appendChild(mark);
    }

    if (kind === "me") {
      row.appendChild(col);
      row.appendChild(makeAvatar("me"));
    } else {
      row.appendChild(makeAvatar(kind));
      row.appendChild(col);
    }
    return row;
  }

  function renderMessages(forceScroll) {
    var body = els.chatBody;
    var nearBottom =
      body.scrollHeight - body.scrollTop - body.clientHeight < 160;

    els.messages.textContent = "";
    if (!state.messages.length) {
      els.emptyState.hidden = false;
    } else {
      els.emptyState.hidden = true;
      var frag = document.createDocumentFragment();
      var prev = null;
      for (var i = 0; i < state.messages.length; i++) {
        var m = state.messages[i];
        if (!prev || (m.ts || 0) - (prev.ts || 0) > 120) {
          var divider = document.createElement("div");
          divider.className = "day-divider";
          divider.textContent = formatDivider(m.ts || Date.now() / 1000);
          frag.appendChild(divider);
        }
        frag.appendChild(renderRow(m));
        prev = m;
      }
      els.messages.appendChild(frag);
    }

    if (forceScroll || nearBottom) {
      scrollToBottom();
    }
  }

  function scrollToBottom() {
    window.requestAnimationFrame(function () {
      els.chatBody.scrollTop = els.chatBody.scrollHeight;
    });
  }

  /* ------------------------------------------------------------ 加载中 / 提示条 */
  function showTyping() {
    hideTyping();
    var row = document.createElement("div");
    row.className = "msg-row bot typing-row";
    row.appendChild(makeAvatar("bot"));
    var col = document.createElement("div");
    col.className = "msg-col";
    col.appendChild(makeName("bot", (state.status && state.status.bot && state.status.bot.name) || "DeepSeek"));
    var bubble = document.createElement("div");
    bubble.className = "bubble typing-bubble";
    var label = document.createElement("span");
    label.className = "typing-text";
    label.textContent = "对方正在输入…";
    bubble.appendChild(label);
    var dots = document.createElement("span");
    dots.className = "dots";
    for (var i = 0; i < 3; i++) {
      dots.appendChild(document.createElement("i"));
    }
    bubble.appendChild(dots);
    col.appendChild(bubble);
    row.appendChild(col);
    els.messages.appendChild(row);
    state.typingEl = row;
    setSubtitle(true);
    scrollToBottom();
  }

  function hideTyping() {
    if (state.typingEl) {
      state.typingEl.remove();
      state.typingEl = null;
    }
    setSubtitle(false);
  }

  function showNotice(text, ms) {
    if (state.noticeTimer) {
      window.clearTimeout(state.noticeTimer);
      state.noticeTimer = null;
    }
    els.noticeText.textContent = text;
    els.noticeBar.hidden = false;
    state.noticeTimer = window.setTimeout(function () {
      els.noticeBar.hidden = true;
    }, ms || 5000);
  }

  function hideNotice() {
    if (state.noticeTimer) {
      window.clearTimeout(state.noticeTimer);
      state.noticeTimer = null;
    }
    els.noticeBar.hidden = true;
  }

  function showError(message, retryPayload) {
    state.lastRetry = retryPayload || null;
    var text = String(message || "发送失败，请稍后重试");
    els.errorText.textContent = text.length > 180 ? text.slice(0, 180) + "…" : text;
    els.errorText.title = text;
    els.retryBtn.hidden = !state.lastRetry;
    els.errorBar.hidden = false;
  }

  function hideError() {
    els.errorBar.hidden = true;
    state.lastRetry = null;
  }

  /* ------------------------------------------------------------ 发送 */
  function setSending(on) {
    els.sendBtn.disabled = on;
    els.sendBtn.classList.toggle("sending", on);
    els.sendBtn.setAttribute("aria-busy", on ? "true" : "false");
    if (els.atBtn) els.atBtn.disabled = on;
  }

  async function refreshMessages() {
    var data = await api("/api/messages");
    state.messages = Array.isArray(data.messages) ? data.messages : [];
    renderMessages(true);
  }

  async function send(textArg, speakerArg) {
    if (state.sending) return;
    var speaker = speakerArg || els.speakerSelect.value || "我";
    var content = (typeof textArg === "string" ? textArg : els.input.value).trim();
    if (!content) {
      els.input.focus();
      return;
    }

    state.sending = true;
    hideError();
    hideNotice();
    els.input.value = "";
    autoGrow();

    // 乐观渲染：自己发的消息立刻出现（和微信一样），不用等 DeepSeek 返回
    var now = new Date();
    state.messages = state.messages.concat([
      {
        id: "pending-" + now.getTime(),
        role: "user",
        speaker: speaker,
        text: content,
        time: clockNow(now),
        ts: now.getTime() / 1000,
        failed: false,
        pending: true,
      },
    ]);
    renderMessages(true);
    showTyping();
    setSending(true);

    try {
      var data = await api("/api/send", {
        method: "POST",
        body: JSON.stringify({ speaker: speaker, text: content }),
      });
      state.messages = Array.isArray(data.messages) ? data.messages : [];
      renderMessages(true);
      if (data.replied === false) {
        var suffix = data.trigger_label ? "（当前规则：" + data.trigger_label + "）" : "";
        showNotice("DeepSeek 这次没接话：" + (data.reason || "未知原因") + suffix);
      }
    } catch (err) {
      var replaced = false;
      if (err && err.data && Array.isArray(err.data.messages)) {
        state.messages = err.data.messages;
        replaced = true;
      } else {
        try {
          await refreshMessages();
          replaced = true;
        } catch (ignored) {
          /* 刷新也失败：把乐观渲染的临时消息撤掉 */
        }
      }
      if (!replaced) {
        state.messages = state.messages.filter(function (m) {
          return String(m.id).indexOf("pending-") !== 0;
        });
      }
      renderMessages(true);
      els.input.value = content;
      autoGrow();
      showError((err && err.message) || "发送失败，请稍后重试", {
        speaker: speaker,
        text: content,
      });
    } finally {
      hideTyping();
      state.sending = false;
      setSending(false);
      api("/api/status").then(renderStatus).catch(function () {});
      els.input.focus();
    }
  }

  /* ------------------------------------------------------------ 输入框 */
  function autoGrow() {
    var el = els.input;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  }

  function syncSpeakerPlaceholder() {
    var who = els.speakerSelect.value || "我";
    els.input.placeholder = "以「" + who + "」的身份说点什么…";
  }

  function insertMention() {
    var botName = (state.status && state.status.bot && state.status.bot.name) || "DeepSeek";
    var token = "@" + botName + " ";
    var input = els.input;
    var start = typeof input.selectionStart === "number" ? input.selectionStart : input.value.length;
    var end = typeof input.selectionEnd === "number" ? input.selectionEnd : input.value.length;
    var before = input.value.slice(0, start);
    var after = input.value.slice(end);
    var glue = before && !/\s$/.test(before) ? " " : "";
    input.value = before + glue + token + after;
    var pos = input.value.length - after.length;
    input.setSelectionRange(pos, pos);
    input.focus();
    autoGrow();
  }

  /* ------------------------------------------------------------ 重置 */
  function doReset() {
    if (state.sending) return;
    api("/api/reset", { method: "POST" })
      .then(function () {
        state.messages = [];
        renderMessages(true);
        hideError();
        hideNotice();
        showNotice("已清空聊天记录和上下文，重新开始吧", 3000);
        els.input.focus();
      })
      .catch(function (err) {
        showError("重置失败：" + ((err && err.message) || "请稍后重试"));
      });
  }

  function onResetClick() {
    if (!els.resetBtn.classList.contains("arming")) {
      els.resetBtn.classList.add("arming");
      showNotice("再点一次「重置」按钮，清空聊天记录和上下文", 4000);
      if (state.resetTimer) window.clearTimeout(state.resetTimer);
      state.resetTimer = window.setTimeout(function () {
        els.resetBtn.classList.remove("arming");
      }, 4000);
      return;
    }
    if (state.resetTimer) window.clearTimeout(state.resetTimer);
    els.resetBtn.classList.remove("arming");
    doReset();
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bindEvents() {
    els.sendBtn.addEventListener("click", function () {
      send();
    });

    els.input.addEventListener("input", autoGrow);

    els.input.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.shiftKey) return;
      if (e.isComposing || e.keyCode === 229) return; // 中文输入法候选阶段
      e.preventDefault();
      send();
    });

    els.speakerSelect.addEventListener("change", function () {
      syncSpeakerPlaceholder();
      els.input.focus();
    });

    if (els.atBtn) {
      els.atBtn.addEventListener("click", insertMention);
    }

    var samples = document.querySelectorAll(".sample");
    for (var i = 0; i < samples.length; i++) {
      samples[i].addEventListener("click", function () {
        var speaker = this.getAttribute("data-speaker") || "朋友";
        var text = this.getAttribute("data-text") || "";
        els.speakerSelect.value = speaker;
        syncSpeakerPlaceholder();
        send(text, speaker);
      });
    }

    els.resetBtn.addEventListener("click", onResetClick);
    els.errorClose.addEventListener("click", hideError);
    els.retryBtn.addEventListener("click", function () {
      if (!state.lastRetry) {
        hideError();
        return;
      }
      var payload = state.lastRetry;
      hideError();
      send(payload.text, payload.speaker);
    });

    window.addEventListener("resize", function () {
      autoGrow();
    });
  }

  /* ------------------------------------------------------------ 启动 */
  async function boot() {
    bindEvents();
    syncSpeakerPlaceholder();
    autoGrow();
    try {
      var results = await Promise.all([api("/api/status"), api("/api/messages")]);
      renderStatus(results[0] || {});
      state.messages = (results[1] && Array.isArray(results[1].messages) && results[1].messages) || [];
      renderMessages(true);
    } catch (err) {
      showError(
        "无法连接本地测试台：" +
          ((err && err.message) || "未知错误") +
          "。请确认已在项目目录运行 python -m src.main web",
        null
      );
    }
    els.input.focus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
