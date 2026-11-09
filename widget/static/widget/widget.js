/* Website widget chat: anonymous visitor, streamed answers, conversation kept in this browser. */
(function () {
  "use strict";
  var config = JSON.parse(document.getElementById("widget-config").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var list = $("messages"), thread = $("thread"), form = $("composer"), prompt = $("prompt"), sendBtn = $("send");
  var busy = false;

  function store(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function randomId() {
    var b = new Uint8Array(18);
    (window.crypto || window.msCrypto).getRandomValues(b);
    return btoa(String.fromCharCode.apply(null, b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  var visitorKey = config.storageKey + ":visitor", convKey = config.storageKey + ":conversation";
  var visitorId = load(visitorKey) || randomId();
  store(visitorKey, visitorId);
  var conversationId = load(convKey);

  $("greeting").textContent = config.greeting;

  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function scroll() { thread.scrollTop = thread.scrollHeight; }

  function addMessage(role, content) {
    var row = el("div", "w-msg " + (role === "user" ? "user" : "bot"));
    var bubble = el("div", "w-bubble");
    if (role === "user") bubble.textContent = content; else bubble.innerHTML = ChatMarkdown.render(content || "");
    row.appendChild(bubble);
    list.appendChild(row);
    return { row: row, bubble: bubble };
  }
  function setMeta(view, sources, faq) {
    var old = view.row.querySelector(".w-meta");
    if (old) old.remove();
    var names = [];
    (sources || []).forEach(function (s) { if (names.indexOf(s.document) === -1) names.push(s.document); });
    if (!names.length && !faq) return;
    var meta = el("div", "w-meta");
    if (faq) meta.appendChild(el("span", null, "From our FAQ"));
    else names.forEach(function (n) { var s = el("span", "w-src", n); s.title = "Source: " + n; meta.appendChild(s); });
    view.row.appendChild(meta);
  }

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson, application/json" },
      body: JSON.stringify(Object.assign({ visitor_id: visitorId }, payload)),
    });
  }

  // Restore this visitor's conversation, if any.
  if (conversationId) {
    post(config.urls.history, { conversation_id: +conversationId })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (data) {
        data.messages.forEach(function (m) {
          var v = addMessage(m.role, m.content);
          if (m.role === "assistant") setMeta(v, m.sources, m.outcome === "faq");
        });
        scroll();
      })
      .catch(function () { conversationId = null; store(convKey, null); });
  }

  function update() { sendBtn.disabled = busy || !prompt.value.trim(); }
  prompt.addEventListener("input", function () {
    prompt.style.height = "auto";
    prompt.style.height = Math.min(prompt.scrollHeight, 120) + "px";
    update();
  });
  prompt.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!busy) form.requestSubmit(); }
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = prompt.value.trim();
    if (!text || busy) return;
    prompt.value = ""; prompt.style.height = "auto";
    busy = true; update();
    addMessage("user", text);
    var view = addMessage("assistant", "");
    view.row.classList.add("w-pending");
    scroll();
    var content = "", sources = [], faq = false;

    function handle(evt) {
      if (evt.type === "meta") { conversationId = evt.conversation_id; store(convKey, String(conversationId)); }
      else if (evt.type === "sources") sources = evt.sources;
      else if (evt.type === "content") {
        content += evt.text;
        view.row.classList.remove("w-pending");
        view.bubble.innerHTML = ChatMarkdown.render(content);
        scroll();
      } else if (evt.type === "replace") {
        content = evt.text;
        view.bubble.innerHTML = ChatMarkdown.render(content);
      } else if (evt.type === "done") faq = !!evt.faq;
      else if (evt.type === "error") throw new Error(evt.error);
    }

    post(config.urls.send, { message: text, conversation_id: conversationId ? +conversationId : null })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (d) {
            if (resp.status === 404 && conversationId) { conversationId = null; store(convKey, null); }
            throw new Error(d.error || "Sorry, something went wrong. Please try again.");
          });
        }
        var reader = resp.body.getReader(), decoder = new TextDecoder(), buffer = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) { if (buffer.trim()) handle(JSON.parse(buffer)); return; }
            buffer += decoder.decode(r.value, { stream: true });
            var lines = buffer.split("\n");
            buffer = lines.pop();
            lines.forEach(function (l) { if (l.trim()) handle(JSON.parse(l)); });
            return pump();
          });
        }
        return pump();
      })
      .then(function () { setMeta(view, sources, faq); })
      .catch(function (err) {
        view.row.classList.remove("w-pending");
        view.row.appendChild(el("div", "w-error", err.message || "Sorry, something went wrong."));
      })
      .then(function () { busy = false; update(); scroll(); prompt.focus(); });
  });

  $("new-chat").addEventListener("click", function () {
    conversationId = null; store(convKey, null); list.textContent = ""; prompt.focus();
  });
  $("close").addEventListener("click", function () {
    // The embedding page (embed.js) closes the panel; no data is sent.
    window.parent.postMessage({ type: "saas-chat:close" }, "*");
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") $("close").click(); });
  prompt.focus();
})();
