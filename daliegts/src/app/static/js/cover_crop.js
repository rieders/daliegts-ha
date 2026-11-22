// DaLiegt’s – Cover mit der Handykamera (v0.34.4): Foto aufnehmen/wählen -> vier Eckpunkte ziehen -> der Server
// entzerrt (POST /api/v1/collection/cover/warp, speichert nichts) -> Vorschau -> „Speichern“ übernimmt das Cover ins
// Formular; hochgeladen (und ein vorhandenes Cover ersetzt) wird es erst mit dem Speichern des Formulars.
// <input type="file" capture="environment"> öffnet die Kamera-App – das geht auch ohne HTTPS und in der HA-App.
"use strict";

const CROP_MAX = 2500; // größere Handyfotos vor dem Hochladen verkleinern
const CROP_INSET = 0.08; // Startpunkte etwas vom Bildrand eingerückt
const CROP_GRAB = 56; // Fangradius um einen Eckpunkt (Bildschirmpixel)
const LOUPE = 120; // Lupe: Größe in Bildschirmpixeln …
const LOUPE_ZOOM = 3; // … und Vergrößerung

function cropLoadImg(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("Das Bild lässt sich nicht öffnen."));
    img.src = url;
  });
}

// Datei -> {blob, url, w, h}. Der Browser zeigt Fotos schon nach EXIF gedreht an; beim Verkleinern über das Canvas
// wird diese Drehung fest eingerechnet. Unverkleinerte Fotos dreht der Server (ImageOps.exif_transpose) genauso.
async function cropPrepare(file) {
  const url0 = URL.createObjectURL(file);
  const img = await cropLoadImg(url0);
  let w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) throw new Error("Das Bild ist leer.");
  if (Math.max(w, h) <= CROP_MAX && /^image\/(jpeg|png|webp)$/i.test(file.type || "")) return { blob: file, url: url0, w, h };
  const k = Math.min(1, CROP_MAX / Math.max(w, h));
  w = Math.round(w * k); h = Math.round(h * k);
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  c.getContext("2d").drawImage(img, 0, 0, w, h);
  const blob = await new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error("Verkleinern fehlgeschlagen."))), "image/jpeg", 0.9));
  URL.revokeObjectURL(url0);
  return { blob, url: URL.createObjectURL(blob), w, h };
}

function cropInset(w, h, f = CROP_INSET) {
  return [{ x: w * f, y: h * f }, { x: w * (1 - f), y: h * f }, { x: w * (1 - f), y: h * (1 - f) }, { x: w * f, y: h * (1 - f) }];
}

function cropFresh(square) {
  return { open: false, step: "crop", role: "", label: "", src: "", w: 0, h: 0, pts: [], square, busy: false, error: "",
           result: "", blob: null, upload: null, drag: -1, k: 1, input: null };
}

// Mixin für recordForm/bookForm: cropped[role] = entzerrtes Cover (Blob), wird in save() als cover_<role> mitgeschickt.
function coverCrop(squareDefault) {
  return {
    cropped: {},
    cr: cropFresh(squareDefault),
    cropSquareDefault: squareDefault,

    async cropStart(role, ev, label) {
      const input = ev.target;
      const f = input.files && input.files[0];
      if (!f) return;
      this.cropFreeUrls();
      this.cr = { ...cropFresh(this.cr.square), open: true, role, label: label || "Cover", busy: true, input };
      input.value = ""; // dieselbe Datei nochmal wählen können
      try {
        const p = await cropPrepare(f);
        Object.assign(this.cr, { upload: p.blob, src: p.url, w: p.w, h: p.h, pts: cropInset(p.w, p.h), busy: false });
        this.$nextTick(() => this.cropMeasure());
      } catch (e) { this.cr.busy = false; this.cr.error = e.message; }
    },
    cropFreeUrls() {
      if (this.cr.src) URL.revokeObjectURL(this.cr.src);
      if (this.cr.result && !Object.values(this.preview || {}).includes(this.cr.result)) URL.revokeObjectURL(this.cr.result);
    },
    cropClose() { this.cropFreeUrls(); this.cr = cropFresh(this.cr.square); },
    cropRetake() {
      const input = this.cr.input;
      this.cropClose();
      if (input) input.click();
    },
    cropMeasure() {
      const svg = this.$refs.cropSvg;
      if (!svg || !this.cr.w) return;
      svg.setAttribute("viewBox", "0 0 " + this.cr.w + " " + this.cr.h);
      const r = svg.getBoundingClientRect();
      if (r.width) this.cr.k = this.cr.w / r.width;
    },
    cropWhole() { this.cr.pts = cropInset(this.cr.w, this.cr.h, 0); },
    cropPolygon() { return this.cr.pts.map((p) => p.x.toFixed(1) + "," + p.y.toFixed(1)).join(" "); },
    cropPt(ev) {
      const r = this.$refs.cropSvg.getBoundingClientRect();
      const k = this.cr.w / r.width;
      this.cr.k = k;
      return { x: Math.min(Math.max((ev.clientX - r.left) * k, 0), this.cr.w), y: Math.min(Math.max((ev.clientY - r.top) * k, 0), this.cr.h),
               sx: ev.clientX - r.left, sy: ev.clientY - r.top };
    },
    cropDown(ev) {
      const p = this.cropPt(ev);
      let best = -1, dist = Infinity;
      this.cr.pts.forEach((q, i) => { const d = Math.hypot(q.x - p.x, q.y - p.y); if (d < dist) { dist = d; best = i; } });
      if (best < 0 || dist > CROP_GRAB * this.cr.k) return;
      ev.preventDefault();
      this.cr.drag = best;
      try { ev.currentTarget.setPointerCapture(ev.pointerId); } catch (_) { /* egal */ }
      this.cr.pts[best] = { x: p.x, y: p.y };
      this.cropLoupe(p);
    },
    cropMove(ev) {
      if (this.cr.drag < 0) return;
      ev.preventDefault();
      const p = this.cropPt(ev);
      this.cr.pts[this.cr.drag] = { x: p.x, y: p.y };
      this.cropLoupe(p);
    },
    cropUp() { this.cr.drag = -1; },
    // Lupe über dem Finger: vergrößerter Bildausschnitt um den Eckpunkt mit Fadenkreuz.
    cropLoupe(p) {
      const cv = this.$refs.cropLoupe, img = this.$refs.cropImg;
      if (!cv || !img) return;
      const stage = this.$refs.cropSvg.getBoundingClientRect();
      let left = p.sx - LOUPE / 2, top = p.sy - LOUPE - 40;
      if (top < 0) top = p.sy + 40; // am oberen Rand unter den Finger
      left = Math.min(Math.max(left, 0), Math.max(0, stage.width - LOUPE));
      cv.style.left = left + "px"; cv.style.top = top + "px";
      const ctx = cv.getContext("2d");
      const s = (LOUPE / LOUPE_ZOOM) * this.cr.k; // Ausschnitt in Bildpixeln
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, p.x - s / 2, p.y - s / 2, s, s, 0, 0, cv.width, cv.height);
      const m = cv.width / 2;
      ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(m, 0); ctx.lineTo(m, cv.height); ctx.moveTo(0, m); ctx.lineTo(cv.width, m); ctx.stroke();
      ctx.strokeStyle = "rgba(31,122,92,1)"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(m, 0); ctx.lineTo(m, cv.height); ctx.moveTo(0, m); ctx.lineTo(cv.width, m); ctx.stroke();
    },
    async cropApply() {
      this.cr.busy = true; this.cr.error = "";
      const fd = new FormData();
      fd.append("image", this.cr.upload, this.cr.upload.name || "foto.jpg");
      fd.append("points", JSON.stringify(this.cr.pts.map((p) => [Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10])));
      fd.append("mode", this.cr.square ? "square" : "free");
      fd.append("width", String(this.cr.w)); fd.append("height", String(this.cr.h));
      try {
        const r = await fetch("/api/v1/collection/cover/warp", { method: "POST", body: fd, credentials: "same-origin",
                                                               headers: { "X-Lagerwerk": "1" } });
        if (!r.ok) {
          let d = null;
          try { d = await r.json(); } catch (_) { /* leer */ }
          throw new Error((d && d.detail) || r.statusText || "Fehler " + r.status);
        }
        const blob = await r.blob();
        if (this.cr.result) URL.revokeObjectURL(this.cr.result);
        Object.assign(this.cr, { blob, result: URL.createObjectURL(blob), step: "preview" });
      } catch (e) { this.cr.error = "Entzerren fehlgeschlagen: " + e.message; }
      this.cr.busy = false;
    },
    // „Speichern“: Cover ins Formular übernehmen; der Server bekommt es mit dem Speichern des Formulars.
    cropSave() {
      const role = this.cr.role;
      this.cropped[role] = this.cr.blob;
      this.files[role] = true;
      this.preview[role] = this.cr.result;
      if (this.cr.src) URL.revokeObjectURL(this.cr.src);
      this.cr = cropFresh(this.cr.square);
      toast("Cover übernommen – wird mit „Speichern“ gespeichert.");
    },
    cropAttach(fd) {
      for (const [role, blob] of Object.entries(this.cropped)) {
        if (blob) fd.set("cover_" + role, new File([blob], "cover-" + role + ".webp", { type: "image/webp" }));
      }
    },
  };
}

