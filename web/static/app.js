(() => {
  const state = {
    role: "board",
    conversations: {
      board: { recipient: "ceo", conversationId: null },
      client: { recipient: "sales", conversationId: null },
    },
    events: [],
    selectedConversation: null,
    filterType: "all",
    filterAgent: "",
    feedStickBottom: true,
    experiment: null,
    experiments: [],
  };

  const els = {
    runtimeStatus: document.getElementById("runtime-status"),
    wsStatus: document.getElementById("ws-status"),
    activeExperiment: document.getElementById("active-experiment"),
    agentList: document.getElementById("agent-list"),
    eventFeed: document.getElementById("event-feed"),
    conversationList: document.getElementById("conversation-list"),
    thread: document.getElementById("thread"),
    recipient: document.getElementById("recipient"),
    content: document.getElementById("content"),
    composeMeta: document.getElementById("compose-meta"),
    composeReply: document.getElementById("compose-reply"),
    sendBtn: document.getElementById("send-btn"),
    filterType: document.getElementById("filter-type"),
    filterAgent: document.getElementById("filter-agent"),
    experimentList: document.getElementById("experiment-list"),
    experimentEmpty: document.getElementById("experiment-empty"),
    experimentPanel: document.getElementById("experiment-panel"),
    btnExperiments: document.getElementById("btn-experiments"),
  };

  function setPill(el, ok, label) {
    el.textContent = label;
    el.classList.remove("status-ok", "status-bad", "status-unknown");
    el.classList.add(ok === null ? "status-unknown" : ok ? "status-ok" : "status-bad");
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || res.statusText);
    }
    return data;
  }

  function roleState() {
    return state.conversations[state.role];
  }

  function syncCompose() {
    const rs = roleState();
    els.recipient.value = rs.recipient;
    els.composeMeta.textContent = `Conversation: ${rs.conversationId || "new"}`;
  }

  function eventMatches(ev) {
    const type = ev.type || "";
    if (state.filterType === "message" && !type.startsWith("message.")) return false;
    if (state.filterType === "tool" && !type.startsWith("tool.")) return false;
    if (state.filterType === "turn" && !type.startsWith("turn.")) return false;
    if (state.filterType === "agent" && !type.startsWith("agent.") && type !== "runtime.ready") {
      return false;
    }
    const needle = state.filterAgent.trim().toLowerCase();
    if (!needle) return true;
    const hay = JSON.stringify(ev).toLowerCase();
    return hay.includes(needle);
  }

  function summarizeEvent(ev) {
    const t = ev.type || "event";
    if (t === "message.sent") {
      return `${ev.sender} → ${ev.recipient}: ${(ev.content || "").slice(0, 160)}`;
    }
    if (t === "tool.called") {
      return `${ev.agent_id} ${ev.tool}(${JSON.stringify(ev.args || {}).slice(0, 120)})`;
    }
    if (t === "tool.result") {
      return `${ev.agent_id} ${ev.tool} → ${ev.ok === false ? ev.error : (ev.result_preview || "").slice(0, 140)}`;
    }
    if (t === "turn.started" || t === "turn.ended") {
      return `${ev.agent_id} ${t} from ${ev.from_id}${ev.duration_ms != null ? ` (${ev.duration_ms}ms)` : ""}`;
    }
    if (t === "agent.started" || t === "agent.stopped") {
      return `${ev.agent_id} (${ev.display_name || ""}) ${t.split(".")[1]}`;
    }
    if (t === "runtime.ready") {
      return `ready with ${(ev.agents || []).map((a) => a.agent_id).join(", ")}`;
    }
    return JSON.stringify(ev).slice(0, 180);
  }

  function isFeedNearBottom(threshold = 48) {
    const el = els.eventFeed;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
  }

  function scrollFeedToBottom() {
    const el = els.eventFeed;
    el.scrollTop = el.scrollHeight;
  }

  function renderFeed({ forceStick } = {}) {
    const shouldStick = forceStick === true || (forceStick !== false && state.feedStickBottom);
    const items = state.events.filter(eventMatches).slice(-300);
    els.eventFeed.innerHTML = items
      .map(
        (ev) => `
      <article class="event-item">
        <div><span class="event-type">${escapeHtml(ev.type || "")}</span>
        <span class="when"> ${escapeHtml((ev.timestamp || "").replace("T", " ").slice(0, 19))}</span></div>
        <div class="event-body">${escapeHtml(summarizeEvent(ev))}</div>
      </article>`
      )
      .join("");
    if (shouldStick) {
      // Double rAF so layout settles before measuring scrollHeight.
      requestAnimationFrame(() => {
        scrollFeedToBottom();
        requestAnimationFrame(scrollFeedToBottom);
      });
    }
  }

  function renderAgents(agents) {
    els.agentList.innerHTML = (agents || [])
      .map(
        (a) => `
      <li class="agent-item">
        <div class="agent-id">${escapeHtml(a.agent_id)}</div>
        <div class="agent-meta">${escapeHtml(a.display_name || "")}${a.manager ? ` · mgr ${escapeHtml(a.manager)}` : ""}</div>
        <span class="badge ${a.status === "active" ? "badge-active" : "badge-stopped"}">${escapeHtml(a.status)}</span>
      </li>`
      )
      .join("");
  }

  function renderConversations(conversations) {
    els.conversationList.innerHTML = (conversations || [])
      .map((c) => {
        const active = c.conversation_id === state.selectedConversation ? "active" : "";
        return `
      <li class="conversation-item ${active}" data-id="${escapeAttr(c.conversation_id)}">
        <div class="agent-id">${escapeHtml(c.conversation_id)}</div>
        <div class="conversation-meta">${escapeHtml((c.participants || []).join(", "))}</div>
        <div class="conversation-meta">${c.message_count || 0} msgs · ${escapeHtml((c.last_timestamp || "").replace("T", " ").slice(0, 19))}</div>
      </li>`;
      })
      .join("");
  }

  function renderThread(messages, conversationId) {
    if (!messages || !messages.length) {
      els.thread.innerHTML = `<p class="empty-hint">No messages in ${escapeHtml(conversationId || "")}</p>`;
      return;
    }
    els.thread.innerHTML = messages
      .map(
        (m) => `
      <article class="thread-msg">
        <div>
          <span class="who">${escapeHtml(m.sender)} → ${escapeHtml(m.recipient)}</span>
          <span class="when">${escapeHtml((m.timestamp || "").replace("T", " ").slice(0, 19))}</span>
        </div>
        <div class="body">${escapeHtml(m.content || "")}</div>
      </article>`
      )
      .join("");
    els.thread.scrollTop = els.thread.scrollHeight;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll("'", "&#39;");
  }

  function ingestEvent(ev) {
    if (!ev || !ev.type) return;
    state.events.push(ev);
    if (state.events.length > 800) state.events = state.events.slice(-800);
    renderFeed();
    if (ev.type === "message.sent" || ev.type === "agent.started" || ev.type === "agent.stopped") {
      refreshAgents();
      refreshConversations();
      if (state.selectedConversation && ev.conversation_id === state.selectedConversation) {
        loadThread(state.selectedConversation);
      }
    }
  }

  async function refreshHealth() {
    try {
      const health = await api("/api/health");
      const up = !!health.runtime_connected;
      setPill(els.runtimeStatus, up, up ? "runtime up" : "runtime idle");
      setActiveExperimentLabel(health.experiment || null);
    } catch {
      setPill(els.runtimeStatus, false, "runtime down");
    }
  }

  function setActiveExperimentLabel(name) {
    state.experiment = name || null;
    els.activeExperiment.textContent = name || "no experiment";
    els.activeExperiment.classList.toggle("is-active", !!name);
  }

  function resetConsoleState() {
    state.events = [];
    state.conversations.board.conversationId = null;
    state.conversations.client.conversationId = null;
    state.selectedConversation = null;
    state.feedStickBottom = true;
    syncCompose();
    els.composeReply.hidden = true;
    els.thread.innerHTML = `<p class="empty-hint">Select a conversation</p>`;
    renderFeed({ forceStick: true });
  }

  function renderExperiments(items, active) {
    state.experiments = items || [];
    setActiveExperimentLabel(active || null);
    const empty = !state.experiments.length;
    els.experimentEmpty.hidden = !empty;
    els.experimentList.innerHTML = state.experiments
      .map((exp) => {
        const isActive = !!exp.active || exp.name === active;
        const notes = exp.notes ? `<div class="exp-notes">${escapeHtml(exp.notes)}</div>` : "";
        const meta = [
          exp.created_at ? `created ${escapeHtml(String(exp.created_at).slice(0, 10))}` : null,
          exp.modified_at ? `mod ${escapeHtml(String(exp.modified_at).slice(0, 16).replace("T", " "))}` : null,
        ]
          .filter(Boolean)
          .join(" · ");
        return `
      <li class="experiment-item ${isActive ? "active" : ""}" data-name="${escapeAttr(exp.name)}">
        <div class="exp-main">
          <div class="agent-id">${escapeHtml(exp.name)}${isActive ? " · active" : ""}</div>
          ${notes}
          <div class="conversation-meta">${meta}</div>
        </div>
        <div class="exp-actions">
          <button type="button" class="ghost-btn exp-act" data-act="start" ${isActive ? "disabled" : ""}>Start</button>
          <button type="button" class="ghost-btn exp-act" data-act="rename">Rename</button>
          <button type="button" class="ghost-btn exp-act" data-act="duplicate">Duplicate</button>
          <button type="button" class="ghost-btn exp-act" data-act="export">Export</button>
          <button type="button" class="danger-btn exp-act" data-act="delete">Delete</button>
        </div>
      </li>`;
      })
      .join("");
  }

  async function refreshExperiments() {
    try {
      const data = await api("/api/experiments");
      renderExperiments(data.experiments || [], data.active);
    } catch (err) {
      els.experimentList.innerHTML = `<li class="empty-hint">${escapeHtml(err.message)}</li>`;
    }
  }

  async function refreshAgents() {
    try {
      const data = await api("/api/agents");
      renderAgents(data.agents || []);
    } catch (err) {
      els.agentList.innerHTML = `<li class="empty-hint">${escapeHtml(err.message)}</li>`;
    }
  }

  async function refreshConversations() {
    try {
      const data = await api("/api/conversations");
      renderConversations(data.conversations || []);
    } catch (err) {
      els.conversationList.innerHTML = `<li class="empty-hint">${escapeHtml(err.message)}</li>`;
    }
  }

  async function loadThread(conversationId) {
    state.selectedConversation = conversationId;
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}`);
      renderThread(data.messages || [], conversationId);
      await refreshConversations();
    } catch (err) {
      els.thread.innerHTML = `<p class="empty-hint">${escapeHtml(err.message)}</p>`;
    }
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    setPill(els.wsStatus, null, "feed connecting");
    ws.addEventListener("open", () => setPill(els.wsStatus, true, "feed live"));
    ws.addEventListener("close", () => {
      setPill(els.wsStatus, false, "feed offline");
      setTimeout(connectWs, 1500);
    });
    ws.addEventListener("message", (msg) => {
      let payload;
      try {
        payload = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (payload.type === "hello" && Array.isArray(payload.events)) {
        state.events = payload.events.slice();
        state.feedStickBottom = true;
        renderFeed({ forceStick: true });
        if (payload.experiment) setActiveExperimentLabel(payload.experiment);
        return;
      }
      if (payload.type === "admin") {
        const switchLike = [
          "experiment_start",
          "experiment_stop",
          "experiment_delete",
          "experiment_rename",
          "experiment_create",
        ].includes(payload.action);
        if (switchLike) {
          resetConsoleState();
          refreshExperiments();
          refreshAgents();
          refreshConversations();
          refreshHealth();
        } else if (payload.action === "restart_session") {
          state.events = [];
          state.feedStickBottom = true;
          renderFeed({ forceStick: true });
        } else if (payload.action === "experiment_duplicate") {
          refreshExperiments();
        }
        return;
      }
      if (payload.type === "runtime_event" && payload.event) {
        ingestEvent(payload.event);
      }
    });
    // Keepalive pings so the server receive loop stays active
    setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 25000);
  }

  document.querySelectorAll(".role-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".role-tab").forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      state.role = btn.dataset.role;
      syncCompose();
      els.composeReply.hidden = true;
    });
  });

  els.recipient.addEventListener("change", () => {
    roleState().recipient = els.recipient.value.trim() || roleState().recipient;
  });

  els.filterType.addEventListener("change", () => {
    state.filterType = els.filterType.value;
    renderFeed({ forceStick: state.feedStickBottom });
  });
  els.filterAgent.addEventListener("input", () => {
    state.filterAgent = els.filterAgent.value;
    renderFeed({ forceStick: state.feedStickBottom });
  });

  els.eventFeed.addEventListener("scroll", () => {
    state.feedStickBottom = isFeedNearBottom();
  });

  document.getElementById("refresh-agents").addEventListener("click", refreshAgents);
  document.getElementById("refresh-comms").addEventListener("click", refreshConversations);

  function askConfirm({ title, body, confirmLabel = "Confirm" }) {
    const dialog = document.getElementById("confirm-dialog");
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-body").textContent = body;
    document.getElementById("confirm-ok").textContent = confirmLabel;
    dialog.showModal();
    return new Promise((resolve) => {
      const onClose = () => {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "confirm");
      };
      dialog.addEventListener("close", onClose);
    });
  }

  function askPrompt({ title, body, label = "Value", initial = "", confirmLabel = "OK" }) {
    const dialog = document.getElementById("prompt-dialog");
    const input = document.getElementById("prompt-input");
    document.getElementById("prompt-title").textContent = title;
    document.getElementById("prompt-body").textContent = body;
    document.getElementById("prompt-label").textContent = label;
    document.getElementById("prompt-ok").textContent = confirmLabel;
    input.value = initial;
    dialog.showModal();
    input.focus();
    input.select();
    return new Promise((resolve) => {
      const onClose = () => {
        dialog.removeEventListener("close", onClose);
        if (dialog.returnValue === "confirm") resolve(input.value.trim());
        else resolve(null);
      };
      dialog.addEventListener("close", onClose);
    });
  }

  els.btnExperiments.addEventListener("click", () => {
    const open = els.experimentPanel.hidden;
    els.experimentPanel.hidden = !open;
    els.btnExperiments.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) refreshExperiments();
  });

  document.addEventListener("click", (ev) => {
    const menu = document.getElementById("experiment-menu");
    if (!menu.contains(ev.target) && !els.experimentPanel.hidden) {
      els.experimentPanel.hidden = true;
      els.btnExperiments.setAttribute("aria-expanded", "false");
    }
  });

  document.getElementById("btn-exp-refresh").addEventListener("click", refreshExperiments);

  document.getElementById("experiment-create-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = document.getElementById("exp-name").value.trim();
    const notes = document.getElementById("exp-notes").value.trim();
    const start = document.getElementById("exp-start").checked;
    try {
      await api("/api/experiments", {
        method: "POST",
        body: JSON.stringify({ name, notes, start }),
      });
      document.getElementById("exp-name").value = "";
      document.getElementById("exp-notes").value = "";
      if (start) resetConsoleState();
      await refreshExperiments();
      await refreshHealth();
      await refreshAgents();
      await refreshConversations();
    } catch (err) {
      alert(`Create failed: ${err.message}`);
    }
  });

  els.experimentList.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".exp-act");
    if (!btn) return;
    const item = btn.closest(".experiment-item");
    const name = item?.dataset?.name;
    if (!name) return;
    const act = btn.dataset.act;
    try {
      if (act === "start") {
        const ok = await askConfirm({
          title: `Start experiment “${name}”?`,
          body: "Stops the current session (if any) and loads this experiment. Only one experiment can run at a time.",
          confirmLabel: "Start",
        });
        if (!ok) return;
        await api(`/api/experiments/${encodeURIComponent(name)}/start`, { method: "POST", body: "{}" });
        resetConsoleState();
      } else if (act === "rename") {
        const newName = await askPrompt({
          title: "Rename experiment",
          body: `Rename “${name}” to:`,
          label: "New name",
          initial: name,
          confirmLabel: "Rename",
        });
        if (!newName || newName === name) return;
        await api(`/api/experiments/${encodeURIComponent(name)}/rename`, {
          method: "POST",
          body: JSON.stringify({ new_name: newName }),
        });
      } else if (act === "duplicate") {
        const newName = await askPrompt({
          title: "Duplicate experiment",
          body: `Copy “${name}” (current files and state) to a new experiment:`,
          label: "New name",
          initial: `${name}-copy`,
          confirmLabel: "Duplicate",
        });
        if (!newName) return;
        await api(`/api/experiments/${encodeURIComponent(name)}/duplicate`, {
          method: "POST",
          body: JSON.stringify({ new_name: newName }),
        });
      } else if (act === "export") {
        const res = await fetch(`/api/experiments/${encodeURIComponent(name)}/export`, { method: "POST" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.detail || res.statusText);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${name}.zip`;
        a.click();
        URL.revokeObjectURL(url);
      } else if (act === "delete") {
        const ok = await askConfirm({
          title: `Delete experiment “${name}”?`,
          body: "This permanently removes the experiment folder and cannot be undone. Export first if you need a backup.",
          confirmLabel: "Delete",
        });
        if (!ok) return;
        await api(`/api/experiments/${encodeURIComponent(name)}`, { method: "DELETE" });
        resetConsoleState();
      }
      await refreshExperiments();
      await refreshHealth();
      await refreshAgents();
      await refreshConversations();
    } catch (err) {
      alert(`${act} failed: ${err.message}`);
    }
  });

  document.getElementById("btn-restart-session").addEventListener("click", async () => {
    const ok = await askConfirm({
      title: "Restart AutoGen session?",
      body:
        "This drops in-memory AutoGen assistants and starts a fresh observability session.\n\n" +
        "Conversations, company files, and agent memory are kept.",
      confirmLabel: "Restart session",
    });
    if (!ok) return;
    try {
      await api("/api/admin/restart-session", { method: "POST", body: "{}" });
      state.events = [];
      state.feedStickBottom = true;
      renderFeed({ forceStick: true });
      await refreshAgents();
    } catch (err) {
      alert(`Restart failed: ${err.message}`);
    }
  });

  els.conversationList.addEventListener("click", (ev) => {
    const item = ev.target.closest(".conversation-item");
    if (!item) return;
    loadThread(item.dataset.id);
  });

  document.getElementById("compose-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const rs = roleState();
    const content = els.content.value.trim();
    if (!content) return;
    els.sendBtn.disabled = true;
    try {
      const data = await api("/api/message", {
        method: "POST",
        body: JSON.stringify({
          acting_as: state.role,
          recipient: els.recipient.value.trim(),
          content,
          conversation_id: rs.conversationId,
        }),
      });
      rs.conversationId = data.conversation_id || rs.conversationId;
      rs.recipient = els.recipient.value.trim() || rs.recipient;
      syncCompose();
      els.content.value = "";
      els.composeReply.hidden = false;
      els.composeReply.textContent = data.content || JSON.stringify(data, null, 2);
      await refreshConversations();
      if (rs.conversationId) await loadThread(rs.conversationId);
    } catch (err) {
      els.composeReply.hidden = false;
      els.composeReply.textContent = `Error: ${err.message}`;
    } finally {
      els.sendBtn.disabled = false;
    }
  });

  syncCompose();
  connectWs();
  refreshHealth();
  refreshAgents();
  refreshConversations();
  refreshExperiments();
  setInterval(refreshHealth, 8000);
})();
