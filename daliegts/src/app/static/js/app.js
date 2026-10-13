// Lagerwerk – kleine Helfer für die Weboberfläche (ohne Build-Schritt)
"use strict";

// Home-Assistant-App (Ingress): die Oberfläche läuft unter /api/hassio_ingress/<token>/ – Pfade dort voranstellen
function lwUrl(u) {
  const p = window.LW_PREFIX || "";
  if (!p || typeof u !== "string" || !u.startsWith("/") || u.startsWith("//") || u.startsWith(p + "/") || u === p) return u;
  return p + u;
}
function lwGo(u) { window.location.href = lwUrl(u); }

function toast(msg, ms = 2600) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.remove("hide");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hide"), ms);
}

async function api(path, body, method = "POST") {
  const opts = { method, headers: { "Accept": "application/json", "X-Lagerwerk": "1" }, credentials: "same-origin" };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { /* leer */ }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

// LED-Anzeige: data-locate='{"location_id": 5}' an Buttons
async function locate(payload) {
  try {
    const r = await api("/api/v1/locate", payload);
    if (!r.ok && r.message) toast(r.message);
    else if (r.errors && Object.keys(r.errors).length) toast("WLED nicht erreichbar: " + Object.keys(r.errors).join(", "));
    else toast("💡 " + (r.notes && r.notes.length ? r.notes.join(" · ") : "Leuchtet"));
    return r;
  } catch (e) { toast("Fehler: " + e.message); }
}

document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-locate]");
  if (b) { ev.preventDefault(); locate(JSON.parse(b.dataset.locate)); }
  const c = ev.target.closest("[data-confirm]");
  if (c && !confirm(c.dataset.confirm)) { ev.preventDefault(); ev.stopImmediatePropagation(); }
}, true);

// ---------------------------------------------------------------- Handscanner (v0.7)
// USB-/Funk-Scanner wie der EY-034 „tippen“ den Code sehr schnell und drücken Enter. Steht der Cursor in keinem
// Eingabefeld, fängt Lagerwerk das ab und springt zum Fach, Artikel oder Bestand. Seiten können das Ereignis
// „lw:scan“ selbst übernehmen (preventDefault), z. B. die Lageransicht oder der Scanner.
const SCAN_GAP_MS = 60;
let scanBuf = "", scanLast = 0;
document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable)) { scanBuf = ""; return; }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const now = performance.now(), burst = now - scanLast < SCAN_GAP_MS;
  if (!burst) scanBuf = "";
  scanLast = now;
  if (e.key === "Enter" || e.key === "Tab") {
    if (burst && scanBuf.length >= 3) { e.preventDefault(); e.stopImmediatePropagation(); const code = scanBuf; scanBuf = ""; handleScan(code); }
    scanBuf = "";
    return;
  }
  if (e.key.length === 1) {
    scanBuf += e.key;
    if (burst && scanBuf.length > 1) { e.preventDefault(); e.stopImmediatePropagation(); } // z. B. „/“ nicht als Tastenkürzel
  }
}, true);

function handleScan(code) {
  const ev = new CustomEvent("lw:scan", { detail: { code }, cancelable: true });
  if (window.dispatchEvent(ev)) scanNavigate(code);
}

// Scanner auf US-Tastatur an deutschem PC: ß statt -, y/z vertauscht – zweiter Versuch mit korrigiertem Code
function scanVariants(code) {
  const out = [code];
  const fixed = code.replace(/ß/g, "-").replace(/[yzYZ]/g, (c) => ({ y: "z", z: "y", Y: "Z", Z: "Y" })[c]);
  if (fixed !== code) out.push(fixed);
  return out;
}

async function scanResolve(code) {
  let r = null;
  for (const c of scanVariants(code)) {
    r = await api("/api/v1/resolve?code=" + encodeURIComponent(c), undefined, "GET").catch(() => null);
    if (r && r.kind !== "unknown") return r;
  }
  return r;
}

async function scanNavigate(code) {
  const r = await scanResolve(code);
  if (!r) { toast("Code nicht lesbar: " + code); return; }
  if (r.kind === "location") {
    const l = r.location;
    lwGo(l.wall ? `/orte/${l.wall}/wand?orte=${l.id}` : l.url);
  } else if (r.kind === "item") lwGo(r.item.url);
  else if (r.kind === "lot") lwGo(`/artikel/${r.item.id}#lot-${r.lot.id}`);
  else if (r.kind === "order") lwGo(r.order.url);
  else if (r.label && Object.keys(r.label).length) lwGo("/artikel/erkennen?code=" + encodeURIComponent(code));
  else toast("Unbekannter Code: " + code + " – im Scanner (📷) kannst du ihn zuordnen.");
}

// Tastenkürzel: "/" fokussiert die Suche
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    const q = document.getElementById("topq");
    if (q) { e.preventDefault(); q.focus(); q.select(); }
  }
});

// Lagerort-Auswahl mit Vorschlägen: <input data-locpicker name="location">
function initLocPickers(root = document) {
  root.querySelectorAll("input[data-locpicker]").forEach((inp) => {
    if (inp._lp) return;
    inp._lp = true;
    const dl = document.createElement("datalist");
    dl.id = "dl-" + Math.random().toString(36).slice(2);
    inp.setAttribute("list", dl.id);
    inp.setAttribute("autocomplete", "off");
    inp.after(dl);
    let timer;
    inp.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = inp.value.trim();
        if (q.length < 2 || /^L-\d{5}$/i.test(q)) return;
        const r = await fetch("/orte/suche?q=" + encodeURIComponent(q), { credentials: "same-origin" });
        if (!r.ok) return;
        const rows = await r.json();
        dl.innerHTML = "";
        rows.forEach((row) => { const o = document.createElement("option"); o.value = row.code; o.label = row.path; o.textContent = row.path; dl.appendChild(o); });
      }, 180);
    });
  });
}

// Drag & Drop in Bäumen: <ul data-sortable-tree data-move-url="/orte/{id}/verschieben">
function initTrees(root = document) {
  if (!window.Sortable) return;
  root.querySelectorAll("ul[data-sortable]").forEach((ul) => {
    if (ul._st) return;
    ul._st = true;
    new Sortable(ul, {
      group: ul.dataset.sortable, handle: ".handle", animation: 150, fallbackOnBody: true, swapThreshold: 0.6, emptyInsertThreshold: 14,
      onStart: () => document.body.classList.add("tree-dragging"),
      onMove: (evt) => {
        // über einem zugeklappten Ort kurz verweilen = aufklappen, damit man hineinziehen kann
        const d = evt.related && evt.related.closest("details:not([open])");
        clearTimeout(window._treeOpenT);
        if (d) window._treeOpenT = setTimeout(() => { d.open = true; }, 600);
      },
      onEnd: async (evt) => {
        document.body.classList.remove("tree-dragging");
        clearTimeout(window._treeOpenT);
        if (evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
        const li = evt.item;
        const parentUl = li.parentElement;
        const fd = new FormData();
        fd.append("parent_id", parentUl.dataset.parent || "");
        fd.append("index", Array.from(parentUl.children).indexOf(li));
        const r = await fetch(li.dataset.moveUrl, { method: "POST", body: fd, credentials: "same-origin" });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) { toast("Verschieben nicht möglich: " + (j.error || j.detail || r.statusText)); setTimeout(() => location.reload(), 900); }
        else toast("Verschoben");
      },
    });
  });
}

// Inline-Umbenennen: <span data-rename-url="/orte/5/umbenennen">Name</span> (Doppelklick)
document.addEventListener("dblclick", (e) => {
  const el = e.target.closest("[data-rename-url]");
  if (!el) return;
  const old = el.textContent.trim();
  const name = prompt("Neuer Name:", old);
  if (!name || name === old) return;
  const fd = new FormData();
  fd.append("name", name);
  fetch(el.dataset.renameUrl, { method: "POST", body: fd, credentials: "same-origin", headers: { "HX-Request": "true" } })
    .then((r) => { if (r.ok) { el.textContent = name; toast("Umbenannt"); } else r.json().then((j) => toast(j.detail || "Fehler")); });
});

// Parameter-Zeilen im Artikelformular
function addParamRow(btn) {
  const tpl = document.getElementById("param-row-tpl");
  const tbody = document.getElementById("param-rows");
  tbody.appendChild(tpl.content.cloneNode(true));
}

document.addEventListener("DOMContentLoaded", () => {
  initLocPickers();
  initTrees();
  // /orte#ort-12: Ort im Baum aufklappen und hervorheben
  const m = location.hash.match(/^#ort-(\d+)$/);
  const el = m && document.getElementById("ort-" + m[1]);
  if (el) {
    for (let p = el.parentElement; p; p = p.parentElement) if (p.tagName === "DETAILS") p.open = true;
    el.classList.add("flash-node");
    el.scrollIntoView({ block: "center" });
  }
  if ("serviceWorker" in navigator && location.protocol === "https:" && !window.LW_PREFIX) navigator.serviceWorker.register("/sw.js").catch(() => {});
});
document.addEventListener("htmx:afterSwap", (e) => { initLocPickers(e.target); initTrees(e.target); });
document.addEventListener("htmx:responseError", (e) => {
  try { toast(JSON.parse(e.detail.xhr.responseText).detail); } catch (_) { toast("Fehler " + e.detail.xhr.status); }
});

// Artikelauswahl mit Vorschlägen (Aufträge, Projekte): x-data="itemPick()" mit name=item / item_id
function itemPick() {
  return {
    q: "", id: "", sugg: [], idx: -1, timer: null,
    find() {
      clearTimeout(this.timer); this.id = "";
      if (this.q.trim().length < 2) { this.sugg = []; return; }
      this.timer = setTimeout(async () => {
        const r = await api("/api/v1/search?limit=12&q=" + encodeURIComponent(this.q.trim()), undefined, "GET").catch(() => null);
        this.sugg = r ? r.items : []; this.idx = -1;
      }, 200);
    },
    choose(it) { this.q = it.name; this.id = it.id; this.sugg = []; setTimeout(() => { const q = document.getElementById("pos-qty"); if (q) { q.focus(); q.select(); } }, 0); },
    key(e) {
      if (!this.sugg.length) return;
      if (e.key === "ArrowDown") { this.idx = Math.min(this.sugg.length - 1, this.idx + 1); e.preventDefault(); }
      else if (e.key === "ArrowUp") { this.idx = Math.max(0, this.idx - 1); e.preventDefault(); }
      else if (e.key === "Enter" && this.idx >= 0) { this.choose(this.sugg[this.idx]); e.preventDefault(); }
    },
  };
}

// ---------------------------------------------------------------- Menü (v0.18): Seitenleiste ein-/ausklappen, am Handy als Schublade
function lwNav(open) {
  document.documentElement.classList.toggle("nav-open", open);
}
function lwNavCollapse() {
  const c = document.documentElement.classList.toggle("nav-collapsed");
  try { localStorage.setItem("lw-nav-collapsed", c ? "1" : "0"); } catch (_) { /* privat */ }
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") lwNav(false); });
document.addEventListener("click", (e) => {  // „Neu“-Menü schließen beim Klick daneben
  document.querySelectorAll("details.dropdown[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});
