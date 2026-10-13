// Buchen (v0.15): Ausbuchen, Umbuchen (Artikel → Fach) und Einräumen (Fach → Artikel) per Handscanner oder Handy-Kamera.
// Nutzt cameraMixin() und beep() aus scan.js sowie api(), scanResolve(), toast(), locate() aus app.js.

function bookApp(cfg) {
  return {
    ...cameraMixin(),
    caps: cfg, mode: cfg.mode || "aus",
    modes: [
      { id: "aus", icon: "📤", label: "Ausbuchen", hint: "Artikel scannen → Menge → Enter" },
      { id: "um", icon: "🔀", label: "Umbuchen", hint: "Artikel scannen → Ziel-Fach scannen" },
      { id: "ein", icon: "📥", label: "Einräumen", hint: "Ziel-Fach scannen → dann alle Artikel, die dort hinkommen" },
    ],
    code: "", item: null, lot: null, choices: [], source: null, target: null, needQty: false,
    qty: "1", part: "", results: [], log: [],

    init() {
      window.addEventListener("lw:scan", (e) => { e.preventDefault(); beep(); this.handle(e.detail.code); });
      window.addEventListener("pagehide", () => this.stopCamera());
      this.focusScan();
    },
    led(payload) { if (this.caps.canLed) api("/api/v1/locate", payload).catch(() => {}); },  // leise: kein Hinweis, wenn keine LEDs
    short(path) { if (!path) return "ohne Ort"; const p = path.split(" / "); return p.slice(-2).join(" › "); },
    fmt(v) { return v === null || v === undefined ? "–" : String(+(+v).toFixed(3)).replace(".", ","); },
    num(v) { const n = parseFloat(String(v).replace(",", ".")); return Number.isFinite(n) ? n : NaN; },
    focusScan() { this.$nextTick(() => { const el = this.$refs.scan; if (el && !this.camOn) el.focus(); }); },
    focusQty() {
      // nach dem Einblenden des Mengenfelds fokussieren (x-show braucht einen Moment)
      const go = (n) => { const el = this.$refs.qty; if (el && el.offsetParent) { el.focus(); el.select(); } else if (n) setTimeout(() => go(n - 1), 30); };
      this.$nextTick(() => go(10));
    },
    setMode(m) { this.mode = m; this.reset(true); this.focusScan(); },
    reset(all = false) {
      this.item = this.lot = this.source = null; this.choices = []; this.needQty = false; this.qty = "1"; this.part = ""; this.results = [];
      if (all) this.target = null;
    },
    activeLots(item) { return (item.lots || []).filter((l) => !["lost", "used"].includes(l.status)); },
    lotLabel(l) {
      const q = l.stock_mode === "exact" ? `${this.fmt(l.quantity)} ${l.unit}` : ({ ok: "genug", low: "wenig", empty: "leer" }[l.coarse] || "");
      return `${this.short(l.location)}${l.batch ? " · Charge " + l.batch : ""}${l.serial ? " · SN " + l.serial : ""} – ${q}`;
    },
    prompt() {
      if (this.needQty) return "Menge eingeben → Enter (Bestand wird angelegt)";
      if (this.choices.length) return "Welcher Bestand? Anklicken oder Nummer + Enter";
      if (this.mode === "aus") return this.lot ? "Menge → Enter" : "Artikel scannen (oder erst das Fach)";
      if (this.mode === "um") return this.lot || (this.item && !this.activeLots(this.item).length) ? "Ziel-Fach scannen" : "Artikel scannen";
      return this.target ? "Artikel scannen – kommt nach „" + this.target.name + "“" : "Ziel-Fach scannen";
    },

    // ------------------------------------------------ Eingabe
    submitScan() {
      const v = this.code.trim();
      this.code = "";
      if (!v) return;
      if (this.choices.length && /^\d{1,2}$/.test(v)) {
        const l = this.choices[+v - 1];
        if (l) return this.chooseLot(l);
      }
      this.handle(v, true);
    },
    submitQty() {
      const v = this.qty.trim();
      if (/^\d{1,5}([.,]\d+)?$/.test(v)) return this.needQty ? this.commitAdd() : this.commitOut();
      // Handscanner hat in das Mengenfeld getippt: Vorgang abbrechen und den Code verarbeiten
      toast("Nicht gebucht – neuer Scan");
      this.reset();
      this.handle(v);
    },
    async handle(code, typed = false) {
      this.busy = true;
      const r = await scanResolve(code);
      this.busy = false;
      if (!r) return toast("Code nicht lesbar: " + code);
      if (r.kind === "location") return this.onLocation(r.location);
      if (r.kind === "item") return this.onItem(r.item, null);
      if (r.kind === "lot") return this.onItem(r.item, r.lot);
      if (typed && !/^[\x21-\x7e]{1,3}$/.test(code)) {
        const s = await api("/api/v1/search?q=" + encodeURIComponent(code) + "&limit=12", undefined, "GET").catch(() => ({ items: [] }));
        this.results = s.items || [];
        if (this.results.length) return;
      }
      toast("Unbekannter Code: " + code, 3500);
      this.focusScan();
    },
    useResult(it) { this.results = []; this.onItem(it, null); },

    // ------------------------------------------------ Ablauf
    onLocation(loc) {
      this.results = [];
      if (this.caps.canLed && loc.has_lights) this.led({ location_id: loc.id });
      if (this.mode === "ein") { this.reset(); this.target = loc; toast("Ziel: " + loc.path); return this.focusScan(); }
      if (this.mode === "um" && (this.lot || (this.item && !this.activeLots(this.item).length))) return this.arrive(loc);
      // Ausbuchen / Umbuchen ohne Artikel: Fach als Quelle – Bestände dort zur Auswahl
      this.reset();
      this.source = loc;
      const lots = (loc.lots || []).filter((l) => !["lost", "used"].includes(l.status));
      if (!lots.length) { toast("„" + loc.name + "“ ist leer"); return this.focusScan(); }
      if (lots.length === 1) return this.chooseLot(lots[0]);
      this.choices = lots;
      this.focusScan();
    },
    onItem(item, lot) {
      this.results = [];
      if (this.mode === "ein") return this.intoTarget(item, lot);
      this.item = item; this.lot = null; this.choices = []; this.needQty = false;
      let lots = this.activeLots(item);
      if (lot) return this.chooseLot(lot);
      if (this.source) {
        const here = lots.filter((l) => l.location_id === this.source.id || (l.location || "").startsWith(this.source.path));
        if (here.length) lots = here;
      }
      if (this.mode === "aus") lots = lots.filter((l) => l.stock_mode !== "exact" || l.quantity > 0);
      if (lots.length === 1) return this.chooseLot(lots[0]);
      if (!lots.length) {
        if (this.mode === "aus") { toast("Kein Bestand von „" + item.name + "“"); this.item = null; return this.focusScan(); }
        toast("Noch ohne Bestand – jetzt Ziel-Fach scannen, dann Menge");  // Umbuchen = Zuordnen
        return this.focusScan();
      }
      this.choices = lots;
      if (this.caps.canLed) this.led({ item_id: item.id });
      this.focusScan();
    },
    chooseLot(l) {
      this.choices = [];
      this.lot = l;
      if (!this.item || this.item.id !== l.item_id) this.item = { id: l.item_id, name: l.item, unit: l.unit, lots: [l] };
      if (this.caps.canLed && l.location_id) this.led({ location_id: l.location_id });
      if (this.mode === "ein") return this.commitMove(this.target, l);
      if (this.mode === "aus") {
        if (l.stock_mode === "exact") { this.qty = "1"; this.focusQty(); } else this.focusScan();
        return;
      }
      toast("Jetzt Ziel-Fach scannen");
      this.focusScan();
    },
    arrive(loc) {  // Umbuchen: Ziel gescannt
      if (this.lot) return this.commitMove(loc, this.lot, this.part);
      this.target = loc; this.needQty = true; this.qty = "1"; this.focusQty();
    },
    intoTarget(item, lot) {  // Einräumen
      if (!this.target) { toast("Erst das Ziel-Fach scannen"); return this.focusScan(); }
      this.item = item;
      if (lot) {
        if (lot.location_id === this.target.id) { toast("Liegt schon hier"); this.item = null; return this.focusScan(); }
        return this.commitMove(this.target, lot);
      }
      const all = this.activeLots(item);
      const cand = all.filter((l) => l.location_id !== this.target.id);
      if (cand.length === 1) return this.commitMove(this.target, cand[0]);
      if (!cand.length) {
        if (all.length) { toast("„" + item.name + "“ liegt schon hier"); this.item = null; return this.focusScan(); }
        this.needQty = true; this.qty = "1"; return this.focusQty();
      }
      this.choices = cand;
      this.focusScan();
    },

    // ------------------------------------------------ Buchen
    pushLog(text, undo) { this.log.unshift({ id: Date.now() + Math.random(), text, undo, done: false, t: new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) }); this.log = this.log.slice(0, 15); },
    async commitOut() {
      const n = this.num(this.qty);
      if (!(n > 0)) return toast("Menge fehlt");
      const l = this.lot;
      try {
        const r = await api("/api/v1/stock/remove", { lot_id: l.id, quantity: n, note: "Ausgebucht (Scan)" });
        beep();
        this.pushLog(`📤 ${this.fmt(n)} ${l.unit} ${l.item} aus ${this.short(l.location)} – Rest ${this.fmt(r.lot.quantity)}`, { kind: "out", lot_id: l.id, quantity: n });
        toast(`✔ ${this.fmt(n)} × ${l.item} ausgebucht – Rest ${this.fmt(r.lot.quantity)} ${l.unit}`);
        const keep = this.source;
        this.reset(); this.source = keep;
      } catch (e) { toast("Fehler: " + e.message, 4000); }
      this.focusScan();
    },
    async coarse(level) {
      try {
        await api("/api/v1/stock/coarse", { lot_id: this.lot.id, level });
        this.pushLog(`📤 ${this.lot.item}: Füllstand ${{ ok: "genug", low: "wenig", empty: "leer" }[level]}`, null);
        toast("✔ Füllstand gespeichert");
        this.reset();
      } catch (e) { toast("Fehler: " + e.message, 4000); }
      this.focusScan();
    },
    async commitMove(target, l, part = "") {
      if (l.location_id === target.id) { toast("Liegt schon in „" + target.name + "“"); this.reset(); return this.focusScan(); }
      const p = this.num(part);
      const whole = !(p > 0) || (l.stock_mode === "exact" && p >= l.quantity);
      const moved = whole ? (l.quantity || 0) : p;
      try {
        const r = await api("/api/v1/stock/move", { lot_id: l.id, location: target.id, quantity: whole ? null : p });
        beep();
        const what = l.stock_mode === "exact" ? `${this.fmt(moved)} ${l.unit} ` : "";
        this.pushLog(`${l.location ? "🔀" : "📌"} ${what}${l.item}: ${this.short(l.location)} → ${this.short(target.path)}`,
          { kind: "move", lot_id: r.lot.id, quantity: moved, location_id: l.location_id });
        toast(`✔ ${l.item} → ${target.name}`);
      } catch (e) { toast("Fehler: " + e.message, 4000); }
      const keep = this.mode === "ein" ? this.target : null;
      this.reset(); this.target = keep;
      this.focusScan();
    },
    async commitAdd() {
      const n = this.num(this.qty);
      if (!(n >= 0)) return toast("Menge fehlt");
      const it = this.item, target = this.target;
      try {
        const r = await api("/api/v1/stock/add", { item_id: it.id, location: target.id, quantity: n, note: "Zugeordnet (Scan)" });
        beep();
        this.pushLog(`📌 ${this.fmt(n)} ${it.unit || ""} ${it.name} → ${this.short(target.path)} (neu zugeordnet)`, { kind: "add", lot_id: r.lot.id, quantity: n });
        toast(`✔ ${it.name} → ${target.name}`);
      } catch (e) { toast("Fehler: " + e.message, 4000); }
      const keep = this.mode === "ein" ? target : null;
      this.reset(); this.target = keep;
      this.focusScan();
    },
    async undo(entry) {
      try {
        await api("/api/v1/stock/undo", entry.undo);
        entry.done = true;
        toast("↶ rückgängig gemacht");
      } catch (e) { toast("Rückgängig nicht möglich: " + e.message, 4000); }
      this.focusScan();
    },
  };
}
