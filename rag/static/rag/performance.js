/* Performance report charts: plain SVG/HTML, no library.
 * Mark specs: bars <= 24px with 4px rounded data-ends, 2px surface gaps, 2px lines,
 * 8px end dots with a surface ring, hairline grid. Tooltips on hover and keyboard focus;
 * every value is also in each chart's "Show data" table. Text uses textContent only. */
(function () {
  "use strict";
  var dataEl = document.getElementById("perf-data");
  if (!dataEl) return;
  var data = JSON.parse(dataEl.textContent);
  if (!data.kpis.questions) return;

  var NS = "http://www.w3.org/2000/svg";
  // Colours follow the validated palette order (slots 1-5), in stacking order.
  var OUTCOMES = [
    { key: "answered", label: "Answered by model", cls: "s1" },
    { key: "faq", label: "FAQ or small talk", cls: "s2" },
    { key: "declined", label: "Declined by model", cls: "s3" },
    { key: "not_found", label: "Not found in documents", cls: "s4" },
    { key: "other", label: "Unavailable, limit or error", cls: "s5" },
  ];
  var tip = document.getElementById("viz-tip");

  /* ---------- helpers ---------- */
  function svgEl(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function text(parent, x, y, str, anchor, cls) {
    var t = svgEl("text", { x: x, y: y, "text-anchor": anchor || "start" }, parent);
    if (cls) t.setAttribute("class", cls);
    t.textContent = str;
    return t;
  }
  function niceStep(raw) {
    var p = Math.pow(10, Math.floor(Math.log10(raw)));
    var f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }
  function ticks(max, count) {
    if (!(max > 0)) return [0, 1];
    var step = niceStep(max / (count || 4));
    var out = [];
    for (var v = 0; v <= max + step * 0.001; v += step) out.push(+v.toFixed(6));
    if (out[out.length - 1] < max) out.push(+(out[out.length - 1] + step).toFixed(6));
    return out;
  }
  function parseDate(s) { var p = s.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function fmtDate(s) { return parseDate(s).toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
  function fmtNum(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "K" : String(n); }
  function fmtSecs(ms) { if (ms == null) return "–"; var s = ms / 1000; return (s < 10 ? s.toFixed(1) : Math.round(s)) + " s"; }
  function pct(x) { return x == null ? "–" : Math.round(x * 100) + "%"; }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  // Rect with only the top corners rounded (data end), square at the baseline.
  function topRounded(parent, x, y, w, h, r, cls) {
    if (h <= 0) return;
    r = Math.min(r, w / 2, h);
    var d = "M" + x + "," + (y + h) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
            "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + h) + "Z";
    svgEl("path", { d: d, class: cls }, parent);
  }

  /* ---------- tooltip ---------- */
  function showTip(title, rows, pos) {
    tip.textContent = "";
    var t = document.createElement("div");
    t.className = "tt-title";
    t.textContent = title;
    tip.appendChild(t);
    rows.forEach(function (r) {
      var row = document.createElement("div");
      row.className = "tt-row";
      var key = document.createElement("span");
      key.className = "tt-key";
      key.style.background = r.color || "transparent";
      var val = document.createElement("strong");
      val.textContent = r.value;
      var lab = document.createElement("span");
      lab.className = "lab";
      lab.textContent = r.label;
      row.appendChild(key); row.appendChild(val); row.appendChild(lab);
      tip.appendChild(row);
    });
    tip.hidden = false;
    var x = pos.x + 14, y = pos.y + 14;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    if (x + w > window.innerWidth - 8) x = pos.x - w - 14;
    if (y + h > window.innerHeight - 8) y = pos.y - h - 14;
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = Math.max(8, y) + "px";
  }
  function hideTip() { tip.hidden = true; }
  function pointerPos(e) { return { x: e.clientX, y: e.clientY }; }
  function elementPos(el) { var r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top }; }
  function cssColor(cls) {
    return getComputedStyle(document.querySelector(".viz-root")).getPropertyValue("--" + cls).trim();
  }
  function bindTip(el, build) {
    el.addEventListener("pointermove", function (e) { var b = build(); showTip(b.title, b.rows, pointerPos(e)); });
    el.addEventListener("pointerleave", hideTip);
    el.addEventListener("focus", function () { var b = build(); showTip(b.title, b.rows, elementPos(el)); });
    el.addEventListener("blur", hideTip);
  }

  /* ---------- tall segmented bar: outcome split ---------- */
  function segbar() {
    var host = document.getElementById("segbar");
    if (!host) return;
    host.textContent = "";
    data.outcomes.forEach(function (o, i) {
      if (!o.count) return;
      var part = document.createElement("div");
      part.className = "part " + OUTCOMES[i].cls;
      part.style.flex = o.count + " 1 0";
      part.tabIndex = 0;
      part.setAttribute("aria-label", o.label + ": " + o.count + " (" + pct(o.share) + ")");
      var label = document.createElement("span");
      label.textContent = pct(o.share);
      part.appendChild(label);
      host.appendChild(part);
      bindTip(part, function () {
        return { title: o.label, rows: [{ value: String(o.count), label: (o.count === 1 ? "question" : "questions") + " · " + pct(o.share), color: cssColor(OUTCOMES[i].cls) }] };
      });
    });
    // A part too narrow for its label prints none (the value stays in the table view).
    Array.prototype.forEach.call(host.children, function (part) {
      var label = part.firstChild;
      if (label && label.offsetWidth + 16 > part.clientWidth) part.removeChild(label);
    });
  }

  /* ---------- column chart frame ---------- */
  function frame(host, height, yMax, yFmt) {
    host.textContent = "";
    var W = Math.max(260, host.clientWidth), H = height;
    var m = { l: 40, r: 10, t: 14, b: 26 };
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, height: H, role: "img" }, host);
    var yt = ticks(yMax, 4), top = yt[yt.length - 1] || 1;
    var plotH = H - m.t - m.b, plotW = W - m.l - m.r;
    var y = function (v) { return m.t + plotH - (v / top) * plotH; };
    yt.forEach(function (v) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: v === 0 ? "baseline" : "grid-line" }, svg);
      text(svg, m.l - 6, y(v) + 4, yFmt ? yFmt(v) : fmtNum(v), "end");
    });
    return { svg: svg, W: W, H: H, m: m, plotW: plotW, plotH: plotH, y: y, top: top };
  }
  function xLabels(f, n, band, labelFor) {
    var every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(f.plotW / 70))));
    for (var i = 0; i < n; i += every) {
      text(f.svg, f.m.l + i * band + band / 2, f.H - 8, labelFor(i), "middle");
    }
  }

  /* ---------- questions per day, stacked by outcome ---------- */
  function dailyChart() {
    var host = document.getElementById("chart-daily");
    if (!host) return;
    var rows = data.daily;
    function total(d) { return OUTCOMES.reduce(function (sum, o) { return sum + (d[o.key] || 0); }, 0); }
    var max = Math.max.apply(null, rows.map(total).concat([1]));
    var f = frame(host, 200, max);
    var band = f.plotW / rows.length;
    var bw = Math.max(2, Math.min(24, band * 0.62));
    rows.forEach(function (d, i) {
      var x = f.m.l + i * band + (band - bw) / 2;
      var segs = OUTCOMES.map(function (o) { return { o: o, v: d[o.key] }; }).filter(function (s) { return s.v > 0; });
      var base = f.y(0);
      segs.forEach(function (s, j) {
        var h = (s.v / f.top) * f.plotH;
        var isTop = j === segs.length - 1;
        var drawH = isTop ? h : Math.max(0, h - 2);  // 2px surface gap above each lower segment
        var yTop = base - h;
        if (isTop) topRounded(f.svg, x, yTop, bw, drawH, 4, "fill-" + s.o.cls);
        else svgEl("rect", { x: x, y: yTop + 2, width: bw, height: drawH, class: "fill-" + s.o.cls }, f.svg);
        base -= h;
      });
      var hit = svgEl("rect", { x: f.m.l + i * band, y: f.m.t, width: band, height: f.plotH, class: "hover-col", tabindex: 0 }, f.svg);
      var count = total(d);
      hit.setAttribute("aria-label", fmtDate(d.date) + ": " + plural(count, "question") + ", " + (d.answered + d.faq) + " answered");
      bindTip(hit, function () {
        return {
          title: (data.bucket === "week" ? "Week of " : "") + fmtDate(d.date) + " · " + plural(count, "question"),
          rows: OUTCOMES.map(function (o) { return { value: String(d[o.key]), label: o.label, color: cssColor(o.cls) }; }),
        };
      });
    });
    xLabels(f, rows.length, band, function (i) { return fmtDate(rows[i].date); });
  }

  /* ---------- response time: median (story) and p95 (context) ---------- */
  function latencyChart() {
    var host = document.getElementById("chart-latency");
    if (!host) return;
    var rows = data.daily;
    var max = Math.max.apply(null, rows.map(function (d) { return d.p95_ms || d.p50_ms || 0; }).concat([1000]));
    var f = frame(host, 200, max / 1000, function (v) { return v + " s"; });
    var band = f.plotW / rows.length;
    var cx = function (i) { return f.m.l + i * band + band / 2; };
    function draw(key, cls) {
      var d = "", last = null;
      rows.forEach(function (r, i) {
        var v = r[key];
        if (v == null) { last = null; return; }
        d += (last === null ? "M" : "L") + cx(i) + "," + f.y(v / 1000);
        last = i;
      });
      if (d) svgEl("path", { d: d, class: "line " + cls }, f.svg);
      // Isolated points (no neighbours) and the latest point get a dot.
      rows.forEach(function (r, i) {
        if (r[key] == null) return;
        var prev = i > 0 && rows[i - 1][key] != null, next = i < rows.length - 1 && rows[i + 1][key] != null;
        var lastIdx = -1;
        rows.forEach(function (rr, k) { if (rr[key] != null) lastIdx = k; });
        if ((!prev && !next) || i === lastIdx) {
          svgEl("circle", { cx: cx(i), cy: f.y(r[key] / 1000), r: 4, class: "dot " + cls.replace("line-", "fill-") }, f.svg);
        }
      });
    }
    draw("p95_ms", "line-muted");
    draw("p50_ms", "line-s1");
    f.svg.querySelectorAll(".fill-muted").forEach(function (c) { c.style.fill = "var(--viz-muted)"; });
    xLabels(f, rows.length, band, function (i) { return fmtDate(rows[i].date); });

    var cross = svgEl("line", { x1: 0, x2: 0, y1: f.m.t, y2: f.m.t + f.plotH, class: "crosshair", visibility: "hidden" }, f.svg);
    var overlay = svgEl("rect", { x: f.m.l, y: f.m.t, width: f.plotW, height: f.plotH, fill: "transparent", tabindex: 0 }, f.svg);
    overlay.setAttribute("aria-label", "Response time by " + data.bucket + "; use arrow keys to step through");
    var idx = rows.length - 1;
    function at(i, pos) {
      idx = Math.max(0, Math.min(rows.length - 1, i));
      var r = rows[idx];
      cross.setAttribute("x1", cx(idx)); cross.setAttribute("x2", cx(idx));
      cross.setAttribute("visibility", "visible");
      showTip(fmtDate(r.date), [
        { value: fmtSecs(r.p50_ms), label: "median", color: cssColor("s1") },
        { value: fmtSecs(r.p95_ms), label: "95th percentile", color: cssColor("viz-muted") },
      ], pos);
    }
    function hide() { cross.setAttribute("visibility", "hidden"); hideTip(); }
    overlay.addEventListener("pointermove", function (e) {
      var box = f.svg.getBoundingClientRect();
      var sx = (e.clientX - box.left) * (f.W / box.width);
      at(Math.round((sx - f.m.l - band / 2) / band), pointerPos(e));
    });
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("focus", function () { at(idx, elementPos(overlay)); });
    overlay.addEventListener("blur", hide);
    overlay.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        at(idx + (e.key === "ArrowRight" ? 1 : -1), elementPos(overlay));
      }
    });
  }

  /* ---------- best-passage relevance histogram with the cut-off ---------- */
  function scoreChart() {
    var host = document.getElementById("chart-scores");
    if (!host || !data.score_hist.length) return;
    var bins = data.score_hist;
    var max = Math.max.apply(null, bins.map(function (b) { return b.answered + b.not_answered; }).concat([1]));
    var f = frame(host, 200, max);
    var band = f.plotW / bins.length;
    var bw = Math.max(2, Math.min(24, band - 2));  // adjacent bars keep a 2px surface gap
    bins.forEach(function (b, i) {
      var x = f.m.l + i * band + (band - bw) / 2;
      var hA = (b.answered / f.top) * f.plotH, hN = (b.not_answered / f.top) * f.plotH;
      var base = f.y(0);
      if (b.answered) {
        if (b.not_answered) svgEl("rect", { x: x, y: base - hA + 2, width: bw, height: Math.max(0, hA - 2), class: "fill-s1" }, f.svg);
        else topRounded(f.svg, x, base - hA, bw, hA, 4, "fill-s1");
      }
      if (b.not_answered) topRounded(f.svg, x, base - hA - hN, bw, hN, 4, "fill-neutral");
      var hit = svgEl("rect", { x: f.m.l + i * band, y: f.m.t, width: band, height: f.plotH, class: "hover-col", tabindex: 0 }, f.svg);
      var range = b.bin.toFixed(2) + "–" + (b.bin + 0.05).toFixed(2);
      hit.setAttribute("aria-label", "Relevance " + range + ": " + b.answered + " answered, " + b.not_answered + " not answered");
      bindTip(hit, function () {
        return { title: "Relevance " + range, rows: [
          { value: String(b.answered), label: "answered", color: cssColor("s1") },
          { value: String(b.not_answered), label: "not answered", color: cssColor("neutral") },
        ] };
      });
    });
    var x0 = bins[0].bin;
    var cutX = f.m.l + ((data.min_score - x0) / 0.05) * band;
    if (cutX >= f.m.l && cutX <= f.m.l + f.plotW) {
      svgEl("line", { x1: cutX, x2: cutX, y1: f.m.t - 4, y2: f.m.t + f.plotH, class: "ref-line" }, f.svg);
      text(f.svg, cutX + 4, f.m.t + 6, "cut-off " + data.min_score, "start", "ref-label");
    }
    xLabels(f, bins.length, band, function (i) { return bins[i].bin.toFixed(2); });
  }

  /* ---------- most used documents: ranked horizontal bars ---------- */
  function docsChart() {
    var host = document.getElementById("chart-docs");
    if (!host) return;
    host.textContent = "";
    var wrap = document.createElement("div");
    wrap.className = "hbars";
    var max = Math.max.apply(null, data.documents.map(function (d) { return d.answers; }).concat([1]));
    data.documents.forEach(function (d) {
      var label = document.createElement("div");
      label.className = "hb-label";
      label.textContent = d.document;
      label.title = d.document;
      var track = document.createElement("div");
      track.className = "hb-track hb-row";
      track.tabIndex = 0;
      track.setAttribute("aria-label", d.document + ": " + d.answers + " answers");
      var bar = document.createElement("div");
      bar.className = "hb-bar";
      bar.style.width = Math.max(1, (d.answers / max) * 85) + "%";
      var val = document.createElement("span");
      val.className = "hb-value";
      val.textContent = d.answers;
      track.appendChild(bar); track.appendChild(val);
      wrap.appendChild(label); wrap.appendChild(track);
      bindTip(track, function () { return { title: d.document, rows: [{ value: String(d.answers), label: "answers drew on it", color: cssColor("s1") }] }; });
    });
    host.appendChild(wrap);
  }

  function renderAll() { segbar(); dailyChart(); latencyChart(); scoreChart(); docsChart(); }
  renderAll();
  var timer = null;
  window.addEventListener("resize", function () { clearTimeout(timer); timer = setTimeout(renderAll, 120); });
})();
