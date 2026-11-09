(function () {
  "use strict";
  var form = document.getElementById("upload-form");
  var input = document.getElementById("files");
  var zone = document.getElementById("drop-zone");
  var title = document.getElementById("drop-title");
  var btn = document.getElementById("upload-btn");
  var defaultTitle = title.innerHTML;

  var maxFiles = parseInt(input.dataset.maxFiles, 10);
  var maxBytes = parseInt(input.dataset.maxBytes, 10);
  var accepted = (input.dataset.accept || "").toLowerCase().split(",").filter(Boolean);

  // Early feedback only; the server enforces the same limits.
  function problems(files) {
    var out = [];
    if (files.length > maxFiles) {
      out.push("Only " + maxFiles + " more file" + (maxFiles === 1 ? "" : "s") + " can be added to this workspace.");
    }
    Array.prototype.forEach.call(files, function (f) {
      var dot = f.name.lastIndexOf(".");
      var ext = dot >= 0 ? f.name.slice(dot).toLowerCase() : "";
      if (accepted.length && accepted.indexOf(ext) === -1) out.push(f.name + ": file type not allowed.");
      else if (maxBytes && f.size > maxBytes) out.push(f.name + ": larger than " + Math.round(maxBytes / 1048576) + " MB.");
    });
    return out;
  }

  function refresh() {
    var n = input.files.length;
    var old = zone.querySelector(".drop-problem");
    if (old) old.remove();
    if (n === 0) { title.innerHTML = defaultTitle; btn.disabled = true; return; }
    title.textContent = n === 1 ? input.files[0].name : n + " files selected";
    var issues = problems(input.files);
    btn.disabled = issues.length > 0;
    if (issues.length) {
      var msg = document.createElement("span");
      msg.className = "drop-problem";
      msg.textContent = issues.join(" ");
      zone.appendChild(msg);
    }
  }

  input.addEventListener("change", refresh);

  ["dragenter", "dragover"].forEach(function (t) {
    zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.remove("over"); });
  });
  zone.addEventListener("drop", function (e) {
    if (input.disabled || !e.dataTransfer || !e.dataTransfer.files.length) return;
    input.files = e.dataTransfer.files;
    refresh();
  });

  form.addEventListener("submit", function () {
    // Embedding can take a while; show progress and block double submits.
    btn.disabled = true;
    btn.classList.add("busy");
    btn.textContent = "Processing…";
  });

  document.querySelectorAll(".rebuild-form").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button");
      b.disabled = true;
      b.textContent = "Rebuilding…";
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!confirm(f.dataset.confirm)) e.preventDefault();
    });
  });
})();
