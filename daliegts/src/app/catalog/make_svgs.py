#!/usr/bin/env python3
"""Generates hand-drawn package illustrations for the Lagerwerk catalog."""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "catalog")
os.makedirs(OUT, exist_ok=True)

MET = 'fill="#c9ccd1" stroke="#80868e" stroke-width="2"'
GOLD = 'fill="#e2b84a" stroke="#9c7a22" stroke-width="2"'
BLK = 'fill="#2e2e33" stroke="#121214" stroke-width="2"'
LEAD = 'stroke="#9aa0a8" stroke-width="4" stroke-linecap="round"'
FILES = {}


def put(name, inner):
    FILES[name] = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 150">\n'
                   + inner.strip() + '\n</svg>\n')


def r(x, y, w, h, style, rx=0):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" {style}/>'


def legs(xs, y0, y1):
    return "".join(f'<line x1="{x}" y1="{y0}" x2="{x}" y2="{y1}" {LEAD}/>' for x in xs)


def axial_leads(x0=12, x1=188, y=75):
    return f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" {LEAD}/>'


def mark(x, y, t, size=9, col="#d8d8d8"):
    return (f'<text x="{x}" y="{y}" font-family="monospace" font-size="{size}" '
            f'fill="{col}" text-anchor="middle">{t}</text>')


# ---------------------------------------------------------------- SMD chips
def chip(name, body, stroke, text="", cap=MET):
    put(name, r(30, 50, 140, 50, f'fill="{body}" stroke="{stroke}" stroke-width="2"', 3)
        + r(30, 50, 28, 50, cap, 3) + r(142, 50, 28, 50, cap, 3)
        + (mark(100, 80, text, 12) if text else ""))


chip("smd-chip-resistor.svg", "#2b2b2e", "#111", "103")
chip("smd-chip-capacitor.svg", "#c9a57a", "#8c6d45")
chip("smd-chip-generic.svg", "#5b5e63", "#35373a")

# SOT-23
put("sot-23.svg", r(68, 44, 16, 22, MET) + r(116, 44, 16, 22, MET) + r(92, 84, 16, 22, MET)
    + r(55, 58, 90, 34, BLK, 3) + '<circle cx="66" cy="66" r="3" fill="#777"/>')
# SOT-89
put("sot-89.svg", r(78, 18, 44, 30, MET) + r(62, 95, 14, 30, MET) + r(93, 95, 14, 30, MET)
    + r(124, 95, 14, 30, MET) + r(52, 40, 96, 60, BLK, 3))
# SOT-223
put("sot-223.svg", r(65, 14, 70, 30, MET) + r(58, 100, 14, 32, MET) + r(93, 100, 14, 32, MET)
    + r(128, 100, 14, 32, MET) + r(45, 38, 110, 66, BLK, 3))
# SOD-123
put("sod-123.svg", r(34, 66, 30, 18, MET, 2) + r(136, 66, 30, 18, MET, 2)
    + r(55, 55, 90, 40, BLK, 5) + r(62, 55, 12, 40, 'fill="#b8bcc2"'))
# MiniMELF
put("minimelf.svg", r(50, 57, 100, 36, 'fill="#e7a07c" stroke="#a8603e" stroke-width="2"', 18)
    + r(40, 57, 22, 36, MET, 6) + r(138, 57, 22, 36, MET, 6)
    + r(70, 57, 10, 36, 'fill="#222"'))
# SMA / SMB / SMC
for nm, s in (("sma.svg", 0), ("smc.svg", 12)):
    put(nm, r(32 - s, 64 - s // 2, 30, 22 + s, MET, 2) + r(138 + s, 64 - s // 2, 30, 22 + s, MET, 2)
        + r(52 - s, 48 - s, 96 + 2 * s, 54 + 2 * s, BLK, 4)
        + r(60 - s, 48 - s, 12, 54 + 2 * s, 'fill="#b8bcc2"'))
put("bridge-smd.svg", "".join(r(x, y, 10, 16, MET) for x in (70, 120) for y in (34, 100))
    + r(55, 48, 90, 54, BLK, 3) + mark(100, 80, "~ + −", 11, "#bbb"))


# SO / TSSOP
def so(name, n, x0=40, x1=160, body_h=50, pin_w=8):
    pitch = (x1 - x0) / n
    pins = ""
    for i in range(n):
        x = x0 + pitch * i + (pitch - pin_w) / 2
        pins += r(round(x, 1), 30, pin_w, 20, MET) + r(round(x, 1), 100, pin_w, 20, MET)
    y = 75 - body_h / 2
    put(name, pins + r(x0 - 6, y, x1 - x0 + 12, body_h, BLK, 3)
        + f'<circle cx="{x0 + 4}" cy="{y + body_h - 9}" r="3.5" fill="#777"/>')


so("so-8.svg", 4)
so("so-14.svg", 7)
so("so-16.svg", 8, body_h=56)
so("tssop.svg", 10, 45, 155, 40, 5)
so("so-4.svg", 2, 70, 130)


def tqfp(name, n=8, pad_only=False):
    x0, s = 50, 100
    pitch = s / n
    out = ""
    for i in range(n):
        p = x0 + pitch * i + pitch * 0.3
        w = pitch * 0.4
        if pad_only:
            out += r(round(p, 1), 30, round(w, 1), 8, MET) + r(round(p, 1), 112, round(w, 1), 8, MET)
        else:
            out += r(round(p, 1), 8, round(w, 1), 20, MET) + r(round(p, 1), 122, round(w, 1), 20, MET)
    if not pad_only:
        for i in range(n):
            p = 25 + pitch * i + pitch * 0.3
            out += r(28, round(p, 1), 20, round(pitch * 0.4, 1), MET) + r(152, round(p, 1), 20, round(pitch * 0.4, 1), MET)
        body = r(50, 25, 100, 100, BLK, 3)
        put(name, out + body + '<circle cx="62" cy="37" r="4" fill="#777"/>')
    else:
        body = r(50, 25, 100, 100, BLK, 4)
        pads = out
        for i in range(n):
            p = 25 + pitch * i + pitch * 0.3
            pads += r(50, round(p, 1), 8, round(pitch * 0.4, 1), MET) + r(142, round(p, 1), 8, round(pitch * 0.4, 1), MET)
        put(name, body + pads.replace('y="30"', 'y="25"').replace('y="112"', 'y="117"')
            + '<circle cx="68" cy="43" r="4" fill="#777"/>')


tqfp("tqfp.svg")
tqfp("qfn.svg", 7, True)


def dip(name, n, socket=False):
    per = n // 2
    x0, x1 = 22, 178
    pitch = (x1 - x0) / per
    pw = min(8.0, pitch * 0.5)
    h = 60 if n > 4 else 50
    y = 75 - h / 2
    pins = ""
    for i in range(per):
        x = round(x0 + pitch * i + (pitch - pw) / 2, 1)
        pins += r(x, y - 16, pw, 18, MET) + r(x, y + h - 2, pw, 18, MET)
    if socket:
        body = (r(x0 - 6, y, x1 - x0 + 12, h, BLK, 3)
                + r(x0 + 6, y + 18, x1 - x0 - 12, h - 36, 'fill="#55575c"'))
        holes = "".join(f'<circle cx="{round(x0 + pitch * i + pitch / 2, 1)}" cy="{yy}" r="{round(min(3.5, pitch * 0.25), 1)}" fill="#c9ccd1"/>'
                        for i in range(per) for yy in (y + 9, y + h - 9))
        put(name, pins + body + holes)
    else:
        put(name, pins + r(x0 - 6, y, x1 - x0 + 12, h, BLK, 3)
            + f'<path d="M{x0 - 6} {75 - 8} a8 8 0 0 1 0 16" fill="#1a1a1c" stroke="#121214" stroke-width="2"/>'
            + f'<circle cx="{x0 + 8}" cy="{y + h - 10}" r="3.5" fill="#666"/>')


for n in (4, 6, 8, 14, 16, 18, 20, 24, 28, 40):
    dip(f"dip-{n}.svg", n)
dip("ic-socket.svg", 16, True)


# ---------------------------------------------------------------- THT transistors
put("to-92.svg", legs((84, 100, 116), 88, 142)
    + f'<path d="M70 90 V48 Q70 22 100 22 Q130 22 130 48 V90 Z" {BLK}/>'
    + mark(100, 62, "ABC", 10, "#bbb"))
put("to-18.svg", legs((86, 100, 114), 96, 144) + r(60, 88, 80, 10, MET, 3)
    + r(66, 30, 68, 62, MET, 8) + '<path d="M134 84 L146 84 L146 94 L134 94 Z" ' + MET + '/>'
    + '<line x1="70" y1="40" x2="130" y2="40" stroke="#a3a8ae" stroke-width="1.5"/>')
put("to-126.svg", legs((84, 100, 116), 95, 142) + r(68, 22, 64, 76, BLK, 3)
    + '<circle cx="100" cy="40" r="8" fill="#6b6e73" stroke="#121214" stroke-width="2"/>')
put("to-220.svg", legs((82, 100, 118), 105, 144)
    + r(62, 8, 76, 58, MET, 3)
    + '<circle cx="100" cy="28" r="9" fill="#80868e" stroke="#5c6168" stroke-width="2"/>'
    + r(62, 54, 76, 54, BLK, 2))
put("to-220-5.svg", legs((76, 88, 100, 112, 124), 105, 144)
    + r(62, 8, 76, 58, MET, 3)
    + '<circle cx="100" cy="28" r="9" fill="#80868e" stroke="#5c6168" stroke-width="2"/>'
    + r(62, 54, 76, 54, BLK, 2))
put("multiwatt.svg", "".join(f'<line x1="{x}" y1="104" x2="{x}" y2="{144 if i % 2 else 130}" {LEAD.replace("4", "3", 1)}/>'
                             for i, x in enumerate(range(44, 160, 8)))
    + r(38, 8, 124, 58, MET, 3)
    + '<circle cx="100" cy="28" r="9" fill="#80868e" stroke="#5c6168" stroke-width="2"/>'
    + r(38, 54, 124, 52, BLK, 2))
put("to-247.svg", legs((78, 100, 122), 110, 146) + r(52, 8, 96, 104, BLK, 4)
    + '<circle cx="100" cy="34" r="11" fill="#6b6e73" stroke="#121214" stroke-width="2"/>')
put("to-3.svg", '<path d="M20 75 Q20 50 50 45 L100 30 L150 45 Q180 50 180 75 Q180 100 150 105 L100 120 L50 105 Q20 100 20 75 Z" '
    + MET + '/>'
    + '<circle cx="38" cy="75" r="8" fill="#80868e" stroke="#5c6168" stroke-width="2"/>'
    + '<circle cx="162" cy="75" r="8" fill="#80868e" stroke="#5c6168" stroke-width="2"/>'
    + '<circle cx="100" cy="75" r="34" fill="#dfe2e6" stroke="#80868e" stroke-width="2"/>'
    + '<circle cx="100" cy="75" r="26" fill="#e9ebee" stroke="#a3a8ae" stroke-width="1.5"/>')

# ---------------------------------------------------------------- diodes axial
put("do-35.svg", axial_leads()
    + r(68, 60, 64, 30, 'fill="#f3b889" fill-opacity="0.85" stroke="#b9774a" stroke-width="2"', 12)
    + r(88, 68, 24, 14, 'fill="#7a4a2a"', 2) + r(74, 60, 9, 30, 'fill="#222"'))
put("do-41.svg", axial_leads() + r(66, 56, 68, 38, BLK, 7) + r(74, 56, 11, 38, 'fill="#c9ccd1"'))
put("do-201.svg", axial_leads() + r(56, 46, 88, 58, BLK, 9) + r(66, 46, 14, 58, 'fill="#c9ccd1"'))
put("reed.svg", axial_leads()
    + r(45, 60, 110, 30, 'fill="#dff0f7" fill-opacity="0.7" stroke="#8fb3c4" stroke-width="2"', 14)
    + '<line x1="45" y1="73" x2="104" y2="73" stroke="#7d828a" stroke-width="3"/>'
    + '<line x1="96" y1="77" x2="155" y2="77" stroke="#7d828a" stroke-width="3"/>')


# ---------------------------------------------------------------- resistors / passives THT
def band_body(name, body, stroke, bands, bone=True):
    if bone:
        shape = (f'<path d="M50 58 Q50 50 60 50 H72 Q76 56 84 56 H116 Q124 56 128 50 H140 Q150 50 150 58 '
                 f'V92 Q150 100 140 100 H128 Q124 94 116 94 H84 Q76 94 72 100 H60 Q50 100 50 92 Z" '
                 f'fill="{body}" stroke="{stroke}" stroke-width="2"/>')
    else:
        shape = r(56, 50, 88, 50, f'fill="{body}" stroke="{stroke}" stroke-width="2"', 16)
    out = axial_leads() + shape
    for x, c, w in bands:
        out += f'<rect x="{x}" y="{51 if bone and (x < 72 or x > 124) else 57}" width="{w}" height="{48 if bone and (x < 72 or x > 124) else 36}" fill="{c}"/>'
    put(name, out)


band_body("resistor-tht.svg", "#7fb2e3", "#4a7fb0",
          [(62, "#8b4513", 6), (86, "#111", 6), (100, "#111", 6), (114, "#c0392b", 6), (134, "#8b4513", 6)])
band_body("inductor-axial.svg", "#4f9d5d", "#2f6b3a",
          [(72, "#8b4513", 7), (88, "#111", 7), (104, "#8b4513", 7), (124, "#d4af37", 7)], bone=False)

put("elko.svg", legs((86, 114), 110, 146).replace('y2="146"', 'y2="146"', 1)
    + r(66, 14, 68, 98, 'fill="#1f3b73" stroke="#10224a" stroke-width="2"', 8)
    + r(114, 14, 14, 98, 'fill="#a9bde0"')
    + "".join(f'<rect x="117" y="{y}" width="8" height="2.5" fill="#1f3b73"/>' for y in (34, 58, 82))
    + '<line x1="68" y1="26" x2="132" y2="26" stroke="#10224a" stroke-width="1.5"/>')
put("tantal.svg", legs((88, 112), 100, 146)
    + '<path d="M100 18 C132 18 140 50 138 72 C136 96 122 106 100 106 C78 106 64 96 62 72 C60 50 68 18 100 18 Z" '
      'fill="#e8b923" stroke="#a37e10" stroke-width="2"/>'
    + '<path d="M80 50 h12 M86 44 v12" stroke="#6b4f00" stroke-width="3"/>')
put("tantal-smd.svg", r(30, 60, 28, 30, MET, 2) + r(142, 60, 28, 30, MET, 2)
    + r(46, 46, 108, 58, 'fill="#e2b021" stroke="#9a7710" stroke-width="2"', 3)
    + r(52, 46, 14, 58, 'fill="#7a5a10"'))
put("mlcc-tht.svg", legs((90, 110), 90, 146)
    + '<ellipse cx="100" cy="62" rx="34" ry="30" fill="#3c78c8" stroke="#224f8c" stroke-width="2"/>'
    + '<ellipse cx="90" cy="52" rx="10" ry="6" fill="#ffffff" fill-opacity="0.35"/>')
put("film-cap.svg", legs((82, 118), 104, 146)
    + r(58, 26, 84, 80, 'fill="#c0392b" stroke="#7e231a" stroke-width="2"', 6)
    + mark(100, 70, "MKS", 12, "#f5d0cb"))
put("ptc.svg", legs((90, 110), 96, 146)
    + '<circle cx="100" cy="64" r="34" fill="#e6c229" stroke="#9c820f" stroke-width="2"/>'
    + mark(100, 68, "PTC", 11, "#6b5a0a"))
put("ntc.svg", legs((94, 106), 80, 146)
    + '<ellipse cx="100" cy="60" rx="18" ry="22" fill="#2e2e33" stroke="#121214" stroke-width="2"/>')
put("ldr.svg", legs((88, 112), 96, 146)
    + '<circle cx="100" cy="62" r="36" fill="#d9d2c0" stroke="#8e8670" stroke-width="2"/>'
    + '<path d="M76 46 H124 V54 H76 V62 H124 V70 H76 V78 H124" fill="none" stroke="#c0462c" stroke-width="4"/>')
put("potentiometer.svg", legs((70, 100, 130), 108, 146)
    + '<circle cx="100" cy="72" r="46" fill="#b8bcc2" stroke="#7d838b" stroke-width="2"/>'
    + '<circle cx="100" cy="72" r="20" fill="#2e2e33" stroke="#121214" stroke-width="2"/>'
    + '<line x1="100" y1="56" x2="100" y2="72" stroke="#ddd" stroke-width="3"/>')
put("trimmer.svg", legs((78, 100, 122), 110, 146)
    + r(56, 28, 88, 84, 'fill="#2f6fd0" stroke="#1b4a91" stroke-width="2"', 6)
    + '<circle cx="100" cy="70" r="28" fill="#f2f2f2" stroke="#9aa0a8" stroke-width="2"/>'
    + '<path d="M86 70 H114 M100 56 V84" stroke="#7d838b" stroke-width="4"/>')


def smd_inductor(name):
    put(name, r(42, 20, 116, 110, BLK, 12)
        + '<circle cx="100" cy="75" r="42" fill="#45474d" stroke="#121214" stroke-width="2"/>'
        + '<circle cx="100" cy="75" r="16" fill="#2e2e33"/>' + mark(100, 79, "220", 10, "#bbb"))


smd_inductor("inductor-smd.svg")

# ---------------------------------------------------------------- LEDs
LEDC = {"rot": ("#e53935", "#9e1f1c"), "gruen": ("#43c05a", "#237a33"), "gelb": ("#f7d43a", "#a88b0e"),
        "blau": ("#3d7bf0", "#1d4aa3"), "weiss": ("#f4f6f8", "#9ea6ae"), "orange": ("#f68b2c", "#a9560f"),
        "ir": ("#8fa9cf", "#4c648a"), "rgb": ("url(#g)", "#8e959c")}
GRAD = ('<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#f06060"/><stop offset="0.5" stop-color="#70d070"/>'
        '<stop offset="1" stop-color="#6090f0"/></linearGradient></defs>')
for size, half in (("3mm", 16), ("5mm", 25)):
    for col, (fill, stroke) in LEDC.items():
        x0, x1 = 100 - half, 100 + half
        if col == "rgb":
            lg = legs((x0 + 8, 96, 104, x1 - 8), 108, 146) + legs((96,), 108, 148)
        else:
            lg = f'<line x1="{100 - 7}" y1="108" x2="{100 - 7}" y2="148" {LEAD}/><line x1="{100 + 7}" y1="108" x2="{100 + 7}" y2="136" {LEAD}/>'
        top = 108 - 2 * half - 34
        put(f"led-{size}-{col}.svg", (GRAD if col == "rgb" else "") + lg
            + f'<path d="M{x0} 104 V{top + half} A{half} {half} 0 0 1 {x1} {top + half} V104 Z" '
              f'fill="{fill}" fill-opacity="0.9" stroke="{stroke}" stroke-width="2"/>'
            + r(x0 - 5, 100, 2 * half + 10, 9, f'fill="{fill}" stroke="{stroke}" stroke-width="2"', 2)
            + f'<ellipse cx="{100 - half / 2.4:.1f}" cy="{top + half}" rx="{half / 5:.1f}" ry="{half / 2.2:.1f}" fill="#fff" fill-opacity="0.45"/>')

put("led-smd.svg", r(30, 50, 28, 50, MET, 3) + r(142, 50, 28, 50, MET, 3)
    + r(50, 46, 100, 58, 'fill="#f1ede2" stroke="#a8a293" stroke-width="2"', 3)
    + r(72, 56, 56, 38, 'fill="#f6d86b" fill-opacity="0.8" stroke="#c7a832" stroke-width="1.5"', 4)
    + r(52, 46, 8, 58, 'fill="#43a85a"'))
put("led-5050.svg", "".join(r(x, y, 16, 10, MET) for x in (36, 148) for y in (40, 70, 100))
    + r(50, 25, 100, 100, 'fill="#f4f4f1" stroke="#a9a99f" stroke-width="2"', 4)
    + '<circle cx="100" cy="75" r="38" fill="#f8f3dc" stroke="#c9c1a0" stroke-width="2"/>'
    + r(82, 62, 9, 9, 'fill="#e53935"') + r(96, 62, 9, 9, 'fill="#43c05a"') + r(110, 62, 9, 9, 'fill="#3d7bf0"')
    + r(90, 80, 20, 14, 'fill="#333"') + '<path d="M50 40 L65 25 H50 Z" fill="#a9a99f"/>')


def seg():
    s = 'fill="#e53935"'
    x, y, w, h = 76, 30, 48, 90
    return (r(x + 6, y, w - 12, 7, s, 3) + r(x + 6, y + h / 2 - 3.5, w - 12, 7, s, 3) + r(x + 6, y + h - 7, w - 12, 7, s, 3)
            + r(x, y + 6, 7, h / 2 - 12, s, 3) + r(x + w - 7, y + 6, 7, h / 2 - 12, s, 3)
            + r(x, y + h / 2 + 6, 7, h / 2 - 12, s, 3) + r(x + w - 7, y + h / 2 + 6, 7, h / 2 - 12, s, 3)
            + f'<circle cx="{x + w + 10}" cy="{y + h - 4}" r="4.5" {s}/>')


put("seven-segment.svg", r(56, 14, 88, 122, 'fill="#1f1f22" stroke="#0c0c0d" stroke-width="2"', 3) + seg())
put("photodiode.svg", legs((88, 112), 104, 146) + r(58, 30, 84, 76, BLK, 3)
    + r(72, 44, 56, 48, 'fill="#4a5d7a" stroke="#9fb3cf" stroke-width="2"', 2))
put("ir-receiver.svg", legs((84, 100, 116), 104, 146) + r(66, 26, 68, 80, BLK, 4)
    + '<circle cx="100" cy="58" r="18" fill="#3a3a40" stroke="#555" stroke-width="2"/>')
put("reflex-sensor.svg", legs((66, 82, 118, 134), 96, 146) + r(50, 36, 100, 62, BLK, 4)
    + '<circle cx="76" cy="66" r="16" fill="#5d8fd8" stroke="#2c5aa0" stroke-width="2"/>'
    + '<circle cx="124" cy="66" r="16" fill="#111" stroke="#555" stroke-width="2"/>')

# ---------------------------------------------------------------- crystals, fuses, relays
put("crystal-hc49.svg", legs((82, 118), 100, 146) + r(52, 36, 96, 66, MET, 20)
    + r(60, 44, 80, 50, 'fill="none" stroke="#a3a8ae" stroke-width="1.5"', 16) + mark(100, 74, "16.000", 11, "#555"))
put("crystal-watch.svg", '<path d="M150 70 Q170 70 188 60 M150 80 Q170 80 188 90" fill="none" ' + LEAD + '/>'
    + r(40, 58, 112, 34, MET, 17))
put("resonator.svg", legs((82, 100, 118), 94, 146)
    + '<path d="M58 60 Q58 30 100 30 Q142 30 142 60 V80 Q142 96 126 96 H74 Q58 96 58 80 Z" fill="#3b82c4" stroke="#1f5689" stroke-width="2"/>'
    + mark(100, 70, "16M", 11, "#e6eef7"))
put("fuse-5x20.svg", r(52, 55, 96, 40, 'fill="#dcecf5" fill-opacity="0.7" stroke="#8fb0c2" stroke-width="2"')
    + '<path d="M52 75 Q76 66 100 75 T148 75" fill="none" stroke="#6d737b" stroke-width="2"/>'
    + r(22, 52, 36, 46, MET, 4) + r(142, 52, 36, 46, MET, 4))
put("fuse-holder.svg", r(30, 40, 140, 70, BLK, 6) + r(46, 58, 108, 34, 'fill="#1a1a1c"', 4)
    + r(52, 62, 20, 26, MET) + r(128, 62, 20, 26, MET))
put("relay.svg", legs((58, 78, 122, 142), 112, 146)
    + r(38, 20, 124, 94, 'fill="#2a6fd1" stroke="#17468c" stroke-width="2"', 4)
    + r(56, 38, 88, 40, 'fill="#e9eef7" stroke="#b7c3d6" stroke-width="1"', 2)
    + '<path d="M64 50 H136 M64 58 H120 M64 66 H128" stroke="#8aa0c2" stroke-width="3"/>')
put("relay-sil.svg", legs((44, 80, 120, 156), 98, 140) + r(28, 46, 144, 54, BLK, 4)
    + '<circle cx="42" cy="60" r="4" fill="#777"/>')

# ---------------------------------------------------------------- connectors
put("pin-header.svg", "".join(r(26 + i * 19, 20, 6, 118, GOLD) for i in range(8))
    + r(16, 80, 168, 20, BLK, 1)
    + "".join(f'<line x1="{16 + i * 19 + 19}" y1="80" x2="{16 + i * 19 + 19}" y2="100" stroke="#121214" stroke-width="1.5"/>' for i in range(7)))
put("female-header.svg", "".join(r(26 + i * 19, 98, 6, 40, GOLD) for i in range(8))
    + r(16, 40, 168, 62, BLK, 2)
    + "".join(r(22 + i * 19, 50, 14, 14, 'fill="#0e0e10"', 1) for i in range(8)))
put("jst-xh.svg", "".join(r(44 + i * 26, 98, 6, 42, GOLD) for i in range(5))
    + r(26, 30, 148, 72, 'fill="#efe6cf" stroke="#a79d82" stroke-width="2"', 3)
    + r(34, 42, 132, 48, 'fill="#d9ceb1" stroke="#a79d82" stroke-width="1.5"', 2)
    + "".join(r(40 + i * 26, 60, 14, 22, 'fill="#8a8068"', 1) for i in range(5)))
put("screw-terminal.svg", legs((70, 130), 118, 146)
    + r(34, 24, 132, 96, 'fill="#2f7ed8" stroke="#1b4e8f" stroke-width="2"', 4)
    + "".join(f'<circle cx="{x}" cy="50" r="17" fill="#c9ccd1" stroke="#80868e" stroke-width="2"/>'
              f'<path d="M{x - 10} 50 H{x + 10}" stroke="#6d737b" stroke-width="3"/>'
              + r(x - 15, 80, 30, 26, 'fill="#1a3a66"', 2) for x in (70, 130)))
put("usb-c.svg", r(40, 50, 120, 50, MET, 25) + r(52, 60, 96, 30, 'fill="#1e1e20"', 15)
    + r(66, 70, 68, 10, 'fill="#3d3f44"', 3))
put("micro-usb.svg", '<path d="M44 52 H156 V84 L144 100 H56 L44 84 Z" ' + MET + '/>'
    + '<path d="M54 60 H146 V80 L138 91 H62 L54 80 Z" fill="#1e1e20"/>' + r(66, 66, 68, 8, 'fill="#3d3f44"', 2))
put("usb-a.svg", r(34, 38, 132, 74, MET, 4) + r(44, 48, 112, 54, 'fill="#1e1e20"', 2)
    + r(48, 54, 104, 18, 'fill="#f2f2f2"', 1) + "".join(r(58 + i * 24, 72, 12, 6, GOLD) for i in range(4)))
put("dc-jack.svg", r(44, 24, 112, 102, BLK, 5)
    + '<circle cx="100" cy="75" r="30" fill="#141416" stroke="#555" stroke-width="2"/>'
    + '<circle cx="100" cy="75" r="7" fill="#c9ccd1"/>')
put("audio-jack.svg", r(50, 30, 100, 90, BLK, 5)
    + '<circle cx="100" cy="75" r="20" fill="#44464b" stroke="#777" stroke-width="2"/>'
    + '<circle cx="100" cy="75" r="9" fill="#141416"/>')
put("rj45.svg", r(34, 22, 132, 110, MET, 4)
    + '<path d="M56 44 H144 V104 H118 V116 H82 V104 H56 Z" fill="#1e1e20"/>'
    + "".join(r(66 + i * 9, 48, 4, 14, GOLD) for i in range(8))
    + r(40, 26, 14, 10, 'fill="#43c05a"') + r(146, 26, 14, 10, 'fill="#f7d43a"'))
put("xt60.svg", legs((82, 118), 116, 146)
    + '<path d="M40 28 H150 L166 44 V106 L150 122 H40 Z" fill="#f3c623" stroke="#a78509" stroke-width="2"/>'
    + '<circle cx="82" cy="75" r="18" fill="#2b2b2e"/><circle cx="124" cy="75" r="18" fill="#2b2b2e"/>'
    + '<circle cx="82" cy="75" r="8" fill="#e2b84a"/><circle cx="124" cy="75" r="8" fill="#e2b84a"/>')
put("wago.svg", r(30, 44, 140, 76, 'fill="#d9dde2" fill-opacity="0.85" stroke="#8e959c" stroke-width="2"', 6)
    + "".join(r(40 + i * 44, 24, 32, 58, 'fill="#f07f1e" stroke="#a1520c" stroke-width="2"', 4)
              + f'<circle cx="{56 + i * 44}" cy="100" r="9" fill="#9aa1a8"/>' for i in range(3)))
put("dupont.svg", r(20, 64, 90, 22, MET, 3) + '<path d="M110 66 L130 70 V80 L110 84 Z" ' + MET + '/>'
    + '<path d="M130 72 H186 M130 78 H186" stroke="#c0392b" stroke-width="4"/>'
    + r(20, 28, 40, 30, BLK, 2) + r(32, 36, 16, 14, 'fill="#0e0e10"'))
put("idc.svg", "".join(r(34 + i * 28, 118, 6, 26, GOLD) for i in range(5))
    + r(18, 26, 164, 94, BLK, 3) + r(30, 40, 140, 66, 'fill="#1a1a1c"', 2)
    + r(86, 106, 28, 14, 'fill="#1a1a1c"')
    + "".join(r(42 + i * 28, y, 6, 6, GOLD) for i in range(5) for y in (54, 84)))


# ---------------------------------------------------------------- switches
def button(name, base, cap_r, cap_fill):
    s = base
    o = (200 - s) / 2
    put(name, r(o, 75 - s / 2, s, s, BLK, 3)
        + r(o + 6, 75 - s / 2 + 6, s - 12, s - 12, MET, 2)
        + f'<circle cx="100" cy="75" r="{cap_r}" fill="{cap_fill}" stroke="#121214" stroke-width="2"/>'
        + "".join(f'<circle cx="{cx}" cy="{cy}" r="3" fill="#80868e"/>' for cx in (o + 14, 200 - o - 14) for cy in (75 - s / 2 + 14, 75 + s / 2 - 14)))


button("button-6x6.svg", 90, 24, "#2e2e33")
button("button-12x12.svg", 116, 38, "#2e2e33")
put("button-smd.svg", r(36, 54, 16, 12, MET) + r(148, 54, 16, 12, MET) + r(36, 84, 16, 12, MET) + r(148, 84, 16, 12, MET)
    + r(50, 45, 100, 60, 'fill="#e9e9e9" stroke="#9a9a9a" stroke-width="2"', 4) + r(78, 58, 44, 34, BLK, 6))
put("toggle-switch.svg", legs((80, 100, 120), 118, 146) + r(62, 78, 76, 42, BLK, 3)
    + r(78, 56, 44, 24, MET, 2) + r(84, 46, 32, 12, MET, 2)
    + '<line x1="100" y1="50" x2="128" y2="10" stroke="#b5bac1" stroke-width="10" stroke-linecap="round"/>'
    + '<line x1="100" y1="50" x2="128" y2="10" stroke="#80868e" stroke-width="1" />')
put("slide-switch.svg", legs((76, 100, 124), 104, 146) + r(52, 66, 96, 40, MET, 2)
    + r(56, 70, 88, 32, BLK, 1) + r(88, 40, 22, 32, BLK, 2))
put("dip-switch.svg", "".join(r(34 + i * 18, y, 6, 12, MET) for i in range(8) for y in (26, 112))
    + r(22, 36, 156, 78, 'fill="#d23b2e" stroke="#8a2119" stroke-width="2"', 3)
    + "".join(r(31 + i * 18, 52, 12, 44, 'fill="#8a2119"', 1) + r(31 + i * 18, 52 + (0 if i % 3 else 22), 12, 22, 'fill="#f4f4f4"', 1)
              for i in range(8)))
put("rocker-switch.svg", r(46, 22, 108, 106, BLK, 5)
    + '<path d="M60 36 H140 V114 H60 Z" fill="#3a3a40" stroke="#121214" stroke-width="2"/>'
    + '<path d="M62 38 L138 50 V112 H62 Z" fill="#48484f"/>' + mark(100, 70, "I", 16, "#ddd") + mark(100, 104, "O", 14, "#ddd"))
put("encoder.svg", legs((64, 82, 100, 118, 136), 118, 146)
    + r(50, 64, 100, 56, MET, 3) + r(80, 40, 40, 26, MET, 2)
    + r(88, 6, 24, 36, 'fill="#d4d7db" stroke="#80868e" stroke-width="2"', 2)
    + '<line x1="100" y1="8" x2="100" y2="38" stroke="#80868e" stroke-width="2"/>')
put("microswitch.svg", legs((64, 100, 136), 108, 146) + r(40, 50, 120, 60, BLK, 4)
    + '<path d="M60 48 L150 24" stroke="#c9ccd1" stroke-width="6" stroke-linecap="round"/>'
    + '<circle cx="152" cy="24" r="9" fill="#e9e9e9" stroke="#80868e" stroke-width="2"/>'
    + r(90, 42, 12, 10, 'fill="#e03a2e"'))


# ---------------------------------------------------------------- modules
def pcb(name, col, stroke, extra, pins_top=True, pins_bottom=True, n=10):
    out = r(14, 28, 172, 94, f'fill="{col}" stroke="{stroke}" stroke-width="2"', 4)
    for i in range(n):
        x = 24 + i * (152 / (n - 1))
        if pins_top:
            out += f'<circle cx="{x:.1f}" cy="36" r="3.5" fill="#e2b84a" stroke="#9c7a22" stroke-width="1"/>'
        if pins_bottom:
            out += f'<circle cx="{x:.1f}" cy="114" r="3.5" fill="#e2b84a" stroke="#9c7a22" stroke-width="1"/>'
    for _cx, _cy in ((22, 60), (178, 60)):
        pass
    put(name, out + extra)


pcb("module-pcb.svg", "#1f5fae", "#123c70", r(70, 54, 60, 42, BLK, 2) + r(140, 60, 20, 12, 'fill="#c9a57a"', 1)
    + r(36, 60, 20, 12, 'fill="#2b2b2e"', 1))
pcb("module-arduino.svg", "#12848f", "#0a5359", r(14, 58, 26, 34, MET, 2) + r(62, 60, 96, 30, BLK, 2)
    + r(42, 88, 14, 12, 'fill="#e03a2e"', 1))
pcb("module-esp.svg", "#1b1b1d", "#000", r(30, 50, 98, 56, MET, 3) + mark(79, 82, "ESP", 12, "#6b6f75")
    + '<path d="M140 52 V100 H150 V58 H160 V100 H170 V58" fill="none" stroke="#c9a33a" stroke-width="3"/>', True, True, 8)
pcb("oled.svg", "#1f5fae", "#123c70", r(26, 48, 148, 64, 'fill="#101418" stroke="#000" stroke-width="2"', 2)
    + r(40, 58, 60, 8, 'fill="#5fc8ff"') + r(40, 72, 96, 6, 'fill="#5fc8ff"') + r(40, 84, 80, 6, 'fill="#5fc8ff"'),
    True, False, 4)
pcb("lcd1602.svg", "#2e8b3e", "#1a5a25", r(26, 46, 148, 62, 'fill="#1d2b17" stroke="#000" stroke-width="2"', 2)
    + r(32, 52, 136, 50, 'fill="#8fce4a"', 2)
    + "".join(r(36 + i * 8.2, y, 6, 9, 'fill="#6fae34"') for i in range(16) for y in (58, 82)), True, False, 16)
pcb("module-relay.svg", "#1f5fae", "#123c70", r(58, 44, 84, 60, 'fill="#2a6fd1" stroke="#17468c" stroke-width="2"', 3)
    + r(150, 50, 28, 50, 'fill="#2f7ed8" stroke="#1b4e8f" stroke-width="2"', 2)
    + r(24, 60, 22, 14, 'fill="#2b2b2e"'), False, False)
pcb("sensor.svg", "#1f5fae", "#123c70", r(78, 56, 44, 40, 'fill="#dfe3e8" stroke="#8e959c" stroke-width="2"', 3)
    + '<circle cx="100" cy="76" r="6" fill="#2e2e33"/>', True, False, 4)
put("dht.svg", legs((82, 94, 106, 118), 118, 146)
    + r(62, 14, 76, 106, 'fill="#e7eaee" stroke="#8e959c" stroke-width="2"', 4)
    + "".join(r(70 + c * 12, 24 + rr * 12, 8, 6, 'fill="#5b6068"', 1) for c in range(5) for rr in (0, 1, 2, 3, 4)))
pcb("ultrasonic.svg", "#1f5fae", "#123c70",
    "".join(f'<circle cx="{x}" cy="75" r="32" fill="#c9ccd1" stroke="#80868e" stroke-width="2"/>'
            f'<circle cx="{x}" cy="75" r="22" fill="#2e2e33"/>' for x in (56, 144)), False, True, 4)
pcb("pir.svg", "#2e8b3e", "#1a5a25",
    '<circle cx="100" cy="75" r="44" fill="#f4f4f1" stroke="#b9b9ae" stroke-width="2"/>'
    + '<path d="M60 75 H140 M100 35 V115 M72 47 L128 103 M128 47 L72 103" stroke="#dcdcd2" stroke-width="2"/>', False, False)

put("generic.svg", r(50, 30, 100, 90, 'fill="#c3c7cc" stroke="#7d838b" stroke-width="2"', 8)
    + mark(100, 90, "?", 44, "#7d838b"))
put("bridge-rectifier.svg", legs((64, 88, 112, 136), 104, 146) + r(44, 26, 112, 80, BLK, 4)
    + mark(64, 60, "+", 14, "#bbb") + mark(88, 60, "~", 14, "#bbb") + mark(112, 60, "~", 14, "#bbb") + mark(136, 60, "−", 14, "#bbb"))

for name, content in FILES.items():
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(content)
print(len(FILES), "SVGs written")
