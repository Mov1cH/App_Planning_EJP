/* Planning EJP — interactions légères (aucune dépendance). */
(function () {
  "use strict";

  /* --- Toasts : disparition automatique --- */
  document.querySelectorAll(".toast").forEach(function (toast, i) {
    var close = function () {
      toast.classList.add("is-leaving");
      setTimeout(function () { toast.remove(); }, 220);
    };
    var btn = toast.querySelector("button");
    if (btn) btn.addEventListener("click", close);
    if (!toast.classList.contains("toast-erreur")) setTimeout(close, 5000 + i * 600);
  });

  /* --- Confirmation avant les actions destructrices --- */
  var dialog = document.getElementById("confirm-dialog");
  var pendingForm = null;
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (!msg || form.dataset.confirmed === "1") return;
    e.preventDefault();
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm(msg)) { form.dataset.confirmed = "1"; form.requestSubmit ? form.requestSubmit() : form.submit(); }
      return;
    }
    pendingForm = form;
    dialog.querySelector("[data-confirm-title]").textContent = form.getAttribute("data-confirm-title") || "Confirmer";
    dialog.querySelector("[data-confirm-text]").textContent = msg;
    dialog.querySelector("[data-confirm-button]").textContent = form.getAttribute("data-confirm-ok") || "Supprimer";
    dialog.showModal();
  }, true);
  if (dialog) {
    dialog.addEventListener("close", function () {
      if (dialog.returnValue === "ok" && pendingForm) {
        pendingForm.dataset.confirmed = "1";
        pendingForm.submit();
      }
      pendingForm = null;
    });
  }

  /* --- Bouton en état de chargement lors d'un envoi --- */
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var btn = e.target.querySelector("[data-loading]");
    if (btn) { btn.classList.add("is-loading"); btn.setAttribute("aria-busy", "true"); }
  });

  /* --- Afficher / masquer le mot de passe --- */
  document.querySelectorAll("[data-toggle-password]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-toggle-password"));
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", String(show));
      btn.setAttribute("aria-label", show ? "Masquer le mot de passe" : "Afficher le mot de passe");
    });
  });

  /* --- Recherche + filtre par rôle dans une liste --- */
  document.querySelectorAll("[data-filter-list]").forEach(function (root) {
    var input = root.querySelector("[data-filter-input]");
    var items = Array.prototype.slice.call(root.querySelectorAll("[data-filter-item]"));
    var chips = Array.prototype.slice.call(root.querySelectorAll("[data-filter-chip]"));
    var counter = root.querySelector("[data-filter-count]");
    var empty = root.querySelector("[data-filter-empty]");
    var activeRole = "";
    var normalize = function (s) { return (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, ""); };
    var apply = function () {
      var q = normalize(input ? input.value : "");
      var visible = 0;
      items.forEach(function (it) {
        var okText = !q || normalize(it.textContent).indexOf(q) !== -1;
        var roles = (it.getAttribute("data-roles") || "").split(",");
        var okRole = !activeRole || roles.indexOf(activeRole) !== -1;
        var show = okText && okRole;
        it.hidden = !show;
        if (show) visible++;
      });
      if (counter) counter.textContent = visible;
      if (empty) empty.hidden = visible !== 0;
    };
    if (input) input.addEventListener("input", apply);
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        activeRole = chip.getAttribute("data-filter-chip");
        chips.forEach(function (c) { c.setAttribute("aria-pressed", String(c === chip)); });
        apply();
      });
    });
  });

  /* --- Compteur de sélection (affectation) --- */
  document.querySelectorAll("[data-pick-count]").forEach(function (out) {
    var form = out.closest("form");
    var quota = parseInt(out.getAttribute("data-quota"), 10) || 1;
    var update = function () {
      var n = form.querySelectorAll("input[type=checkbox]:checked").length;
      out.textContent = n + " / " + quota;
      out.className = "counter " + (n >= quota ? "ok" : "low");
    };
    form.addEventListener("change", update);
    update();
  });
})();
