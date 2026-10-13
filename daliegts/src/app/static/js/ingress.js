// DaLiegt’s als Home-Assistant-App (Ingress): alle Pfade „/…“ bekommen den Ingress-Pfad vorangestellt.
// Wird nur eingebunden, wenn die Seite über Home Assistant geöffnet wurde (window.LW_PREFIX ist gesetzt).
(function () {
  "use strict";
  const P = window.LW_PREFIX;
  if (!P) return;
  const fix = (u) => (typeof u === "string" && u.startsWith("/") && !u.startsWith("//") && !u.startsWith(P + "/") && u !== P ? P + u : u);
  window.lwUrl = window.lwUrl || fix;

  const _fetch = window.fetch;
  window.fetch = function (input, init) {
    if (typeof input === "string") input = fix(input);
    else if (input instanceof URL && input.origin === location.origin) input = new URL(fix(input.pathname) + input.search + input.hash, location.origin);
    return _fetch.call(this, input, init);
  };
  const _open = XMLHttpRequest.prototype.open; // htmx
  XMLHttpRequest.prototype.open = function (method, url, ...rest) { return _open.call(this, method, fix(String(url)), ...rest); };

  const ATTRS = ["href", "src", "action", "formaction", "poster", "hx-get", "hx-post", "hx-put", "hx-delete", "hx-patch"];
  const fixEl = (el) => {
    if (el.nodeType !== 1) return;
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v && v !== fix(v)) el.setAttribute(a, fix(v));
    }
  };
  const fixTree = (root) => { fixEl(root); if (root.querySelectorAll) root.querySelectorAll("[href],[src],[action],[formaction],[hx-get],[hx-post]").forEach(fixEl); };
  new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "attributes") fixEl(m.target);
      else m.addedNodes.forEach((n) => { if (n.nodeType === 1) fixTree(n); });
    }
  }).observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ATTRS });
  document.addEventListener("DOMContentLoaded", () => fixTree(document.documentElement));
  // Sicherheitsnetz: Klick auf Link / Absenden eines Formulars
  document.addEventListener("click", (e) => { const a = e.target.closest && e.target.closest("a[href]"); if (a) fixEl(a); }, true);
  document.addEventListener("submit", (e) => fixEl(e.target), true);
})();
