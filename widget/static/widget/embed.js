/* SaaS Chat website widget loader.
 * Usage: <script src="https://<host>/static/widget/embed.js" data-key="<public key>" async></script>
 * Adds a chat button; the chat itself runs in an isolated iframe served by <host>. */
(function () {
  "use strict";
  var script = document.currentScript ||
    document.querySelector('script[src*="/widget/embed.js"][data-key]');
  if (!script || !script.dataset.key || window.__saasChatWidgetLoaded) return;
  window.__saasChatWidgetLoaded = true;

  var key = script.dataset.key;
  var origin = new URL(script.src, location.href).origin;
  var base = origin + "/w/" + encodeURIComponent(key) + "/";

  fetch(base + "config.json", { credentials: "omit" })
    .then(function (r) { return r.ok ? r.json() : { enabled: false }; })
    .then(function (cfg) { if (cfg.enabled) mount(cfg); })
    .catch(function () { /* widget unavailable: show nothing */ });

  function mount(cfg) {
    var host = document.createElement("div");
    host.setAttribute("data-saas-chat-widget", "");
    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
    var side = cfg.position === "left" ? "left" : "right";
    var style = document.createElement("style");
    style.textContent =
      ":host{all:initial}" +
      ".wrap{position:fixed;bottom:20px;" + side + ":20px;z-index:2147483000;font-family:system-ui,-apple-system,'Segoe UI',sans-serif}" +
      ".btn{width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;display:grid;place-items:center;" +
      "background:" + cfg.color + ";color:" + cfg.textColor + ";box-shadow:0 6px 20px rgba(0,0,0,.22);transition:transform .15s}" +
      ".btn:hover{transform:scale(1.05)}.btn:focus-visible{outline:3px solid " + cfg.color + ";outline-offset:3px}" +
      ".panel{position:absolute;bottom:70px;" + side + ":0;width:380px;height:600px;max-height:calc(100vh - 110px);" +
      "max-width:calc(100vw - 40px);border-radius:16px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.25);" +
      "background:#fff;display:none}" +
      ".panel.open{display:block}iframe{width:100%;height:100%;border:0;display:block}" +
      "@media (max-width:480px){.panel.open{position:fixed;inset:0;width:100vw;height:100%;max-width:none;max-height:none;border-radius:0}" +
      ".wrap.is-open .btn{display:none}}" +
      "@media (prefers-reduced-motion:reduce){.btn{transition:none}}";
    var wrap = document.createElement("div");
    wrap.className = "wrap";
    var panel = document.createElement("div");
    panel.className = "panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", cfg.title);
    var btn = document.createElement("button");
    btn.className = "btn";
    btn.type = "button";
    btn.setAttribute("aria-label", "Open chat: " + cfg.title);
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M4 5h16v11H9l-5 4z"/></svg>';
    var frame = null;

    function open(state) {
      if (state && !frame) {
        frame = document.createElement("iframe");
        frame.src = base;
        frame.title = cfg.title;
        frame.setAttribute("allow", "clipboard-write");
        panel.appendChild(frame);
      }
      panel.classList.toggle("open", state);
      wrap.classList.toggle("is-open", state);
      btn.setAttribute("aria-expanded", String(state));
      btn.setAttribute("aria-label", (state ? "Close chat: " : "Open chat: ") + cfg.title);
      if (state && frame) setTimeout(function () { try { frame.focus(); } catch (e) {} }, 50);
      if (!state) btn.focus();
    }
    btn.addEventListener("click", function () { open(!panel.classList.contains("open")); });
    window.addEventListener("message", function (e) {
      if (e.origin === origin && e.data && e.data.type === "saas-chat:close") open(false);
    });

    wrap.appendChild(panel);
    wrap.appendChild(btn);
    root.appendChild(style);
    root.appendChild(wrap);
    (document.body || document.documentElement).appendChild(host);
  }
})();
