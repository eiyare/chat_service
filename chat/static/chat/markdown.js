/*
 * Minimal, safe Markdown renderer for chat messages.
 * All text is HTML-escaped before formatting, and links only allow
 * http(s)/mailto, so model output can never inject markup or scripts.
 * Supports: fenced code, inline code, headings, lists, blockquotes,
 * horizontal rules, bold, italic, strikethrough, links, autolinks.
 * Tolerates unterminated fences so partial (streaming) text renders well.
 */
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function safeUrl(url) {
    // url is already HTML-escaped; check the scheme on the decoded form.
    var raw = url.replace(/&amp;/g, "&").trim();
    return /^(https?:|mailto:)/i.test(raw) ? url : null;
  }

  function inline(text) {
    var codes = [];
    var out = escapeHtml(text).replace(/`([^`\n]+)`/g, function (_, code) {
      codes.push("<code>" + code + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    out = out
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, label, url) {
        var safe = safeUrl(url);
        return safe ? '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + label + "</a>" : m;
      })
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, function (_, pre, url) {
        return pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>";
      })
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return out.replace(/\u0000(\d+)\u0000/g, function (_, i) { return codes[+i]; });
  }

  function blocks(text) {
    var lines = text.split("\n");
    var html = [];
    var para = [];
    var list = null; // {type: "ul"|"ol", items: []}
    var quote = [];

    function flushPara() {
      if (para.length) { html.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
    }
    function flushList() {
      if (list) {
        html.push("<" + list.type + ">" + list.items.map(function (i) { return "<li>" + inline(i) + "</li>"; }).join("") + "</" + list.type + ">");
        list = null;
      }
    }
    function flushQuote() {
      if (quote.length) { html.push("<blockquote>" + blocks(quote.join("\n")) + "</blockquote>"); quote = []; }
    }
    function flushAll() { flushPara(); flushList(); flushQuote(); }

    lines.forEach(function (line) {
      var m;
      if (/^\s*>\s?/.test(line)) { flushPara(); flushList(); quote.push(line.replace(/^\s*>\s?/, "")); return; }
      flushQuote();
      if (!line.trim()) { flushPara(); flushList(); return; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushAll();
        var level = Math.min(m[1].length + 2, 6); // keep headings modest inside a chat bubble
        html.push("<h" + level + ">" + inline(m[2]) + "</h" + level + ">");
        return;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushAll(); html.push("<hr>"); return; }
      if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
        flushPara();
        if (!list || list.type !== "ul") { flushList(); list = { type: "ul", items: [] }; }
        list.items.push(m[1]); return;
      }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushPara();
        if (!list || list.type !== "ol") { flushList(); list = { type: "ol", items: [] }; }
        list.items.push(m[1]); return;
      }
      if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += " " + line.trim(); return; }
      flushList();
      para.push(line);
    });
    flushAll();
    return html.join("");
  }

  function render(src) {
    var out = [];
    var re = /```([\w+#.-]*)[^\n]*\n([\s\S]*?)(?:```|$)/g;
    var last = 0, m;
    while ((m = re.exec(src)) !== null) {
      if (m.index > last) out.push(blocks(src.slice(last, m.index)));
      var lang = m[1] ? '<span class="code-lang">' + escapeHtml(m[1]) + "</span>" : "";
      out.push('<div class="code-block">' + lang +
               '<button type="button" class="copy-code" aria-label="Copy code">Copy</button>' +
               "<pre><code>" + escapeHtml(m[2].replace(/\n$/, "")) + "</code></pre></div>");
      last = re.lastIndex;
      if (m[0].length === 0) break;
    }
    if (last < src.length) out.push(blocks(src.slice(last)));
    return out.join("");
  }

  global.ChatMarkdown = { render: render, escapeHtml: escapeHtml };
})(window);
