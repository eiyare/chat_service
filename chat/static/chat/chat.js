(function () {
  "use strict";

  var config = JSON.parse(document.getElementById("chat-config").textContent);
  var initialMessages = JSON.parse(document.getElementById("chat-messages").textContent);
  var csrfToken = document.querySelector("#composer [name=csrfmiddlewaretoken]").value;
  var MODEL_KEY = "chat:model";

  var $ = function (id) { return document.getElementById(id); };
  var thread = $("thread"), list = $("messages"), emptyState = $("empty-state");
  var form = $("composer"), prompt = $("prompt"), sendBtn = $("send");
  var modelSelect = $("model-select"), statusEl = $("status");
  var convoList = $("convo-list");

  var state = { conversationId: config.conversationId, controller: null, modelsReady: false };

  /* ---------- helpers ---------- */

  function storageGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function storageSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

  function nearBottom() { return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120; }
  function scrollToBottom(force) { if (force || nearBottom()) thread.scrollTop = thread.scrollHeight; }

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.classList.toggle("error", !!isError);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ---------- message rendering ---------- */

  function addMessage(msg) {
    emptyState.hidden = true;
    var row = el("div", "msg " + msg.role);
    var bubble = el("div", "bubble");
    var body = el("div", "body");
    var view = { row: row, body: body, thinking: null, thinkingBody: null, meta: null };

    if (msg.role === "assistant") {
      if (msg.thinking) ensureThinking(view, bubble).textContent = msg.thinking;
      body.innerHTML = ChatMarkdown.render(msg.content || "");
    } else {
      body.textContent = msg.content;
    }
    bubble.appendChild(body);
    row.appendChild(bubble);
    if (msg.role === "assistant") {
      view.meta = el("div", "meta", msg.outcome === "faq" ? faqText(msg.stats) : statsText(msg.model, msg.stats));
      row.appendChild(view.meta);
    }
    view.bubble = bubble;
    if (msg.role === "assistant" && msg.sources && msg.sources.length) renderSources(view, msg.sources);
    list.appendChild(row);
    return view;
  }

  // Group passages by document: one chip per document, listing passage numbers.
  function renderSources(view, sources) {
    if (view.sources) view.sources.remove();
    view.sources = null;
    if (!sources.length) return;
    var byDoc = {};
    var order = [];
    sources.forEach(function (s) {
      if (!byDoc[s.document_id]) {
        byDoc[s.document_id] = { name: s.document, ns: [], question: s.question };
        order.push(s.document_id);
      }
      byDoc[s.document_id].ns.push(s.n);
    });
    var wrap = el("div", "sources");
    wrap.appendChild(el("span", "sources-label", "Sources"));
    order.forEach(function (id) {
      var d = byDoc[id];
      var chip = el("span", "src", d.name);
      chip.title = d.question ? "FAQ: " + d.question : "Passage" + (d.ns.length > 1 ? "s " : " ") + d.ns.join(", ");
      chip.insertBefore(el("span", "src-n", d.ns.map(function (n) { return "[" + n + "]"; }).join("")), chip.firstChild);
      wrap.appendChild(chip);
    });
    view.row.insertBefore(wrap, view.meta);
    view.sources = wrap;
  }

  // "model · 4.2 s · 1,234 in / 56 out · 1,290 tokens"; fixed replies show "no model call".
  function statsText(model, stats) {
    if (!stats) return model ? labelOf(model) : "";
    var parts = [];
    if (model) parts.push(labelOf(model));
    if (stats.response_ms != null) parts.push((stats.response_ms / 1000).toFixed(1) + " s");
    if (stats.total_tokens != null) {
      var fmt = function (n) { return n == null ? "?" : n.toLocaleString(); };
      parts.push(fmt(stats.prompt_tokens) + " in / " + fmt(stats.completion_tokens) + " out");
      parts.push(fmt(stats.total_tokens) + " tokens");
    } else if (!model) {
      parts.push("no model call");
    }
    return parts.join(" \u00b7 ");
  }

  function faqText(stats) {
    var t = "Answered from FAQ";
    if (stats && stats.response_ms != null) t += " \u00b7 " + (stats.response_ms / 1000).toFixed(1) + " s";
    return t + " \u00b7 no model call";
  }

  function ensureThinking(view, bubble) {
    if (!view.thinking) {
      var d = el("details", "thinking");
      d.appendChild(el("summary", null, "Thinking"));
      view.thinkingBody = el("div", "thinking-body");
      d.appendChild(view.thinkingBody);
      (bubble || view.bubble).insertBefore(d, view.body);
      view.thinking = d;
    }
    return view.thinkingBody;
  }

  /* ---------- models ---------- */

  // The model list is the server-side allowlist (admin / LLM_CHAT_MODELS).
  function labelOf(id) {
    for (var i = 0; i < config.models.length; i++) if (config.models[i].id === id) return config.models[i].label;
    return id;
  }

  function loadModels() {
    modelSelect.innerHTML = "";
    if (!config.models.length) {
      modelSelect.appendChild(el("option", null, "No models"));
      setStatus("No chat model available. An administrator needs to enable one (Django admin > LLM models).", true);
      return;
    }
    var ids = config.models.map(function (m) { return m.id; });
    config.models.forEach(function (m) {
      var opt = el("option", null, m.label);
      opt.value = m.id;
      modelSelect.appendChild(opt);
    });
    var preferred = [config.conversationModel, storageGet(MODEL_KEY), config.defaultModel];
    for (var i = 0; i < preferred.length; i++) {
      if (preferred[i] && ids.indexOf(preferred[i]) !== -1) { modelSelect.value = preferred[i]; break; }
    }
    modelSelect.disabled = false;
    state.modelsReady = true;
    updateSendButton();
  }

  modelSelect.addEventListener("change", function () { storageSet(MODEL_KEY, modelSelect.value); });

  /* ---------- composer ---------- */

  function autosize() {
    prompt.style.height = "auto";
    prompt.style.height = Math.min(prompt.scrollHeight, 220) + "px";
  }

  function updateSendButton() {
    var busy = !!state.controller;
    sendBtn.classList.toggle("stop", busy);
    sendBtn.setAttribute("aria-label", busy ? "Stop" : "Send");
    sendBtn.disabled = !busy && (!state.modelsReady || !prompt.value.trim());
  }

  prompt.addEventListener("input", function () { autosize(); updateSendButton(); });
  prompt.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!state.controller) form.requestSubmit();
    }
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (state.controller) { state.controller.abort(); return; }
    var text = prompt.value.trim();
    if (!text || !state.modelsReady) return;
    prompt.value = "";
    autosize();
    send(text, modelSelect.value);
  });

  /* ---------- sidebar ---------- */

  function addToSidebar(id, title, url) {
    var empty = $("convo-empty");
    if (empty) empty.remove();
    Array.prototype.forEach.call(convoList.querySelectorAll(".convo.active"), function (n) { n.classList.remove("active"); });
    var item = el("div", "convo active");
    item.dataset.id = id;
    var a = el("a", null, title);
    a.href = url;
    var del = el("button", "convo-del", "×");
    del.type = "button";
    del.dataset.id = id;
    del.setAttribute("aria-label", "Delete conversation");
    item.appendChild(a);
    item.appendChild(del);
    convoList.insertBefore(item, convoList.firstChild);
  }

  function bumpInSidebar(id) {
    var item = convoList.querySelector('.convo[data-id="' + id + '"]');
    if (item && item !== convoList.firstElementChild) convoList.insertBefore(item, convoList.firstChild);
  }

  convoList.addEventListener("click", function (e) {
    var btn = e.target.closest(".convo-del");
    if (!btn) return;
    e.preventDefault();
    if (!confirm("Delete this conversation?")) return;
    var id = btn.dataset.id;
    fetch(config.urls.deleteTemplate.replace("/0/", "/" + id + "/"), {
      method: "POST",
      headers: { "X-CSRFToken": csrfToken },
    }).then(function (r) {
      if (!r.ok) throw new Error();
      if (String(state.conversationId) === String(id)) { window.location.href = config.urls.home; return; }
      var item = btn.closest(".convo");
      if (item) item.remove();
    }).catch(function () { setStatus("Could not delete conversation", true); });
  });

  var shell = $("shell"), scrim = $("scrim");
  function toggleSidebar(open) { shell.classList.toggle("sidebar-open", open); scrim.hidden = !open; }
  $("open-sidebar").addEventListener("click", function () { toggleSidebar(true); });
  $("close-sidebar").addEventListener("click", function () { toggleSidebar(false); });
  scrim.addEventListener("click", function () { toggleSidebar(false); });

  /* ---------- copy code ---------- */

  list.addEventListener("click", function (e) {
    var btn = e.target.closest(".copy-code");
    if (!btn) return;
    var code = btn.parentElement.querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(function () {
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = "Copy"; }, 1500);
    });
  });

  /* ---------- streaming ---------- */

  function send(text, model) {
    addMessage({ role: "user", content: text });
    var view = addMessage({ role: "assistant", content: "", model: model });
    view.row.classList.add("pending");
    scrollToBottom(true);

    var controller = new AbortController();
    state.controller = controller;
    updateSendButton();
    setStatus("");

    var content = "", thinking = "", frame = null;
    function paint() {
      frame = null;
      var stick = nearBottom();
      if (thinking) ensureThinking(view).textContent = thinking;
      if (content) { view.row.classList.remove("pending"); view.body.innerHTML = ChatMarkdown.render(content); }
      if (stick) scrollToBottom(true);
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(paint); }

    function handle(evt) {
      switch (evt.type) {
        case "meta":
          if (!state.conversationId) {
            state.conversationId = evt.conversation_id;
            history.replaceState(null, "", evt.url);
            addToSidebar(evt.conversation_id, evt.title, evt.url);
          } else {
            bumpInSidebar(evt.conversation_id);
          }
          break;
        case "sources": renderSources(view, evt.sources); break;
        case "thinking": thinking += evt.text; schedule(); break;
        case "content": content += evt.text; schedule(); break;
        case "replace": content = evt.text; schedule(); break;
        case "done":
          // Fixed replies (no relevant passages) never reach a model.
          view.meta.textContent = evt.faq ? faqText(evt.stats) : statsText(evt.grounded ? model : "", evt.stats);
          break;
        case "error": showError(evt.error); break;
      }
    }

    function showError(message) {
      view.row.classList.remove("pending");
      view.row.classList.add("errored");
      view.bubble.appendChild(el("div", "msg-error", message));
    }

    fetch(config.urls.send, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken, Accept: "application/x-ndjson" },
      body: JSON.stringify({ message: text, model: model, conversation_id: state.conversationId }),
      signal: controller.signal,
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          throw new Error(data.error || "Request failed (" + resp.status + ")");
        });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            if (buffer.trim()) handle(JSON.parse(buffer));
            return;
          }
          buffer += decoder.decode(r.value, { stream: true });
          var lines = buffer.split("\n");
          buffer = lines.pop();
          lines.forEach(function (line) { if (line.trim()) handle(JSON.parse(line)); });
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err.name === "AbortError") {
        view.meta.textContent = model + " · stopped";
      } else {
        showError(err.message || "Something went wrong");
      }
    }).then(function () {
      if (frame) { cancelAnimationFrame(frame); }
      paint();
      if (!content && !thinking && !view.row.classList.contains("errored")) view.row.classList.remove("pending");
      if (!content && view.row.classList.contains("pending")) view.row.classList.remove("pending");
      state.controller = null;
      updateSendButton();
      prompt.focus();
    });
  }

  /* ---------- init ---------- */

  initialMessages.forEach(addMessage);
  scrollToBottom(true);
  loadModels();
  autosize();
  prompt.focus();
})();
