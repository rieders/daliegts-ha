// Lagerwerk – Wandansicht: Module anordnen, Fächer finden, LED-Ketten (ohne Build-Schritt, Alpine.js)
"use strict";

function wallEditor(cfg) {
  return {
    ...cfg,
    data: { modules: [], unplaced: [], width: 100, height: 60, overlap: [], chains: [] },
    scale: 4, edit: false, sel: null, cell: null, drag: null, busy: false, freeMode: false, areaName: "", spot: null,
    q: "", hits: { cells: [], locations: [], count: 0 }, hitSet: new Set(),
    size: { w: 0, h: 0 },
    add: { open: false, side: null, kind: "template", template_id: "", type: "magazin", rows: 4, cols: 4, subdivision: 0, row_counts: "", name: "", w: 30, h: 20 },
    chains: [], specs: {}, chainsDirty: false, calib: null, checkColors: null, showLeds: false,
    gridForm: { template_id: "", rows: 8, cols: 4, merges: "", count_direction: "rows" },

    async init() {
      await this.load();
      this.fit();
      if (this.preset) this.$nextTick(() => this.applyHits(this.preset, true));
      window.addEventListener("lw:scan", (e) => { e.preventDefault(); this.onScan(e.detail.code); });
      window.addEventListener("resize", () => { if (!this.userZoom) this.fit(); this.$nextTick(() => this.updateSpot()); });
      this.$watch("cell", () => this.$nextTick(() => this.updateSpot()));
      this.$watch("scale", () => this.$nextTick(() => this.updateSpot()));
    },
    async load() {
      const r = await fetch(`/orte/${this.id}/wand.json`, { credentials: "same-origin" });
      if (r.ok) this.setData(await r.json());
    },
    setData(d) {
      this.data = d;
      this.chains = JSON.parse(JSON.stringify(d.chains || []));
      this.specs = {};
      d.modules.forEach((m) => { this.specs[m.id] = { ...m.led_spec }; });
      this.chainsDirty = false;
      if (this.sel) {
        this.sel = d.modules.find((m) => m.id === this.sel.id) || null;
        if (this.sel) this.size = { w: this.sel.w, h: this.sel.h };
      }
      if (this.cell && this.sel) this.cell = this.sel.cells.find((c) => c.id === this.cell.id) || null;
    },
    findCell(id) {
      for (const m of this.data.modules) {
        if (m.id === id) return { m, c: null };
        const c = m.cells.find((c) => c.id === id);
        if (c) return { m, c };
      }
      return null;
    },
    updateSpot() {
      // Markierungsring um das gewählte Fach – liegt über allem, damit er auch bei kleinen Fächern auffällt
      if (!this.cell) { this.spot = null; return; }
      const el = this.$root.querySelector(`.wcell[data-id="${this.cell.id}"]`);
      const canvas = el && el.closest(".wall-canvas");
      if (!el || !canvas) { this.spot = null; return; }
      const a = el.getBoundingClientRect(), c = canvas.getBoundingClientRect(), pad = 7;
      this.spot = { x: a.left - c.left - pad, y: a.top - c.top - pad, w: a.width + 2 * pad, h: a.height + 2 * pad, t: Date.now() };
    },
    reveal(id) {
      const el = this.$root.querySelector(`[data-id="${id}"]`);
      if (el) el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    },

    // ---------------------------------------------------------- Zoom
    fit() {
      const el = this.$refs.scroll;
      if (!el) return;
      const w = el.clientWidth - 16, ms = this.data.modules;
      if (!this.edit && ms.length) {
        // Ansehen: auf die Möbel zoomen (Breite und Höhe), damit Fächer lesbar sind
        const right = Math.max(...ms.map((m) => m.x + m.w)), bottom = Math.max(...ms.map((m) => m.y + m.h));
        const h = Math.max(260, window.innerHeight * 0.72);
        this.scale = Math.max(1.5, Math.min(30, (w - 8) / (right + 2), h / (bottom + 6)));
      } else {
        this.scale = Math.max(1.5, Math.min(12, w / (this.data.width + 20)));
      }
      this.userZoom = false;
    },
    bounds() {
      const ms = this.data.modules;
      if (this.edit || !ms.length) return { w: this.data.width + 20, h: this.data.height + 12 };
      return { w: Math.max(...ms.map((m) => m.x + m.w)) + 2, h: Math.max(...ms.map((m) => m.y + m.h)) + 4 };
    },
    zoomBy(f) { this.scale = Math.max(0.8, Math.min(30, this.scale * f)); this.userZoom = true; },

    // ---------------------------------------------------------- Auswahl
    toggleEdit() { this.edit = !this.edit; this.cell = null; if (!this.edit) this.add.open = false; if (!this.userZoom) this.$nextTick(() => this.fit()); },
    selectModule(m) {
      if (this.calib) { this.calibPick(m); return; }
      if (this.justDragged) return;
      this.sel = m; this.cell = null; this.size = { w: m.w, h: m.h };
      if (m.count >= 2) this.suggestGrid(m);
    },

    // ---------------------------------------------------------- Raster für vorhandene Fächer
    templatesFor(m) {
      // passende Vorlagen (gleiche Fachzahl) zuerst
      return [...this.templates].sort((a, b) => (b.cells === m.count) - (a.cells === m.count) || a.name.localeCompare(b.name));
    },
    suggestGrid(m) {
      const match = this.templates.find((t) => t.cells === m.count);
      const cols = 4, full = Math.floor(m.count / cols), rest = m.count % cols;
      this.gridForm = { template_id: match ? String(match.id) : "", rows: full + (rest ? 1 : 0), cols,
        merges: rest === 1 ? `${full + 1} 1 1 ${cols}` : "", count_direction: "rows" };
    },
    gridHint(m) {
      if (this.gridForm.template_id) {
        const t = this.templates.find((t) => String(t.id) === String(this.gridForm.template_id));
        if (!t) return "";
        const d = t.cells - m.count;
        return d === 0 ? "Passt genau." + (t.w ? ` Größe wird auf ${t.w} × ${t.h} cm gesetzt.` : "")
          : d > 0 ? `Vorlage hat ${d} Fächer mehr – bleiben leer.` : `Vorlage hat ${-d} Fächer zu wenig – die letzten bleiben ohne Platz.`;
      }
      let n = 0;
      const covered = new Set();
      String(this.gridForm.merges || "").split(/[;\n]/).forEach((l) => {
        const v = l.trim().split(/[\s,]+/).map(Number);
        if (v.length === 4 && v.every((x) => x > 0)) for (let r = v[0]; r < v[0] + v[2]; r++) for (let c = v[1]; c < v[1] + v[3]; c++) if (r !== v[0] || c !== v[1]) covered.add(`${r},${c}`);
      });
      n = this.gridForm.rows * this.gridForm.cols - covered.size;
      return `Raster ergibt ${n} Fächer, vorhanden sind ${m.count}.` + (n === m.count ? " ✓" : "");
    },
    async applyGrid() {
      const f = this.gridForm, body = { action: "grid", id: this.sel.id };
      if (f.template_id) body.template_id = f.template_id; else Object.assign(body, { rows: f.rows, cols: f.cols, merges: f.merges, count_direction: f.count_direction });
      await this.act(body);
    },
    selectCell(m, c) {
      if (this.calib) { this.calibPick(m); return; }
      if (this.edit) { this.selectModule(m); return; }
      this.sel = m; this.cell = c;
      if (this.canLed && c.lit) locate({ location_id: c.id });
    },
    openModule(m) {
      if (this.edit) return;
      lwGo(m.rows ? `/orte/${m.id}` : `/orte/${m.id}/wand`);
    },

    // ---------------------------------------------------------- Suche
    async search() {
      const q = this.q.trim();
      if (q.length < 2) { this.hits = { cells: [], locations: [], count: 0 }; this.hitSet = new Set(); return; }
      const r = await fetch(`/orte/${this.id}/wand/suche?q=${encodeURIComponent(q)}`, { credentials: "same-origin" });
      if (!r.ok) return;
      this.presetLabel = "";
      this.applyHits(await r.json());
      if (!this.hits.count) toast("Keine Treffer in dieser Wand");
    },
    applyHits(h, jump = false) {
      this.hits = { cells: [], locations: [], count: 0, places: [], ...h };
      const s = new Set(this.hits.cells);
      // Module mit Treffern in ihren Fächern ebenfalls markieren
      this.data.modules.forEach((m) => { if (m.cells.some((c) => s.has(c.id))) s.add(m.id); });
      this.hitSet = s;
      if (jump && this.hits.cells.length) {
        const f = this.findCell(this.hits.cells[0]);
        if (f && this.hits.cells.length === 1) { this.sel = f.m; this.cell = f.c; }
        this.$nextTick(() => this.reveal(this.hits.cells[0]));
      }
    },
    async enterSearch() {
      // Handscanner tippt ins Suchfeld: Codes (Fach-/Artikel-Etikett) direkt auflösen, normale Suchbegriffe bleiben Suche
      const q = this.q.trim();
      if (q.length < 3 || /\s/.test(q)) return;
      const r = await scanResolve(q);
      if (r && ["location", "item", "lot"].includes(r.kind)) { this.q = ""; this.onScan(q); }
    },
    async onScan(code) {
      // Handscanner in der Lageransicht: Fach dieser Fläche direkt auswählen, sonst normal springen
      const r = await scanResolve(code);
      if (r && r.kind === "location") {
        const f = this.findCell(r.location.id);
        if (f) { this.presetLabel = r.location.name; this.applyHits({ cells: [r.location.id], locations: [r.location.id] }, true); this.sel = f.m; this.cell = f.c; return; }
      } else if (r && (r.kind === "item" || r.kind === "lot")) {
        lwGo(`/orte/${this.id}/wand?artikel=${r.item.id}`);
        return;
      }
      scanNavigate(code);
    },
    clearHits() { this.hits = { cells: [], locations: [], count: 0, places: [] }; this.hitSet = new Set(); this.presetLabel = ""; this.q = ""; },
    goPlace(p) {
      if (!p.cell && p.href) { lwGo(p.href); return; }
      const f = this.findCell(p.cell);
      if (!f) return;
      this.sel = f.m; this.cell = f.c;
      this.reveal(p.cell);
      if (this.canLed && ((f.c && f.c.lit) || (!f.c && f.m.lit))) locate({ location_id: p.cell });
    },

    // ---------------------------------------------------------- Ziehen mit Einrasten
    startDrag(ev, m) {
      if (!this.edit || ev.button > 0) return;
      ev.preventDefault();
      const el = ev.currentTarget;
      el.setPointerCapture(ev.pointerId);
      this.drag = { id: m.id, m, sx: ev.clientX, sy: ev.clientY, ox: m.x, oy: m.y, moved: false };
      const onMove = (e) => this.onDrag(e);
      const onUp = (e) => {
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
        el.removeEventListener("pointercancel", onUp);
        this.endDrag(e);
      };
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
    },
    onDrag(e) {
      const d = this.drag;
      if (!d) return;
      const dx = (e.clientX - d.sx) / this.scale, dy = (e.clientY - d.sy) / this.scale;
      if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 4) return;
      d.moved = true;
      const snapped = this.snap(d.m, d.ox + dx, d.oy + dy);
      d.m.x = snapped.x; d.m.y = snapped.y;
      this.data.overlap = this.overlaps();
    },
    snap(m, x, y) {
      const tol = 10 / this.scale; // 10 Bildschirmpixel
      let bx = Math.round(x * 2) / 2, by = Math.round(y * 2) / 2, best = { x: tol, y: tol };
      for (const o of this.data.modules) {
        if (o.id === m.id) continue;
        for (const cx of [o.x, o.x + o.w, o.x - m.w, o.x + o.w - m.w]) {
          const dd = Math.abs(cx - x); if (dd < best.x) { best.x = dd; bx = cx; }
        }
        for (const cy of [o.y, o.y + o.h, o.y - m.h, o.y + o.h - m.h]) {
          const dd = Math.abs(cy - y); if (dd < best.y) { best.y = dd; by = cy; }
        }
      }
      return { x: Math.max(-500, bx), y: Math.max(-500, by) };
    },
    overlaps() {
      const ms = this.data.modules, out = [];
      for (let i = 0; i < ms.length; i++) for (let j = i + 1; j < ms.length; j++) {
        const a = ms[i], b = ms[j];
        if (a.x < b.x + b.w - 0.05 && b.x < a.x + a.w - 0.05 && a.y < b.y + b.h - 0.05 && b.y < a.y + a.h - 0.05) out.push(a.id, b.id);
      }
      return out;
    },
    async endDrag() {
      const d = this.drag;
      this.drag = null;
      if (!d || !d.moved) return;
      this.justDragged = true;
      setTimeout(() => { this.justDragged = false; }, 50);
      this.sel = d.m; this.size = { w: d.m.w, h: d.m.h };
      await this.act({ action: "move", id: d.m.id, x: d.m.x, y: d.m.y }, true);
      if (this.data.overlap.length) toast("Achtung: Module überlappen (rot markiert)");
    },

    // ---------------------------------------------------------- Aktionen
    async act(body, quiet = false) {
      this.busy = true;
      try {
        const r = await api(`/orte/${this.id}/wand/aktion`, body);
        if (!r.ok) { toast(r.error || "Fehler"); await this.load(); return false; }
        const keepScale = this.userZoom;
        this.setData(r.data);
        if (!keepScale) this.fit();
        if (r.message && !quiet) toast(r.message);
        return r;
      } catch (e) { toast("Fehler: " + e.message); await this.load(); return false; } finally { this.busy = false; }
    },

    // ---------------------------------------------------------- Neues Modul
    sideLabel() {
      const s = this.add.side, n = this.sel ? `„${this.sel.name}“` : "";
      return !s || !this.sel ? "(rechts ans Ende)" : { right: `rechts neben ${n}`, left: `links neben ${n}`, above: `über ${n}`, below: `unter ${n}` }[s];
    },
    openAdd(side) {
      this.add.open = true;
      this.add.side = side;
      if (this.sel && (side === "above" || side === "below")) { this.add.w = this.sel.w; } // gleich breit stapeln
      if (!this.add.name) this.add.name = this.nextName();
    },
    nextName() {
      const base = this.add.kind === "box" ? "Kiste" : (this.add.type === "regal" ? "Regal" : "Magazin");
      let n = this.data.modules.length + this.data.unplaced.length + 1;
      const names = new Set([...this.data.modules, ...this.data.unplaced].map((m) => m.name));
      while (names.has(`${base} ${n}`)) n++;
      return `${base} ${n}`;
    },
    tplGroups() {
      const order = ["Eigene Vorlagen", "IKEA", "Sortimentskästen", "Allgemein"], groups = {}, pos = (g) => (order.indexOf(g) < 0 ? 50 : order.indexOf(g));
      this.templates.forEach((t) => { (groups[t.group || "Allgemein"] = groups[t.group || "Allgemein"] || []).push(t); });
      return Object.keys(groups).sort((a, b) => pos(a) - pos(b) || a.localeCompare(b)).map((name) => ({ name, items: groups[name] }));
    },
    tplNote() {
      const t = this.templates.find((t) => String(t.id) === String(this.add.template_id));
      return t ? [t.w && t.h ? `${t.w} × ${t.h} cm` : "", t.notes].filter(Boolean).join(" · ") : "";
    },
    applyTemplate() {
      const t = this.templates.find((t) => String(t.id) === String(this.add.template_id));
      if (!t) return;
      if (t.w && t.h) { this.add.w = t.w; this.add.h = t.h; }
      const base = t.name.split(" (")[0].replace(/\s+\d+\s*[×x]\s*\d+.*$/, "") || t.name;
      if (!this.add.name || /^(Magazin|Regal|Kiste) \d+$/.test(this.add.name)) this.add.name = base;
    },
    previewDims() {
      if (this.add.kind === "template") {
        const t = this.templates.find((t) => String(t.id) === String(this.add.template_id));
        if (!t) return { rows: 0, cols: 0, row_counts: null, merges: [] };
        return { rows: t.rows || 1, cols: t.cols || 1, row_counts: t.row_counts, merges: t.merges || [], gaps: t.gaps || [] };
      }
      const rc = String(this.add.row_counts || "").split(/[,;]/).map((x) => parseInt(x, 10)).filter((x) => x > 0);
      return { rows: this.add.rows || 1, cols: this.add.cols || 1, row_counts: rc.length ? rc : null, merges: [] };
    },
    previewCells() {
      const d = this.previewDims(), out = [], covered = new Set(), merged = {};
      if (!d.rows) return out;
      (d.gaps || []).forEach(([r, c]) => { for (let rr = 1; rr <= d.rows; rr++) if (!r || r === rr) covered.add(`${rr},${c}`); });
      (d.merges || []).forEach(([r, c, rs, cs]) => {
        merged[`${r},${c}`] = [rs, cs];
        for (let rr = r; rr < r + rs; rr++) for (let cc = c; cc < c + cs; cc++) if (rr !== r || cc !== c) covered.add(`${rr},${cc}`);
      });
      // Fachzahl je Reihe = Anzahl Schubladen (breite zählen als eine) – wie auf dem Server
      for (let r = 1; r <= Math.min(d.rows, 60); r++) {
        const want = d.row_counts && d.row_counts[r - 1] ? d.row_counts[r - 1] : null;
        let c = 1, placed = 0;
        while (c <= 60) {
          if ((want === null && c > d.cols) || (want !== null && placed >= want)) break;
          if (covered.has(`${r},${c}`)) { c++; continue; }
          const [rs, cs] = merged[`${r},${c}`] || [1, 1];
          out.push({ r, c, rs, cs });
          placed++; c += cs;
        }
      }
      return out;
    },
    async createModule() {
      const a = this.add;
      if (a.kind === "template" && !a.template_id) { toast("Bitte eine Vorlage wählen"); return; }
      const body = { action: "add", kind: a.kind === "box" ? "box" : "grid", name: a.name, w: a.w, h: a.h,
        ref: this.sel && a.side ? this.sel.id : null, side: a.side || "right" };
      if (a.kind === "template") body.template_id = a.template_id;
      else if (a.kind === "grid") Object.assign(body, { type: a.type, rows: a.rows, cols: a.cols, subdivision: a.subdivision, row_counts: a.row_counts });
      else body.type = a.type;
      const r = await this.act(body);
      if (r) {
        const created = this.data.modules.reduce((x, m) => (!x || m.id > x.id ? m : x), null);
        if (created) { this.sel = created; this.size = { w: created.w, h: created.h }; }
        a.open = false; a.name = "";
      }
    },

    // ---------------------------------------------------------- LED-Ketten
    modName(id) { const m = this.data.modules.find((m) => m.id === id); return m ? m.name : `#${id}`; },
    spec(id) {
      if (!this.specs[id]) this.specs[id] = { leds_per_cell: 3, led_gap: 0, led_row_gap: 0, serpentine: true, led_start_corner: "top_left", led_direction: "rows" };
      const s = this.specs[id];
      for (const [k, v] of Object.entries({ led_mode: "cell", xy_rows_side: "left", xy_cols_side: "top", xy_first: "rows", xy_rows_reverse: false, xy_cols_reverse: false, xy_gap: 0 })) if (s[k] === undefined) s[k] = v;
      return s;
    },
    ledsOf(id) {
      // Schätzung im Browser (exakt rechnet der Server beim Speichern)
      const m = this.data.modules.find((m) => m.id === id);
      if (!m) return 0;
      const s = this.spec(id), orig = m.led_spec || {};
      const same = ["leds_per_cell", "led_gap", "led_row_gap", "led_direction", "led_mode", "xy_gap"].every((k) => s[k] === orig[k]);
      if (same) return m.leds;
      if (!m.rows) return s.leds_per_cell;
      if (s.led_mode === "xy") return m.rows + m.cols + (s.xy_gap || 0);
      const lines = s.led_direction === "rows" ? m.rows : m.cols, along = s.led_direction === "rows" ? m.cols : m.rows;
      return lines * along * (s.leds_per_cell + s.led_gap) - s.led_gap + (lines - 1) * s.led_row_gap;
    },
    // ---------------------------------------------------------- LEDs in der Ansicht (v0.30)
    moduleStart(m) {
      for (const ch of this.chains) { const i = ch.modules.findIndex((e) => e.id === m.id); if (i >= 0) return { start: this.chainStart(ch, i), placed: true }; }
      return { start: m.led_start || 0, placed: !!m.led_placed };
    },
    xyOf(m) {
      // Kreuz-Modus: LED-Nummer je Zeile / je Spalte (live aus den Einstellungen, auch ungespeichert)
      const s = this.spec(m.id);
      if (!m.rows || s.led_mode !== "xy") return null;
      const { start, placed } = this.moduleStart(m);
      const grp = (n, first, rev) => { const a = Array.from({ length: n }, (_, i) => first + i); return rev ? a.reverse() : a; };
      let rows, cols;
      if (s.xy_first === "cols") { cols = grp(m.cols, start, s.xy_cols_reverse); rows = grp(m.rows, start + m.cols + (s.xy_gap || 0), s.xy_rows_reverse); }
      else { rows = grp(m.rows, start, s.xy_rows_reverse); cols = grp(m.cols, start + m.rows + (s.xy_gap || 0), s.xy_cols_reverse); }
      return { rows, cols, rs: s.xy_rows_side, cs: s.xy_cols_side, placed };
    },
    ledDots(m) {
      if (!this.showLeds || !m.rows) return [];
      const dots = [], xy = this.xyOf(m), c = this.cell && m.cells.find((x) => x.id === this.cell.id);
      if (xy) {
        xy.rows.forEach((n, i) => dots.push({ k: "r" + i, x: xy.rs === "left" ? 0 : 100, y: (i + 0.5) / m.rows * 100, n: xy.placed ? n : "", on: !!c && i + 1 >= c.r && i + 1 < c.r + c.rs, kind: "k-row" }));
        xy.cols.forEach((n, i) => dots.push({ k: "c" + i, x: (i + 0.5) / m.cols * 100, y: xy.cs === "top" ? 0 : 100, n: xy.placed ? n : "", on: !!c && i + 1 >= c.c && i + 1 < c.c + c.cs, kind: "k-col" }));
        return dots;
      }
      m.cells.forEach((cl) => {
        const nums = (cl.leds || []).flatMap(([s, n]) => Array.from({ length: Math.min(n, 12) }, (_, i) => s + i));
        nums.forEach((n, i) => dots.push({ k: cl.id + "_" + i, x: (cl.c - 1 + cl.cs * (i + 0.5) / nums.length) / m.cols * 100, y: (cl.r - 1 + cl.rs * 0.88) / m.rows * 100, n, on: !!c && c.id === cl.id, kind: "k-cell" }));
      });
      return dots;
    },
    chainStart(ch, idx) {
      let pos = ch.start || 0;
      for (let i = 0; i < idx; i++) pos += this.ledsOf(ch.modules[i].id) + (ch.modules[i].gap || 0);
      return pos;
    },
    rangeText(ch, idx) { const s = this.chainStart(ch, idx), n = this.ledsOf(ch.modules[idx].id); return `LED ${s}–${s + n - 1}`; },
    chainEnd(ch) { const n = ch.modules.length; return n ? this.chainStart(ch, n - 1) + this.ledsOf(ch.modules[n - 1].id) : ch.start || 0; },
    ctrlLeds(ch) { const c = this.controllers.find((c) => c.id === ch.controller_id); return c ? c.leds : 0; },
    moveInChain(ch, i, d) { const j = i + d; if (j < 0 || j >= ch.modules.length) return; const t = ch.modules[i]; ch.modules[i] = ch.modules[j]; ch.modules[j] = t; ch.modules = [...ch.modules]; this.chainsDirty = true; },
    autoOrder(ch) {
      // Reihen von unten nach oben, abwechselnd links->rechts / rechts->links (typische Verkabelung über gestapelte Magazine)
      const mods = ch.modules.map((e) => ({ e, m: this.data.modules.find((m) => m.id === e.id) })).filter((x) => x.m);
      const rows = [];
      mods.sort((a, b) => (b.m.y + b.m.h) - (a.m.y + a.m.h)).forEach((x) => {
        const row = rows.find((r) => Math.abs(r.bottom - (x.m.y + x.m.h)) < Math.min(x.m.h, r.h) / 2);
        if (row) row.items.push(x); else rows.push({ bottom: x.m.y + x.m.h, h: x.m.h, items: [x] });
      });
      ch.modules = rows.flatMap((r, i) => r.items.sort((a, b) => (i % 2 ? b.m.x - a.m.x : a.m.x - b.m.x)).map((x) => x.e));
      this.chainsDirty = true;
    },
    // ---------------------------------------------------------- Einmessen (v0.21)
    // Nacheinander pulsiert die erste LED des nächsten Abschnitts; man tippt das Modul an, an dem es leuchtet.
    // Aus den LED-Zahlen der Module ergibt sich, wo der nächste Abschnitt beginnt.
    calibChain() { return this.calib ? this.chains[this.calib.ci] : null; },
    calibIndex(id) { const ch = this.calibChain(); return ch ? ch.modules.findIndex((e) => e.id === id) : -1; },
    startCalib(ci) {
      const ch = this.chains[ci];
      if (!ch || !ch.controller_id) { toast("Bitte zuerst einen Controller wählen"); return; }
      this.edit = false; this.sel = null; this.cell = null; this.checkColors = null;
      this.calib = { ci, orig: JSON.parse(JSON.stringify(ch)), pos: ch.start || 0, error: "" };
      ch.modules = [];
      this.calibShow();
      this.$nextTick(() => { const el = this.$root.querySelector(".calib-card"); if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" }); });
    },
    async calibShow() {
      const ch = this.calibChain();
      if (!ch) return;
      const last = ch.modules[ch.modules.length - 1];
      this.calib.pos = this.chainEnd(ch) + (last ? last.gap || 0 : 0);  // Lücke nach dem letzten Modul zählt mit
      const done = ch.modules.map((e, i) => [this.chainStart(ch, i), this.ledsOf(e.id)]);
      try {
        const r = await api(`/wled/${ch.controller_id}/einmessen`, { pos: this.calib.pos, n: 2, done });
        this.calib.error = r.ok ? "" : (r.error || "Fehler");
      } catch (e) { this.calib.error = "WLED nicht erreichbar: " + e.message; }
    },
    calibPick(m) {
      const ch = this.calibChain();
      if (ch.modules.some((e) => e.id === m.id)) { toast(`„${m.name}“ ist schon Nummer ${this.calibIndex(m.id) + 1}`); return; }
      ch.modules = [...ch.modules, { id: m.id, gap: 0 }];
      this.chainsDirty = true;
      this.calibShow();
    },
    calibShift(d) {
      // Leuchtet nicht die erste LED des Moduls (Kabelbrücke mit LEDs, falsche LED-Zahl): Startpunkt verschieben
      const ch = this.calibChain(), n = ch.modules.length;
      if (n) { const e = ch.modules[n - 1]; if ((e.gap || 0) + d < 0) return; e.gap = (e.gap || 0) + d; ch.modules = [...ch.modules]; }
      else { if ((ch.start || 0) + d < 0) return; ch.start = (ch.start || 0) + d; }
      this.chainsDirty = true;
      this.calibShow();
    },
    calibUndo() {
      const ch = this.calibChain();
      if (!ch.modules.length) return;
      ch.modules = ch.modules.slice(0, -1);
      this.calibShow();
    },
    async calibCancel() {
      const ch = this.calibChain(), o = this.calib.orig;
      ch.start = o.start; ch.modules = o.modules;
      const cid = ch.controller_id;
      this.calib = null;
      try { await api(`/wled/${cid}/einmessen`, { aus: true }); } catch (e) { /* egal */ }
    },
    async calibDone() {
      const ch = this.calibChain();
      if (!ch.modules.length) { toast("Noch kein Modul angetippt"); return; }
      const ci = this.calib.ci;
      this.calib = null;
      const r = await this.act({ action: "chains", chains: this.chains.map((c) => ({ controller_id: c.controller_id, start: c.start, modules: c.modules.map((e) => ({ id: e.id, gap: e.gap || 0 })) })),
        specs: Object.fromEntries(this.chains.flatMap((c) => c.modules.map((e) => [e.id, this.spec(e.id)]))) });
      if (r) this.checkChain(ci);
    },
    async checkChain(ci) {
      const ch = this.chains[ci];
      if (!ch || !ch.modules.length) return;
      const ranges = ch.modules.map((e, i) => [this.chainStart(ch, i), this.ledsOf(e.id)]);
      try {
        const r = await api(`/wled/${ch.controller_id}/kette-pruefen`, { ranges });
        if (r.ok) this.checkColors = { ci, colors: r.colors };
      } catch (e) { toast("WLED nicht erreichbar: " + e.message); }
    },
    checkColor(id) {
      if (!this.checkColors) return "";
      const ch = this.chains[this.checkColors.ci], i = ch ? ch.modules.findIndex((e) => e.id === id) : -1;
      return i < 0 ? "" : "#" + this.checkColors.colors[i % this.checkColors.colors.length];
    },
    async saveChains() {
      const specs = {};
      this.chains.forEach((ch) => ch.modules.forEach((e) => { specs[e.id] = this.spec(e.id); }));
      const chains = this.chains.map((ch) => ({ controller_id: ch.controller_id, start: ch.start, modules: ch.modules.map((e) => ({ id: e.id, gap: e.gap || 0 })) }));
      await this.act({ action: "chains", chains, specs });
    },
  };
}
