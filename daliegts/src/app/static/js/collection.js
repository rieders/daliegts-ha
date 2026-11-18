// DaLiegt’s – Sammlung (v0.33): Erfassen von Tonträgern und Büchern (Alpine-Komponenten, ohne Build-Schritt).
// Gespeichert wird über /api/v1/collection/… (Sitzung + X-Lagerwerk-Header über api()); unter Home Assistant
// setzt ingress.js den Ingress-Pfad vor alle „/…“-Adressen.
"use strict";

function fmtLen(s) {
  if (!s && s !== 0) return "";
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}
function parseLen(v) {
  v = String(v || "").trim();
  if (!v) return null;
  const p = v.split(":").map((x) => parseInt(x, 10)).filter((x) => !isNaN(x));
  if (!p.length) return null;
  return p.reduce((a, b) => a * 60 + b, 0);
}
function qs(params) {
  return Object.entries(params).filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "")
    .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(String(v).trim())).join("&");
}
function isbnDigits(code) { return String(code || "").toUpperCase().replace(/[^0-9X]/g, ""); }
function looksLikeIsbn(code) {
  const d = isbnDigits(code);
  return (d.length === 13 && (d.startsWith("978") || d.startsWith("979"))) || d.length === 10;
}

// ------------------------------------------------------------------ Tonträger
function recordForm(cfg) {
  const data = cfg.data || {};
  return {
    ...cameraMixin(),
    id: cfg.id,
    d: {
      artist: data.artist || "", album: data.album || "", year: data.year || "", medium: data.medium || "LP", genre: data.genre || "",
      label: data.label || "", catalog_no: data.catalog_no || "", country: data.country || "", barcode: data.barcode || "",
      media_condition: data.media_condition || "", sleeve_condition: data.sleeve_condition || "", digitized: !!data.digitized,
      digitized_path: data.digitized_path || "", musicbrainz_id: data.musicbrainz_id || "", discogs_id: data.discogs_id || "",
      tracks: (data.tracks || []).map((t) => ({ ...t })),
    },
    s: { artist: "", album: "", barcode: data.barcode || "", catno: "" },
    hits: [], notes: [], busy: false, searched: false, saving: false,
    preview: { ...(cfg.covers || {}) }, urls: {}, files: {},
    fmtLen, parseLen,
    init() { if (!this.id && this.s.barcode) this.search(); },
    handle(code) { this.stopCamera(); this.s.barcode = code; this.d.barcode = code; this.search(); },
    async search() {
      this.busy = true; this.notes = []; this.hits = [];
      try {
        const r = await api("/api/v1/collection/records/search?" + qs(this.s), undefined, "GET");
        this.hits = r.hits; this.notes = r.notes || [];
      } catch (e) { this.notes = [e.message]; }
      this.busy = false; this.searched = true;
    },
    async pick(h) {
      this.busy = true;
      try {
        const r = await api("/api/v1/collection/records/release?" + qs({ source: h.source, id: h.id }), undefined, "GET");
        const x = r.hit;
        for (const k of ["artist", "album", "year", "genre", "label", "catalog_no", "country", "barcode"]) if (x[k]) this.d[k] = x[k];
        if (x.medium) this.d.medium = x.medium;
        if (x.musicbrainz_id) this.d.musicbrainz_id = x.musicbrainz_id;
        if (x.discogs_id) this.d.discogs_id = x.discogs_id;
        if (x.tracks && x.tracks.length) this.d.tracks = x.tracks;
        if (x.cover_front) { this.urls.front = x.cover_front; if (!this.files.front) this.preview.front = x.cover_front; }
        if (x.cover_back) { this.urls.back = x.cover_back; if (!this.files.back) this.preview.back = x.cover_back; }
        this.hits = [];
        toast("Übernommen – bitte Zustand und Lagerort ergänzen.");
      } catch (e) { toast("Fehler: " + e.message); }
      this.busy = false;
    },
    fileChosen(role, ev) {
      const f = ev.target.files && ev.target.files[0];
      this.files[role] = !!f;
      if (f) this.preview[role] = URL.createObjectURL(f);
    },
    addTrack() {
      const last = this.d.tracks[this.d.tracks.length - 1];
      this.d.tracks.push({ side: last ? last.side : "A", position: last ? (last.position || 0) + 1 : 1, title: "", artist: "", length_seconds: null });
    },
    async save() {
      this.saving = true;
      const fd = new FormData(this.$refs.form);
      if (this.id) fd.set("id", this.id);
      fd.set("digitized", this.d.digitized ? "1" : "0");
      if (!this.d.digitized) fd.set("digitized_path", "");
      fd.set("tracks", JSON.stringify(this.d.tracks.filter((t) => (t.title || "").trim())));
      for (const role of ["front", "back"]) if (this.urls[role] && !this.files[role]) fd.set("cover_" + role + "_url", this.urls[role]);
      for (const [k, v] of [...fd.entries()]) if (v instanceof File && !v.name) fd.delete(k);
      try {
        const r = await api("/api/v1/collection/records", fd);
        toast(r.created ? "Tonträger angelegt." : "Tonträger aktualisiert.");
        lwGo(r.record.url);
      } catch (e) { toast("Fehler: " + e.message, 5000); this.saving = false; }
    },
  };
}

// ------------------------------------------------------------------ Bücher (Serien-Scan, Suche ohne ISBN, Formular)
const BOOK_EMPTY = { title: "", subtitle: "", authors: "", publisher: "", year: "", edition: "", isbn13: "", language: "", pages: "",
  series: "", series_number: "", genre: "", binding: "", condition: "", signed: false, read: false, market_value: "" };

function bookForm(cfg) {
  const data = cfg.data || {};
  const d = { ...BOOK_EMPTY };
  for (const k of Object.keys(BOOK_EMPTY)) if (data[k] !== undefined && data[k] !== null) d[k] = data[k];
  if (Array.isArray(data.authors)) d.authors = data.authors.join("; ");
  if (data.market && data.market.value !== null && data.market.value !== undefined) d.market_value = String(data.market.value).replace(".", ",");
  return {
    ...cameraMixin(),
    id: cfg.id,
    mode: cfg.id ? "form" : "scan",
    d, isbnInput: "", existing: null, sources: [], notes: [], busy: false, saving: false,
    autoSave: false, saved: [],
    s: { title: "", author: "" }, hits: [], searched: false,
    preview: { ...(cfg.covers || {}) }, urls: {}, files: {},
    init() {
      try { this.autoSave = localStorage.getItem("lw-book-autosave") === "1"; } catch (_) { /* egal */ }
      this.$watch("autoSave", (v) => { try { localStorage.setItem("lw-book-autosave", v ? "1" : "0"); } catch (_) { /* egal */ } });
      if (!this.id && d.isbn13) this.lookup(d.isbn13);
    },
    handle(code) {
      if (!looksLikeIsbn(code)) { toast("Kein ISBN-Barcode: " + code); return; }
      this.lookup(code);
    },
    resetBook() {
      this.d = { ...BOOK_EMPTY }; this.existing = null; this.sources = []; this.notes = [];
      this.preview = {}; this.urls = {}; this.files = {};
      this.$refs.form.querySelectorAll("input[type=file]").forEach((i) => { i.value = ""; });
    },
    async lookup(raw) {
      const isbn = isbnDigits(raw);
      if (!isbn) return;
      this.resetBook();
      this.d.isbn13 = isbn;
      this.busy = true;
      try {
        const r = await api("/api/v1/collection/books/lookup?" + qs({ isbn }), undefined, "POST");
        this.notes = r.notes || [];
        this.existing = r.existing;
        if (r.book) this.fill(r.book);
        else this.notes.unshift("Zu dieser ISBN nichts gefunden – bitte von Hand eintragen.");
        if (r.book && this.autoSave && this.mode === "scan" && !this.existing) { await this.save(); }
      } catch (e) { this.notes = [e.message]; }
      this.busy = false;
    },
    fill(b) {
      for (const k of ["title", "subtitle", "publisher", "year", "edition", "language", "pages", "series", "series_number", "genre", "binding"]) {
        if (b[k]) this.d[k] = b[k];
      }
      if (b.isbn13) this.d.isbn13 = b.isbn13;
      if (b.authors && b.authors.length) this.d.authors = Array.isArray(b.authors) ? b.authors.join("; ") : b.authors;
      if (b.cover_url) { this.urls.front = b.cover_url; this.preview.front = b.cover_url; }
      this.sources = b.sources || (b.source ? [b.source] : []);
    },
    async search() {
      this.busy = true; this.hits = []; this.notes = [];
      try {
        const r = await api("/api/v1/collection/books/search?" + qs(this.s), undefined, "GET");
        this.hits = r.hits; this.notes = r.notes || [];
      } catch (e) { this.notes = [e.message]; }
      this.busy = false; this.searched = true;
    },
    pick(h) {
      this.resetBook();
      this.fill(h);
      this.hits = [];
      this.mode = "form";
      toast("Übernommen – Lagerort wählen und speichern.");
    },
    fileChosen(role, ev) {
      const f = ev.target.files && ev.target.files[0];
      this.files[role] = !!f;
      if (f) this.preview[role] = URL.createObjectURL(f);
    },
    imgBroken(role) { if (this.urls[role] && !this.files[role]) { delete this.preview[role]; delete this.urls[role]; } },
    async save() {
      if (!String(this.d.title || "").trim() && !this.d.isbn13) { toast("Titel oder ISBN fehlt."); return; }
      this.saving = true;
      const fd = new FormData(this.$refs.form);
      if (this.id) fd.set("id", this.id);
      fd.set("signed", this.d.signed ? "1" : "0");
      fd.set("read", this.d.read ? "1" : "0");
      for (const role of ["front", "back"]) if (this.urls[role] && !this.files[role]) fd.set("cover_" + role + "_url", this.urls[role]);
      for (const [k, v] of [...fd.entries()]) if (v instanceof File && !v.name) fd.delete(k);
      if (!fd.get("isbn13")) fd.delete("isbn13");
      try {
        const r = await api("/api/v1/collection/books", fd);
        if (this.id) { lwGo(r.book.url); return; } // Bearbeiten: zurück zur Detailseite; Neu: auf der Seite bleiben (Serien-Scan)
        this.saved.unshift({ title: r.book.title, url: r.book.url, created: r.created, location: r.book.location });
        toast((r.created ? "📖 Gespeichert: " : "📖 Aktualisiert: ") + r.book.title);
        this.resetBook();
        this.mode = "scan";
        this.saving = false;
      } catch (e) { toast("Fehler: " + e.message, 5000); this.saving = false; }
    },
  };
}

// ------------------------------------------------------------------ Platten scannen (Serien-Scan, v0.34)
// Jeder Scan landet in einer Warteschlange und wird nacheinander abgearbeitet (MusicBrainz: 1 Anfrage/s), damit ein
// USB-Handscanner ohne Warten weiterscannen kann. Mehrere/keine Treffer bleiben als offene Karte stehen.
const RS_SETTINGS = "lw-record-scan";
const RS_DONE = "lw-record-scan-done";
function rsIsBarcode(code) { const d = String(code || "").replace(/[\s-]/g, ""); return /^\d+$/.test(d) && [8, 12, 13, 14].includes(d.length); }

function recordScan(cfg) {
  return {
    ...cameraMixin(),
    set: { location: "", medium: "", media_condition: "", sleeve_condition: "", auto_save: true },
    input: "", queue: [], working: false, pending: [], done: [], seq: 0,
    init() {
      try { Object.assign(this.set, JSON.parse(localStorage.getItem(RS_SETTINGS) || "{}")); } catch (_) { /* egal */ }
      if (cfg.location) this.set.location = cfg.location;
      try { this.done = JSON.parse(sessionStorage.getItem(RS_DONE) || "[]"); } catch (_) { this.done = []; }
      this.$watch("set", (v) => { try { localStorage.setItem(RS_SETTINGS, JSON.stringify(v)); } catch (_) { /* egal */ } });
      this.$watch("done", (v) => { try { sessionStorage.setItem(RS_DONE, JSON.stringify(v.slice(0, 300))); } catch (_) { /* egal */ } });
      this.focusScan();
    },
    focusScan() {
      this.$nextTick(() => {
        const a = document.activeElement;
        // Nicht wegspringen, während jemand in einem anderen Feld tippt (Lagerort, Kat.-Nr. …)
        if (a && a !== this.$refs.scan && a !== document.body && a.matches("input, select, textarea")) return;
        if (this.$refs.scan) this.$refs.scan.focus();
      });
    },
    handle(code) { this.enqueue({ code }); },  // Kamera
    submitInput() {
      const v = this.input.trim();
      this.input = "";
      if (v) this.enqueue({ code: v });
      if (this.$refs.scan) this.$refs.scan.focus();
    },
    label(body) {
      if (body.code && rsIsBarcode(body.code)) return "Barcode " + body.code;
      const parts = [body.catno || (body.code && !rsIsBarcode(body.code) ? body.code : ""), body.artist, body.album].filter(Boolean);
      return parts.length ? "Suche: " + parts.join(" · ") : "Scan";
    },
    enqueue(body) {
      this.queue.push(body);
      if (!this.working) this.work();
    },
    async work() {
      this.working = true;
      while (this.queue.length) {
        const body = this.queue.shift();
        await this.run(body);
      }
      this.working = false;
      this.focusScan();
    },
    async run(body) {
      const barcode = body.scanned || (rsIsBarcode(body.code) ? body.code.trim() : "");
      const base = { key: ++this.seq, label: this.label(body), barcode, notes: [] };
      try {
        const r = await api("/api/v1/collection/records/scan", { ...this.set, ...body, scanned: barcode });
        const notes = r.notes || [];
        if (r.status === "location") {
          this.set.location = r.location;
          toast("📍 Lagerort für die Serie: " + r.path);
        } else if (r.status === "saved") {
          this.done.unshift({ ...r.record, status: "neu" });
          beep();
          toast("💿 Gespeichert: " + [r.record.artist, r.record.album].filter(Boolean).join(" – "));
          notes.forEach((n) => toast(n, 4000));
        } else if (r.status === "duplicate") {
          this.done.unshift({ ...r.record, status: "dup" });
          toast("⚠ Schon in der Sammlung: " + [r.record.artist, r.record.album].filter(Boolean).join(" – "), 4000);
        } else if (r.status === "choose") {
          this.pending.unshift({ ...base, kind: "choose", hits: r.hits, notes });
        } else {
          this.pending.unshift({ ...base, kind: "none", notes, variants: (r.query && r.query.variants) || [], catno: "", artist: "", album: "" });
        }
      } catch (e) {
        this.pending.unshift({ ...base, kind: "error", message: "Fehler: " + e.message, body });
      }
      this.focusScan();
    },
    drop(p) { this.pending = this.pending.filter((x) => x !== p); this.focusScan(); },
    pick(p, h) {
      this.drop(p);
      this.enqueue({ mbid: h.musicbrainz_id || "", discogs_id: h.discogs_id || "", scanned: p.barcode });
      if (this.$refs.scan) this.$refs.scan.focus();
    },
    retry(p) {
      if (!String(p.catno || p.artist || p.album || "").trim()) { toast("Katalognummer oder Interpret/Album eingeben."); return; }
      this.drop(p);
      this.enqueue({ catno: p.catno, artist: p.artist, album: p.album, scanned: p.barcode });
      if (this.$refs.scan) this.$refs.scan.focus();
    },
    again(p) { this.drop(p); this.enqueue(p.body); },
    clearDone() { this.done = []; },
  };
}
