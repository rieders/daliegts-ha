// Lagerwerk – Handy-Scanner (Alpine-Komponente)
// Erkennung: native BarcodeDetector (Chrome/Android), sonst zxing-wasm (lokal eingebunden, offline).
"use strict";

const FORMATS = ["qr_code", "data_matrix", "ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39"];
let zxingReady = null;

function loadZxing() {
  if (zxingReady) return zxingReady;
  zxingReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = lwUrl("/static/vendor/zxing/zxing-reader-2.2.4.js");
    s.onload = () => {
      try {
        ZXingWASM.prepareZXingModule({
          overrides: { locateFile: (path, prefix) => (path.endsWith(".wasm") ? "/static/vendor/zxing/" + path : prefix + path) },
        });
        resolve(ZXingWASM);
      } catch (e) { reject(e); }
    };
    s.onerror = reject;
    document.head.appendChild(s);
  });
  return zxingReady;
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.frequency.value = 1320; g.gain.value = 0.08; o.connect(g); g.connect(ctx.destination);
    o.start(); setTimeout(() => { o.stop(); ctx.close(); }, 90);
  } catch (_) { /* egal */ }
  if (navigator.vibrate) navigator.vibrate(60);
}

// Kamera-Scan (BarcodeDetector bzw. zxing) – gemeinsam für Scanner- und Buchen-Seite. Erwartet this.handle(code) und $refs.video.
function cameraMixin() {
  return {
    camOn: false, camError: "", engine: "", stream: null, detector: null, torchAvailable: false, torch: false,
    lastCode: "", lastTime: 0, busy: false,
    async toggleCamera() { this.camOn ? this.stopCamera() : this.startCamera(); },
    async startCamera() {
      this.camError = "";
      if (!navigator.mediaDevices || !window.isSecureContext) {
        this.camError = "Kamera braucht HTTPS. Bitte DaLiegt’s über https:// öffnen (siehe Anleitung „Zertifikat aufs Handy“).";
        return;
      }
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
      } catch (e) { this.camError = "Kamera nicht verfügbar: " + e.message; return; }
      this.camOn = true;
      await this.$nextTick();
      const v = this.$refs.video;
      v.srcObject = this.stream;
      await v.play().catch(() => {});
      const track = this.stream.getVideoTracks()[0];
      try { this.torchAvailable = !!(track.getCapabilities && track.getCapabilities().torch); } catch (_) { this.torchAvailable = false; }
      if ("BarcodeDetector" in window) {
        try {
          const supported = await BarcodeDetector.getSupportedFormats();
          this.detector = new BarcodeDetector({ formats: FORMATS.filter((f) => supported.includes(f)) });
          this.engine = "nativ";
        } catch (_) { this.detector = null; }
      }
      if (!this.detector) { await loadZxing(); this.engine = "zxing"; }
      this.loop();
    },
    stopCamera() {
      this.camOn = false; this.torch = false;
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    },
    async toggleTorch() {
      const track = this.stream && this.stream.getVideoTracks()[0];
      if (!track) return;
      this.torch = !this.torch;
      try { await track.applyConstraints({ advanced: [{ torch: this.torch }] }); } catch (_) { toast("Taschenlampe nicht unterstützt"); }
    },
    async loop() {
      const v = this.$refs.video;
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      while (this.camOn) {
        if (v.readyState >= 2 && !this.busy) {
          try {
            let codes = [];
            if (this.detector) {
              codes = (await this.detector.detect(v)).map((b) => b.rawValue);
            } else {
              const w = v.videoWidth, h = v.videoHeight;
              const cw = Math.min(w, 960), ch = Math.round(h * cw / w);
              canvas.width = cw; canvas.height = ch;
              ctx.drawImage(v, 0, 0, cw, ch);
              const res = await ZXingWASM.readBarcodes(ctx.getImageData(0, 0, cw, ch), { tryHarder: true, maxNumberOfSymbols: 1 });
              codes = res.filter((r) => r.isValid).map((r) => r.text);
            }
            if (codes.length) this.onDetected(codes[0]);
          } catch (_) { /* nächster Frame */ }
        }
        await new Promise((r) => setTimeout(r, this.detector ? 120 : 220));
      }
    },
    onDetected(code) {
      const now = Date.now();
      if (code === this.lastCode && now - this.lastTime < 2500) return;
      this.lastCode = code; this.lastTime = now;
      beep();
      this.handle(code);
    },
  };
}

function scanApp(cfg) {
  return {
    caps: cfg, mode: cfg.mode || "info",
    modes: [
      { id: "info", label: "Suchen", icon: "🔎", show: true },
      { id: "einlagern", label: "Einlagern", icon: "📥", show: cfg.canStock },
      { id: "entnehmen", label: "Entnehmen", icon: "📤", show: cfg.canStock },
      { id: "umlagern", label: "Umlagern", icon: "🔀", show: cfg.canStock },
      { id: "inventur", label: "Inventur", icon: "✅", show: cfg.canInv },
      { id: "neu", label: "Neu", icon: "➕", show: cfg.canCreate },
      { id: "zuordnen", label: "Etiketten", icon: "🏷", show: cfg.canAlias },
    ],
    ...cameraMixin(),
    manualCode: "", searchResults: [],
    item: null, loc: null, lot: null, target: null, moveWhole: null, unknownCode: null, unknownLabel: null, qty: 1, checked: {},
    qc: { name: "", key: "", catalog: [], category: "", location: "", locationPath: "", qty: 1, barcode: "" },
    assign: { furniture: null, options: [], cells: [], idx: 0, last: null },

    init() {
      if (cfg.unknown) this.handle(cfg.unknown);
      if (cfg.furniture) { this.mode = "zuordnen"; api(`/api/v1/locations/${cfg.furniture}`, undefined, "GET").then((l) => this.loadFurniture(l)); }
      // Hardware-Scanner (Tastatur-Emulation) landen im Eingabefeld
      this.$nextTick(() => this.$refs.manualInput && this.$refs.manualInput.focus());
      window.addEventListener("pagehide", () => this.stopCamera());
      // Handscanner, während der Cursor nicht im Eingabefeld steht
      window.addEventListener("lw:scan", (e) => { e.preventDefault(); beep(); this.handle(e.detail.code, true); });
    },
    fmt(v) { return v === null || v === undefined ? "–" : String(+(+v).toFixed(3)).replace(".", ","); },
    setMode(m) { this.mode = m; this.reset(); if (m === "zuordnen" && !this.assign.furniture) this.furnitureSearch(""); },
    reset() { this.item = this.loc = this.lot = this.target = this.moveWhole = this.unknownCode = this.unknownLabel = null; this.qty = 1; this.searchResults = []; this.checked = {}; },

    prompt() {
      const m = this.mode;
      if (m === "info") return "Etikett, Barcode oder Name";
      if (m === "einlagern") return !this.item ? "1. Artikel scannen/suchen" : !this.loc ? "2. Lagerort scannen" : "3. Menge bestätigen";
      if (m === "entnehmen") return !this.lot ? "Artikel oder Fach scannen, Bestand wählen" : "Menge bestätigen";
      if (m === "umlagern") return this.moveWhole ? "Ziel-Lagerort scannen" : !this.lot ? "Artikel scannen, Bestand wählen (oder ganzen Ort scannen)" : !this.target ? "Ziel-Lagerort scannen" : "Menge bestätigen";
      if (m === "inventur") return !this.loc ? "Lagerort/Fach scannen" : "Artikel scannen oder abhaken";
      if (m === "neu") return "Foto, Name, Ort – fertig";
      if (m === "zuordnen") return !this.assign.furniture ? "Möbel wählen" : "Etikett am markierten Fach scannen";
      return "";
    },
    needsQty() {
      if (this.mode === "einlagern") return this.item && this.loc;
      if (this.mode === "entnehmen") return !!this.lot;
      if (this.mode === "umlagern") return this.lot && this.target;
      return false;
    },
    commitLabel() { return { einlagern: "Einlagern", entnehmen: "Entnehmen", umlagern: "Umlagern" }[this.mode] || "OK"; },

    manual() {
      const v = this.manualCode.trim();
      this.manualCode = "";
      if (v) this.handle(v, true);
    },

    // ------------------------------------------------ Verarbeitung
    async handle(code, typed = false) {
      if (this.mode === "zuordnen") return this.assignCode(code);
      if (this.mode === "neu") {
        const r = await api("/api/v1/resolve?code=" + encodeURIComponent(code), undefined, "GET").catch(() => null);
        if (r && r.kind === "location") { this.qc.location = r.location.code; this.qc.locationPath = r.location.path; }
        else if (typed && !this.qc.name) { this.qc.name = code; this.catalogSearch(); }
        else this.qc.barcode = code;
        return;
      }
      this.busy = true;
      let r;
      try { r = await api("/api/v1/resolve?code=" + encodeURIComponent(code), undefined, "GET"); }
      catch (e) { toast(e.message); this.busy = false; return; }
      this.busy = false;
      if (r.kind === "unknown") {
        if (typed && !r.label) return this.search(code);
        this.unknownCode = code;
        this.unknownLabel = r.label || null;
        return;
      }
      this.unknownCode = null;
      this.searchResults = [];
      if (r.kind === "location") return this.onLocation(r.location);
      if (r.kind === "item") return this.onItem(r.item);
      if (r.kind === "lot") { this.onItem(r.item); this.pickLot(r.lot); }
    },
    async search(q) {
      const r = await api("/api/v1/search?q=" + encodeURIComponent(q) + "&limit=15", undefined, "GET");
      this.searchResults = r.items;
      if (!r.items.length) toast("Nichts gefunden");
    },
    useItem(it) { this.searchResults = []; this.onItem(it); },
    led(payload) { locate(payload); },

    onLocation(loc) {
      const m = this.mode;
      if (m === "info" || m === "inventur") {
        this.loc = loc; this.item = null; this.lot = null;
        if (loc.has_lights && this.caps.canLed) this.led({ location_id: loc.id });
        return;
      }
      if (m === "einlagern") {
        this.loc = loc;
        if (loc.has_lights && this.caps.canLed) this.led({ location_id: loc.id });
        if (!this.item) toast("Ort gewählt – jetzt Artikel scannen");
        return;
      }
      if (m === "entnehmen") { this.loc = loc; this.item = null; this.lot = null; if (loc.lots.length === 1) this.pickLot(loc.lots[0]); return; }
      if (m === "umlagern") {
        if (this.moveWhole) return this.moveLocation(loc);
        if (this.lot) { this.target = loc; this.qty = ""; toast("Ziel: " + loc.path); return; }
        this.loc = loc;
      }
    },
    onItem(item) {
      const m = this.mode;
      if (m === "inventur" && this.loc) {
        const l = this.loc.lots.find((x) => x.item_id === item.id);
        if (l) { this.confirmLot(l); return; }
        if (confirm(`„${item.name}“ ist hier nicht erwartet. Mit Menge 1 hier einlagern?`)) {
          api("/api/v1/stock/add", { item_id: item.id, location: this.loc.id, quantity: 1, note: "Inventur: gefunden" })
            .then(() => { toast("Eingelagert"); this.refreshLoc(); });
        }
        return;
      }
      this.item = item;
      if (m === "info" && item.lots.length && this.caps.canLed) this.led({ item_id: item.id });
      if ((m === "entnehmen" || m === "umlagern") && item.lots.length === 1) this.pickLot(item.lots[0]);
    },
    pickLot(l) {
      this.lot = l;
      if (this.mode === "umlagern") { this.qty = ""; toast("Jetzt Ziel-Lagerort scannen"); }
      else this.qty = 1;
      if (l.location_id && this.caps.canLed) this.led({ location_id: l.location_id });
    },
    async refreshLoc() {
      if (!this.loc) return;
      const r = await api(`/api/v1/locations/${this.loc.id}`, undefined, "GET");
      this.loc = r;
    },
    async commit() {
      try {
        if (this.mode === "einlagern") {
          await api("/api/v1/stock/add", { item_id: this.item.id, location: this.loc.id, quantity: +this.qty });
          toast(`✔ ${this.fmt(this.qty)} × ${this.item.name} → ${this.loc.name}`);
          this.item = null; this.qty = 1;  // Ort bleibt für weitere Artikel
        } else if (this.mode === "entnehmen") {
          const r = await api("/api/v1/stock/remove", { lot_id: this.lot.id, quantity: +this.qty });
          toast(`✔ entnommen – Rest ${this.fmt(r.lot.quantity)} ${r.lot.unit}`);
          this.reset();
        } else if (this.mode === "umlagern") {
          await api("/api/v1/stock/move", { lot_id: this.lot.id, location: this.target.id, quantity: this.qty === "" ? null : +this.qty });
          toast("✔ umgelagert nach " + this.target.name);
          this.reset();
        }
      } catch (e) { toast("Fehler: " + e.message, 4000); }
    },
    async coarse(level) {
      await api("/api/v1/stock/coarse", { lot_id: this.lot.id, level });
      toast("✔ Füllstand gespeichert"); this.reset();
    },
    async moveLocation(target) {
      try {
        await api("/api/v1/stock/move", { from_location_id: this.moveWhole.id, location: target.id });
        toast(`✔ ${this.moveWhole.name} → ${target.path}`); this.reset();
      } catch (e) { toast("Fehler: " + e.message, 4000); }
    },
    async confirmLot(l) {
      await api("/api/v1/stock/confirm", { lot_id: l.id });
      this.checked[l.id] = true; l.needs_review = false; toast("✔ " + l.item);
    },
    async countLot(l, v) {
      await api("/api/v1/stock/count", { lot_id: l.id, quantity: +v });
      l.quantity = +v; this.checked[l.id] = true; l.needs_review = false; toast("✔ gezählt");
    },
    async assignAliasTo(body, msg) {
      try { await api("/api/v1/alias", body); toast("✔ " + msg); this.unknownCode = null; }
      catch (e) { toast(e.message, 5000); }
    },
    assignAliasToLoc(code) { return this.assignAliasTo({ location_id: this.loc.id, code }, "Etikett zugeordnet"); },
    assignAliasToItem(code) { return this.assignAliasTo({ item_id: this.item.id, code }, "Code zugeordnet"); },
    assignAliasToLot(code) { return this.assignAliasTo({ lot_id: this.lot.id, code }, "Code dem Bestand zugeordnet"); },

    // ------------------------------------------------ Schnell-Neuanlage
    startQuickCreate(code) { this.mode = "neu"; this.qc.barcode = code; this.unknownCode = null; if (this.loc) { this.qc.location = this.loc.code; this.qc.locationPath = this.loc.path; } },
    async catalogSearch() {
      this.qc.key = "";
      if (this.qc.name.trim().length < 2) { this.qc.catalog = []; return; }
      this.qc.catalog = (await api("/api/v1/catalog?q=" + encodeURIComponent(this.qc.name), undefined, "GET")).slice(0, 8);
    },
    async quickCreate() {
      const fd = new FormData();
      fd.append("name", this.qc.name); fd.append("catalog_key", this.qc.key); fd.append("location", this.qc.location);
      fd.append("quantity", this.qc.qty || 0); fd.append("barcode", this.qc.barcode);
      if (this.qc.category) fd.append("category_id", this.qc.category);
      const f = this.$refs.photo.files[0];
      if (f) fd.append("photo", f);
      try {
        const r = await api("/api/v1/items/quick", fd);
        toast("✔ angelegt: " + r.item.name);
        this.qc = { name: "", key: "", catalog: [], category: this.qc.category, location: this.qc.location, locationPath: this.qc.locationPath, qty: 1, barcode: "" };
        this.$refs.photo.value = "";
      } catch (e) { toast("Fehler: " + e.message, 4000); }
    },

    // ------------------------------------------------ Etiketten zuordnen
    async furnitureSearch(q) { this.assign.options = await api("/api/v1/locations?furniture=1&q=" + encodeURIComponent(q), undefined, "GET"); },
    async loadFurniture(f) {
      this.assign.furniture = f;
      this.assign.cells = await api(`/api/v1/locations/${f.id}/cells`, undefined, "GET");
      this.assign.idx = Math.max(0, this.assign.cells.findIndex((c) => !c.aliases.length));
      this.ledCell();
    },
    ledCell() {
      const c = this.assign.cells[this.assign.idx];
      if (c && this.caps.canLed) api("/api/v1/locate", { location_id: c.id, duration: 60 }).catch(() => {});
    },
    async assignCode(code) {
      const c = this.assign.cells[this.assign.idx];
      if (!c) { toast("Alle Fächer erledigt"); return; }
      try {
        const r = await api("/api/v1/alias", { location_id: c.id, code });
        c.aliases.push(r.code);
        this.assign.last = { cell: c, code: r.code };
        toast(`✔ ${c.name}`);
        this.assign.idx++;
        this.ledCell();
      } catch (e) { toast("Fehler: " + e.message, 4000); }
    },
    async undoAssign() {
      // Letzte Zuordnung zurücknehmen: gleiches Etikett dem vorherigen Fach wieder abnehmen, indem es neu zugeordnet wird
      const last = this.assign.last;
      if (!last) return;
      this.assign.idx = this.assign.cells.indexOf(last.cell);
      last.cell.aliases = last.cell.aliases.filter((a) => a !== last.code);
      this.assign.last = null;
      toast("Fach erneut scannen – das Etikett wird neu zugeordnet");
      this.ledCell();
    },
  };
}
