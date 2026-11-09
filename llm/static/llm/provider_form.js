/* Show only the connection fields the selected provider type uses; mark required ones. */
(function () {
  "use strict";
  var FIELDS = ["key", "secret", "endpoint", "region", "api_version", "project"];

  function row(name) { return document.querySelector(".form-row.field-" + name) || document.querySelector(".field-" + name); }

  function apply(select, schema) {
    var spec = schema[select.value] || { fields: {}, hints: {} };
    FIELDS.forEach(function (name) {
      var r = row(name);
      if (!r) return;
      var need = spec.fields[name];
      r.style.display = need ? "" : "none";
      var clear = row("clear_" + name);
      if (clear) clear.style.display = need ? "" : "none";
      var label = r.querySelector("label");
      if (label) label.classList.toggle("required", need === "required");
      var hint = r.querySelector(".llm-hint");
      if (!hint) {
        hint = document.createElement("div");
        hint.className = "help llm-hint";
        var input = r.querySelector("input, textarea, select");
        (input ? input.parentNode : r).appendChild(hint);
      }
      hint.textContent = need ? (need === "required" ? "Required. " : "Optional. ") + (spec.hints[name] || "") : "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("id_provider_type");
    if (!select) return;
    var schema = JSON.parse(select.dataset.schema || "{}");
    select.addEventListener("change", function () { apply(select, schema); });
    apply(select, schema);
  });
})();
