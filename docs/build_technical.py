"""Builds docs/technical.html (technical view). Stdlib only (+ optional Graphviz `dot`).
Run: python docs/build_technical.py"""
import html
import pathlib
import re
import shutil
import subprocess

OUT = pathlib.Path(__file__).with_name("technical.html")
e = html.escape
WARN = []

CSS = r"""
:root { --ground:#F6F6F3; --card:#FFFFFF; --line:#D9DBDF; --ink:#17191D; --muted:#5B6068; --soft:#EEEFEC;
        --teal:#E4F1EF; --teal-ink:#2F7F78; --amber:#FCEBC8; --amber-ink:#8A5A00; --green:#1E6B43; --blue:#1F5FA8;
        --lav:#E7E2F5; --lav-ink:#6B4FA8; --red:#A12D2D; --orange:#C2570C; }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--ground); color:var(--ink); font:15px/1.5 "IBM Plex Sans","Segoe UI",Arial,sans-serif; }
main { max-width:1000px; margin:0 auto; padding:32px 0 56px; }
section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:20px 16px; margin:18px 0; }
h1 { font-size:30px; font-weight:600; margin:4px 0 6px; } h2 { font-size:20px; font-weight:600; margin:0 0 8px; }
h3 { font-size:16px; margin:22px 0 4px; padding-top:10px; border-top:1px solid var(--line); } h4 { font-size:14px; margin:14px 0 2px; }
ul { margin:6px 0; padding-left:22px; } li { margin:3px 0; }
code, pre, .mono, .key, .cap, th, .eyebrow, .chip, .cite { font-family:"IBM Plex Mono",Consolas,monospace; }
code { font-size:12.5px; background:var(--soft); border:1px solid var(--line); border-radius:4px; padding:0 4px; }
.eyebrow { font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }
.deck { margin:0 0 10px; font-size:16px; }
.key, .cap { font-size:12px; color:var(--muted); margin:8px 0 0; } .cap { color:var(--ink); }
.cite { font-size:10.5px; color:var(--muted); margin-left:6px; overflow-wrap:anywhere; }
table { width:100%; border-collapse:collapse; margin:10px 0; font-size:13.5px; }
th, td { text-align:left; vertical-align:top; padding:6px 9px; border:1px solid var(--line); }
th { font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); background:var(--soft); }
td.m { font-family:"IBM Plex Mono",Consolas,monospace; font-size:12px; overflow-wrap:anywhere; }
.tblwrap { overflow-x:auto; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
.chip { font-size:12px; border:1px solid var(--line); border-radius:999px; padding:2px 10px; background:var(--card); color:var(--ink); text-decoration:none; }
a.chip:hover { border-color:var(--ink); } a.chip:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }
nav.toc { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin:18px 0; }
details { margin-top:10px; } summary { cursor:pointer; font-weight:600; }

.map-wrap { overflow-x:auto; margin:10px -16px 0; }
.map { position:relative; margin:0 auto; font-size:13px; line-height:1.3; }
.map svg { position:absolute; left:0; top:0; overflow:visible; }
.map .b { position:absolute; background:var(--card); border:1px solid var(--ink); border-radius:8px; padding:6px 8px; }
.map .b.routine { background:var(--teal); border-color:var(--teal-ink); border-radius:4px; }
.map .b.data { background:var(--soft); border-color:#B9BCC2; }
.map .b.shape { background:var(--amber); border-color:var(--amber-ink); }
.map .b.tbc { background:var(--soft); border:1px dashed #8A9099; color:var(--muted); }
.map .b.fail { border:1px dashed var(--red); color:var(--red); }
.map .b .t { display:block; font-weight:600; }
.map .b.routine .t, .map .b.mono .t { font-family:"IBM Plex Mono",Consolas,monospace; font-size:12px; }
.map .b .tag { font-family:"IBM Plex Mono",Consolas,monospace; font-size:10px; letter-spacing:.08em; color:var(--muted); display:block; }
.map .b.c { display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; }
.map .b.c .sub { font-size:11.5px; color:var(--muted); }
.map .b.actor { border-radius:999px; } .map .b.entry { border-width:2px; border-color:var(--ink); }
.map .b.decision { background:transparent; border:0; padding:0 8px; font-size:12px; }
.map .b.decision svg { position:absolute; left:0; top:0; width:100%; height:100%; }
.map .b.decision span { position:relative; z-index:1; }
.map .b.stepio { padding:5px 7px; }
.map .b.stepio ul { margin:3px 0 4px; padding-left:14px; font-size:11.5px; } .map .b.stepio li { margin:0; }
.io { display:grid; grid-template-columns:28px 1fr; gap:2px 5px; font-size:11px; border-top:1px dashed rgba(23,25,29,.25); padding-top:4px; }
.io .l { font-family:"IBM Plex Mono",Consolas,monospace; font-size:9.5px; font-weight:600; border-radius:3px; text-align:center; height:15px; line-height:15px; }
.io .l.in { background:var(--card); border:1px solid var(--blue); color:var(--blue); }
.io .l.out { background:var(--green); color:#fff; }
.io code { font-size:10.5px; padding:0 2px; background:rgba(255,255,255,.7); overflow-wrap:anywhere; }
.map .lbl { position:absolute; font-family:"IBM Plex Mono",Consolas,monospace; font-size:10.5px; color:var(--ink); white-space:nowrap; background:var(--card); padding:0 3px; height:17px; line-height:17px; display:flex; align-items:center; gap:4px; }
.map .lbl i { font-style:normal; width:17px; height:17px; border-radius:50%; background:var(--ink); color:#fff; font-size:10px; text-align:center; line-height:17px; flex:none; }
.map .lbl.grey { color:var(--muted); } .map .lbl.grey i { background:#8A9099; }
.map .card2 { position:absolute; font-family:"IBM Plex Mono",Consolas,monospace; font-size:10.5px; color:var(--ink); background:var(--card); padding:0 2px; line-height:13px; }
.map .lane { position:absolute; left:0; background:var(--soft); border-top:1px solid var(--card); }
.map .lane span { position:absolute; left:10px; top:5px; font-family:"IBM Plex Mono",Consolas,monospace; font-size:11px; letter-spacing:.08em; color:var(--muted); }
.map .grp { position:absolute; border:1px dashed #9AA0A8; border-radius:10px; }
.map .grp span { position:absolute; left:10px; top:5px; font-family:"IBM Plex Mono",Consolas,monospace; font-size:10.5px; letter-spacing:.08em; color:var(--muted); white-space:nowrap; }
.map .grp.dev { border-color:#B9BCC2; } .map .grp.lav { border-color:var(--lav-ink); } .map .grp.lav span { color:var(--lav-ink); }
.chipUA { display:inline-block; font:600 10px/14px "IBM Plex Mono",Consolas,monospace; padding:0 5px; border-radius:3px; color:#fff; margin-right:3px; }
.chipUA.u { background:var(--blue); } .chipUA.a { background:var(--orange); }
.map .b.ent { padding:0; overflow:hidden; background:var(--card); border:1px solid var(--ink); border-radius:6px; }
.map .ent .eh { padding:4px 8px; background:var(--soft); border-bottom:1px solid var(--ink); }
.map .ent .eh b { display:block; font-family:"IBM Plex Mono",Consolas,monospace; font-size:12px; line-height:15px; }
.map .ent .eh small { font-size:10.5px; color:var(--muted); display:block; line-height:13px; }
.map .ent .eh .tag { display:block; }
.map .ent table { margin:0; font-size:11.5px; } .map .ent td { border:0; border-bottom:1px solid var(--line); padding:0 8px; height:22px; font-family:"IBM Plex Mono",Consolas,monospace; white-space:nowrap; }
.map .ent td:last-child { text-align:right; color:var(--muted); font-size:10.5px; }
.map .ent span[class^="k"] { font-size:9px; font-weight:600; padding:0 4px; border-radius:3px; margin-left:4px; }
.map .ent .kpk { background:var(--ink); color:#fff; } .map .ent .kfk { border:1px solid var(--blue); color:var(--blue); }
.map .ent .kx { border:1px solid var(--line); color:var(--muted); }
.map polyline.flow { animation:flow 1.1s linear infinite; }
@keyframes flow { to { stroke-dashoffset:-18; } }
@media (prefers-reduced-motion: reduce) { .map polyline.flow { animation:none; } }

.steps td:first-child { width:34px; text-align:center; }
.n { display:inline-block; width:20px; height:20px; border-radius:50%; background:var(--ink); color:#fff; font:600 11px/20px "IBM Plex Mono",Consolas,monospace; text-align:center; }
.payloads { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; margin-top:10px; }
.payload { border:1px solid var(--line); border-radius:8px; overflow:hidden; min-width:0; margin-top:10px; }
.payloads .payload { margin-top:0; }
.payload .h { display:flex; justify-content:space-between; gap:8px; padding:6px 10px; background:var(--soft); border-bottom:1px solid var(--line); font-family:"IBM Plex Mono",Consolas,monospace; font-size:11.5px; }
.payload .h .dir { font-weight:600; } .payload .h .dir.in { color:var(--blue); } .payload .h .dir.out { color:var(--green); }
.payload pre { margin:0; padding:10px 12px; font-size:12px; line-height:1.5; overflow-x:auto; white-space:pre; }
.tk { color:#1C5C57; } .ts { color:#8A5A00; } .tn { color:#1F5FA8; } .tb { color:#6B4FA8; } .tc { color:var(--muted); font-style:italic; }
.journey { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin-top:10px; }
.journey .stage { border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--card); min-width:0; display:flex; flex-direction:column; }
.journey .stage.changed { border-color:var(--amber-ink); }
.journey .stage .h { padding:5px 8px; background:var(--soft); border-bottom:1px solid var(--line); font-size:12px; font-weight:600; }
.journey .stage.changed .h { background:var(--amber); }
.journey table { margin:0; font-size:11px; border:0; }
.journey td { border:0; border-bottom:1px solid var(--line); padding:3px 6px; font-family:"IBM Plex Mono",Consolas,monospace; overflow-wrap:anywhere; }
.journey td:first-child { color:var(--muted); width:46%; }
.journey .what { font-size:11.5px; color:var(--muted); padding:5px 8px; margin-top:auto; }
.calc { color:var(--lav-ink); } .chg { background:var(--amber); }
.exset { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; margin-top:10px; }
.ex { border:1px solid var(--line); border-radius:8px; overflow:hidden; display:flex; flex-direction:column; min-width:0; }
.ex .h { padding:6px 10px; background:var(--soft); border-bottom:1px solid var(--line); }
.ex .h b { display:block; font-size:13px; }
.verdict { font-family:"IBM Plex Mono",Consolas,monospace; font-size:10px; font-weight:600; letter-spacing:.06em; }
.ex.ok { border-top:3px solid var(--green); } .ex.ok .verdict { color:var(--green); }
.ex.edge { border-top:3px solid var(--amber-ink); } .ex.edge .verdict { color:var(--amber-ink); }
.ex.bad { border-top:3px solid var(--red); } .ex.bad .verdict { color:var(--red); }
.ex table { margin:0; font-size:12px; } .ex td { border:0; border-bottom:1px solid var(--line); padding:4px 10px; }
.ex td:first-child { width:34px; font-family:"IBM Plex Mono",Consolas,monospace; font-size:10px; font-weight:600; color:var(--muted); }
.ex td.m { font-size:11.5px; }
.ex .src { padding:4px 10px; margin-top:auto; font-family:"IBM Plex Mono",Consolas,monospace; font-size:10.5px; color:var(--muted); }
.rule { font-weight:600; margin:4px 0 6px; }
@media (max-width:820px) { .journey { grid-template-columns:1fr; } section { padding:16px; } }
.lin svg { width:100%; height:auto; }
.lin svg text { font-family:"IBM Plex Mono",Consolas,monospace; }
@media print {
  body { background:#fff; } main { padding:0; max-width:none; }
  section { border:0; padding:0; break-inside:auto; }
  .map polyline.flow { animation:none; } .map-wrap, .payload, .ex, .journey { break-inside:avoid; }
  nav.toc a { text-decoration:none; }
}
"""

# ------------------------------------------------------------------ text helpers
CITE_RE = re.compile(r"^(.*?)\s*\[([^\[\]]+)\]\s*$", re.S)


def txt(s):
    s = str(s)
    """Escape text; trailing [cite] becomes a small mono span."""
    m = CITE_RE.match(s)
    if m and ("/" in m.group(2) or ":" in m.group(2) or "." in m.group(2)):
        c = f'<span class="cite">[{e(m.group(2))}]</span>'
        return f"{e(m.group(1))}{c}" if m.group(1) else c
    return e(s)


def bullets(items):
    return "<ul>" + "".join(f"<li>{txt(i)}</li>" for i in items) + "</ul>"


def table(head, rows, cls="", mono=()):
    h = "".join(f"<th>{e(c)}</th>" for c in head)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            (f'<td class="m">{txt(c)}</td>' if i in mono else f"<td>{txt(c)}</td>") for i, c in enumerate(r)) + "</tr>"
    return f'<div class="tblwrap"><table class="{cls}"><tr>{h}</tr>{body}</table></div>'


def section(id_, title, body):
    return f'<section id="{id_}"><h2>{e(title)}</h2>{body}</section>'


def foot(key, cap):
    return f'<p class="key">Key: {e(key)}</p><p class="cap">{txt(cap)}</p>'


def rule(t):
    return f'<p class="rule">{e(t)}</p>'


TOK = re.compile(r'(?P<c>(?:--|#)[^\n]*)|(?P<k>"[^"\n]*"(?=\s*:))|(?P<s>"[^"\n]*"|\'[^\'\n]*\')|(?P<b>\b(?:true|false|null|CASE|WHEN|THEN|ELSE|END|AS|AND|OR|ON|GRANT|USE|SELECT|CATALOG|SCHEMA|TO)\b)|(?P<n>-?\b\d[\d,.]*\b)')


def color(src, lang):
    out, pos = [], 0
    for m in TOK.finditer(src):
        out.append(e(src[pos:m.start()]))
        kind = m.lastgroup
        if kind == "k" and lang != "json":
            kind = "s"
        if kind == "b" and lang == "json" and m.group() not in ("true", "false", "null"):
            out.append(e(m.group()))
        else:
            out.append(f'<span class="t{kind}">{e(m.group())}</span>')
        pos = m.end()
    out.append(e(src[pos:]))
    return "".join(out)


def payload(head, body, direction="", lang="json", cite=""):
    d = f'<span class="dir {direction.lower()}">{direction}</span>' if direction else ""
    c = f'<span class="cite">[{e(cite)}]</span>' if cite else ""
    return (f'<div class="payload"><div class="h"><span>{d} {e(head)}</span>{c}</div>'
            f'<pre>{color(body.strip(chr(10)), lang)}</pre></div>')


def payloads(*ps):
    return '<div class="payloads">' + "".join(ps) + "</div>"


def ex(kind, title, ins, outs, src):
    v = {"ok": "TYPICAL", "edge": "EDGE CASE", "bad": "REJECTED"}[kind]
    rows = "".join(f'<tr><td>IN</td><td class="m">{e(i)}</td></tr>' for i in (ins if isinstance(ins, list) else [ins]))
    rows += "".join(f'<tr><td>OUT</td><td class="m">{e(o)}</td></tr>' for o in (outs if isinstance(outs, list) else [outs]))
    return (f'<div class="ex {kind}"><div class="h"><span class="verdict">{v}</span><b>{e(title)}</b></div>'
            f'<table>{rows}</table><div class="src">{e(src)}</div></div>')


def exset(*cards):
    return '<div class="exset">' + "".join(cards) + "</div>"


def tcard(title, head, rows, src, kind="ok"):
    return (f'<div class="ex {kind}"><div class="h"><span class="verdict">{"TYPICAL" if kind == "ok" else "EDGE CASE"}</span><b>{e(title)}</b></div>'
            f'<div class="tblwrap">{table(head, rows, mono=range(len(head)))}</div><div class="src">{e(src)}</div></div>')


def details(summary, body):
    return f"<details open><summary>{e(summary)}</summary>{body}</details>"


def journey(stages):
    """stages: (title, [(field, value-html)], what, changed)"""
    s = ""
    for t, rows, what, ch in stages:
        r = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in rows)
        s += (f'<div class="stage{" changed" if ch else ""}"><div class="h">{e(t)}</div><table>{r}</table>'
              f'<div class="what">{e(what)}</div></div>')
    return f'<div class="journey">{s}</div>'


# ------------------------------------------------------------------ diagram engine
def tw(s, mono=False, size=12, bold=False):
    f = 0.62 if mono else (0.57 if bold else 0.52)
    return len(s) * f * size


def wrap(s, px, mono=False, size=12, bold=False):
    out = []
    per = max(4, int(px / ((0.62 if mono else (0.57 if bold else 0.52)) * size)))
    for para in s.split("\n"):
        cur = ""
        for w in para.split(" "):
            while len(w) > per:  # break long identifiers after / _ . or by length
                cut = max([w.rfind(c, 0, per) for c in "/_.-"] + [-1]) + 1
                if cut < per * 0.5:
                    cut = per
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(w[:cut])
                w = w[cut:]
            if cur and len(cur) + 1 + len(w) > per:
                out.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        out.append(cur)
    return out


class Node:
    pass


class Dia:
    def __init__(s, name, gx=52, gy=44, mx=18, my=18, maxw=996, lanes=None, lane_pad=(26, 14), maxbox=230):
        s.name, s.gx, s.gy, s.mx, s.my, s.maxw = name, gx, gy, mx, my, maxw
        s.nodes, s.order, s.edges, s.frames = {}, [], [], []
        s.lanes = lanes
        s.lane_pad = lane_pad
        s.maxbox = maxbox

    def add(s, id_, col, row, kind="step", title="", sub="", tag="", mono=False, w=None, h=None, rows=None, extra=None):
        n = Node()
        n.id, n.col, n.row, n.kind, n.title, n.sub, n.tag, n.mono = id_, col, row, kind, title, sub, tag, mono
        n.rows, n.extra = rows, extra
        s._size(n, w, h)
        s.nodes[id_] = n
        s.order.append(id_)
        return n

    def _size(s, n, w, h):
        k = n.kind
        if k == "ent":
            st = [r[2] for r in n.rows if r[1].startswith("PK") and len(r) > 2 and r[2] in ("tested", "inferred")]
            if st:
                n.tag = n.tag + " · key " + st[0]
            n.rows = [(r[0], r[1], "" if (len(r) > 2 and r[2] in ("tested", "inferred")) else (r[2] if len(r) > 2 else "")) for r in n.rows]
            names = [n.tag, n.title] + [r[0] + "  " + r[1] + "  " + r[2] for r in n.rows]
            n.w = w or int(min(max(tw(x, True, 11.5) for x in names) + 30, s.maxbox))
            n.sl = wrap(n.sub, n.w - 16, False, 10.5)
            n.eh = 28 + 13 * len(n.sl) + (12 if n.tag else 0)
            n.h = h or n.eh + 22 * len(n.rows) + 5
            return
        if k == "stepio":
            n.w = w or 170
            tl = wrap(n.title, n.w - 16, n.mono, 12, True)
            bl = wrap(n.sub, n.w - 30, False, 11.5)
            il = wrap(n.extra[0], n.w - 62, True, 10.5)
            ol = wrap(n.extra[1], n.w - 62, True, 10.5)
            n.lines = (tl, bl, il, ol)
            n.h = h or int(14 + 15 * len(tl) + 5 + 15 * len(bl) + 10 + 14 * len(il) + 14 * len(ol) + 12)
            return
        if k == "decision":
            lines = wrap(n.title, 105, False, 12)
            n.lines = lines
            lw = max(tw(x, False, 12) for x in lines)
            n.w = w or int(lw * 1.5 + 30)
            n.h = h or max(76, int(len(lines) * 15 * 1.7 + 26))
            return
        mono = n.mono or k in ("routine", "entry")
        tsz = 12 if mono else 13
        cand = [tw(n.title, mono, tsz, True)]
        subs = n.sub.split("\n") if n.sub else []
        cand += [tw(x, False, 11.5) for x in subs]
        if n.tag:
            cand.append(tw(n.tag, True, 10))
        bw = w or int(min(s.maxbox, max(max(cand) + 22, 96)))
        n.w = bw
        n.tl = wrap(n.title, bw - 18, mono, tsz, True)
        n.sl = wrap(n.sub, bw - 18, False, 11.5) if n.sub else []
        n.h = h or int(14 + 16 * len(n.tl) + 15 * len(n.sl) + (12 if (n.tag or k in ("data", "entry", "dataentry")) else 0) + (4 if n.sl else 0))
        n.h = max(n.h, 34)

    def edge(s, a, b, label="", dashed=False, num=None, color="#17191D", arrow=True, c1="", c2="", force=None):
        s.edges.append(dict(a=a, b=b, label=label, dashed=dashed, num=num, color=color, arrow=arrow, c1=c1, c2=c2, force=force))

    def frame(s, label, ids, cls="", pad=14, top=28):
        s.frames.append(dict(label=label, ids=ids, cls=cls, pad=pad, top=top))

    def layout(s):
        ns = list(s.nodes.values())
        ncol = max(n.col for n in ns) + 1
        nrow = max(n.row for n in ns) + 1
        cw = [max([n.w for n in ns if n.col == c] or [0]) for c in range(ncol)]
        rh = [max([n.h for n in ns if n.row == r] or [0]) for r in range(nrow)]
        s.cw, s.rh = cw, rh
        cx, x = [], s.mx
        for c in range(ncol):
            cx.append(x)
            x += cw[c] + s.gx
        s.W = x - s.gx + s.mx
        ry, y = [], (s.lane_pad[0] if s.lanes else s.my)
        step_extra = (s.lane_pad[0] + s.lane_pad[1]) if s.lanes else s.gy
        for r in range(nrow):
            ry.append(y)
            y += rh[r] + step_extra
        s.H = (y - step_extra + s.lane_pad[1]) if s.lanes else y - s.gy + s.my
        s.cx, s.ry = cx, ry
        for n in ns:
            n.x = cx[n.col] + (cw[n.col] - n.w) // 2
            n.y = ry[n.row] + (rh[n.row] - n.h) // 2
        if s.W > s.maxw:
            WARN.append(f"{s.name}: width {s.W} > {s.maxw}")
        s.gxs = [cx[0] - s.mx // 2] + [cx[c] + cw[c] + s.gx // 2 for c in range(ncol - 1)] + [cx[-1] + cw[-1] + s.mx // 2]
        if s.lanes:
            s.gys = [4] + [ry[r] + rh[r] + s.lane_pad[1] for r in range(nrow - 1)] + [s.H - 4]
        else:
            s.gys = [s.my // 2] + [ry[r] + rh[r] + s.gy // 2 for r in range(nrow - 1)] + [s.H - s.my // 2]

    def rect(s, n):
        return (n.x, n.y, n.x + n.w, n.y + n.h)

    @staticmethod
    def port(n, side, off=0):
        if side == "R":
            return (n.x + n.w, n.y + n.h / 2 + off)
        if side == "L":
            return (n.x, n.y + n.h / 2 + off)
        if side == "T":
            return (n.x + n.w / 2 + off, n.y)
        return (n.x + n.w / 2 + off, n.y + n.h)

    @staticmethod
    def seg_hits(p, q, r):
        x0, y0, x1, y1 = r[0] + 1, r[1] + 1, r[2] - 1, r[3] - 1
        if abs(p[0] - q[0]) < 0.01:
            x = p[0]
            lo, hi = sorted((p[1], q[1]))
            return x0 < x < x1 and lo < y1 and hi > y0
        y = p[1]
        lo, hi = sorted((p[0], q[0]))
        return y0 < y < y1 and lo < x1 and hi > x0

    def hits(s, pts):
        c = 0
        for i in range(len(pts) - 1):
            for n in s.nodes.values():
                if s.seg_hits(pts[i], pts[i + 1], s.rect(n)):
                    c += 1
        return c

    @staticmethod
    def clean(pts):
        out = [pts[0]]
        for p in pts[1:]:
            if abs(p[0] - out[-1][0]) < .01 and abs(p[1] - out[-1][1]) < .01:
                continue
            out.append(p)
        i = 1
        while i < len(out) - 1:
            a, b, c = out[i - 1], out[i], out[i + 1]
            if (abs(a[0] - b[0]) < .01 and abs(b[0] - c[0]) < .01) or (abs(a[1] - b[1]) < .01 and abs(b[1] - c[1]) < .01):
                del out[i]
            else:
                i += 1
        return out

    @staticmethod
    def dirok(pts, ea, eb):
        if len(pts) < 2:
            return False
        a0, a1, b0, b1 = pts[0], pts[1], pts[-2], pts[-1]
        dx, dy = a1[0] - a0[0], a1[1] - a0[1]
        ok = {"R": dx > 0.5, "L": dx < -0.5, "T": dy < -0.5, "B": dy > 0.5}[ea]
        dx, dy = b1[0] - b0[0], b1[1] - b0[1]
        ok2 = {"L": dx > 0.5, "R": dx < -0.5, "T": dy > 0.5, "B": dy < -0.5}[eb]
        return ok and ok2

    def build(s, spec, A, B, offa=0, offb=0, gl=0):
        ea, eb, kind, g = spec
        pa, pb = s.port(A, ea, offa), s.port(B, eb, offb)
        if kind == "S":
            if ea in "LR":
                return [(pa[0], g), (pb[0], g)]
            return [(g, pa[1]), (g, pb[1])]
        if kind == "Zh":
            return s.clean([pa, (g + gl, pa[1]), (g + gl, pb[1]), pb])
        if kind == "Zv":
            return s.clean([pa, (pa[0], g + gl), (pb[0], g + gl), pb])
        if kind == "Lh":
            return s.clean([pa, (pb[0], pa[1]), pb])
        return s.clean([pa, (pa[0], pb[1]), pb])

    def candidates(s, A, B):
        out = []
        ra, rb = s.rect(A), s.rect(B)
        lo, hi = max(ra[1], rb[1]) + 10, min(ra[3], rb[3]) - 10
        if lo <= hi:
            y = min(max((A.y + A.h / 2 + B.y + B.h / 2) / 2, lo), hi)
            if B.x > A.x + A.w:
                out.append(("R", "L", "S", y))
            elif A.x > B.x + B.w:
                out.append(("L", "R", "S", y))
        lo, hi = max(ra[0], rb[0]) + 10, min(ra[2], rb[2]) - 10
        if lo <= hi:
            x = min(max((A.x + A.w / 2 + B.x + B.w / 2) / 2, lo), hi)
            if B.y > A.y + A.h:
                out.append(("B", "T", "S", x))
            elif A.y > B.y + B.h:
                out.append(("T", "B", "S", x))
        for ea in "RLTB":
            for eb in "RLTB":
                if ea in "LR" and eb in "LR":
                    for g in s.gxs:
                        out.append((ea, eb, "Zh", g))
                elif ea in "TB" and eb in "TB":
                    for g in s.gys:
                        out.append((ea, eb, "Zv", g))
                elif ea in "LR":
                    out.append((ea, eb, "Lh", 0))
                else:
                    out.append((ea, eb, "Lv", 0))
        return out

    def route_all(s):
        specs = []
        for ed in s.edges:
            A, B = s.nodes[ed["a"]], s.nodes[ed["b"]]
            best = None
            cs = s.candidates(A, B)
            if ed["force"]:
                cs = [c for c in cs if c[0] + c[1] == ed["force"]] or cs
            for c in cs:
                pts = s.build(c, A, B)
                if not s.dirok(pts, c[0], c[1]) or s.hits(pts):
                    continue
                L = sum(abs(pts[i][0] - pts[i + 1][0]) + abs(pts[i][1] - pts[i + 1][1]) for i in range(len(pts) - 1))
                cost = L + 40 * (len(pts) - 2) + (-15 if c[2] == "S" else 0)
                if best is None or cost < best[0]:
                    best = (cost, c)
            if best is None:
                WARN.append(f"{s.name}: no clean route {ed['a']}->{ed['b']}")
                best = (0, cs[0])
            specs.append(best[1])
        use = {}
        for i, (ed, c) in enumerate(zip(s.edges, specs)):
            if c[2] == "S":
                continue
            use.setdefault((ed["a"], c[0]), []).append((i, "a"))
            use.setdefault((ed["b"], c[1]), []).append((i, "b"))
        offs = {}
        for key, lst in use.items():
            n = len(lst)
            if n > 1:
                node = s.nodes[key[0]]
                span = node.h if key[1] in "LR" else node.w
                step = min(14, (span - 20) / max(n - 1, 1))
                for j, (i, which) in enumerate(lst):
                    offs[(i, which)] = (j - (n - 1) / 2) * step
        glane, gl = {}, {}
        for i, c in enumerate(specs):
            if c[2] in ("Zh", "Zv"):
                glane.setdefault((c[2], c[3]), []).append(i)
        for lst in glane.values():
            n = len(lst)
            for j, i in enumerate(lst):
                gl[i] = (j - (n - 1) / 2) * 11
        s.routes = []
        for i, (ed, c) in enumerate(zip(s.edges, specs)):
            A, B = s.nodes[ed["a"]], s.nodes[ed["b"]]
            pts = s.build(c, A, B, offs.get((i, "a"), 0), offs.get((i, "b"), 0), gl.get(i, 0))
            if s.hits(pts):
                WARN.append(f"{s.name}: edge {ed['a']}->{ed['b']} crosses a box after offsets")
            s.routes.append(pts)

    def render(s, key="", cap=""):
        s.layout()
        s.route_all()
        H, W = int(s.H), int(s.W)
        back = ""
        if s.lanes:
            for r, name in enumerate(s.lanes):
                top = s.ry[r] - s.lane_pad[0]
                bot = s.ry[r] + s.rh[r] + s.lane_pad[1]
                back += f'<div class="lane" style="top:{top}px;height:{bot - top}px;width:{W}px"><span>{e(name)}</span></div>'
        for f in s.frames:
            ns = [s.nodes[i] for i in f["ids"]]
            x0 = min(n.x for n in ns) - f["pad"]
            y0 = min(n.y for n in ns) - f["top"]
            x1 = max(n.x + n.w for n in ns) + f["pad"]
            y1 = max(n.y + n.h for n in ns) + f["pad"]
            f["r"] = (x0, y0, x1, y1)
            if y0 < 0 or x0 < 0 or x1 > W:
                WARN.append(f"{s.name}: frame {f['label']} outside canvas ({x0},{y0},{x1},{y1}) W={W}")
            for n in s.nodes.values():
                if n.id not in f["ids"] and not (n.x + n.w < x0 or n.x > x1 or n.y + n.h < y0 or n.y > y1):
                    WARN.append(f"{s.name}: frame {f['label']} overlaps node {n.id}")
            back += (f'<div class="grp {f["cls"]}" style="left:{x0}px;top:{y0}px;width:{x1 - x0}px;height:{y1 - y0}px">'
                     f'<span>{e(f["label"])}</span></div>')
        for i, f in enumerate(s.frames):
            for g in s.frames[i + 1:]:
                a, b = f["r"], g["r"]
                if not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]):
                    if not (set(f["ids"]) <= set(g["ids"]) or set(g["ids"]) <= set(f["ids"])):
                        WARN.append(f"{s.name}: frames overlap {f['label']} / {g['label']}")
        mid = "m" + re.sub(r"\W", "", s.name)
        cols = sorted({ed["color"] for ed in s.edges})
        cidx = {c: k for k, c in enumerate(cols)}
        defs = "".join(f'<marker id="{mid}{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for c, k in cidx.items())
        lines, labels, placed, dropped = "", "", [], []
        s.auto = 0
        rects = [s.rect(n) for n in s.nodes.values()]

        def free(r):
            for q in rects + placed:
                if not (r[2] < q[0] - 1 or q[2] < r[0] - 1 or r[3] < q[1] - 1 or q[3] < r[1] - 1):
                    return False
            return r[0] >= -2 and r[2] <= W + 2 and r[1] >= -2 and r[3] <= H + 2
        for ed, pts in zip(s.edges, s.routes):
            p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            dash = ' stroke-dasharray="5 4"' if ed["dashed"] else ""
            cl = "flow" if ed["dashed"] and ed["arrow"] else ""
            mk = f' marker-end="url(#{mid}{cidx[ed["color"]]})"' if ed["arrow"] else ""
            lines += (f'<polyline class="{cl}" points="{p}" fill="none" stroke="{ed["color"]}" stroke-width="1.4" '
                      f'stroke-linejoin="round"{dash}{mk}/>')
            text = ed["label"]
            if text or ed["num"] is not None:
                lw = (tw(text, True, 10.5) + 8 if text else 0) + (21 if ed["num"] is not None else 0) + (4 if text and ed["num"] is not None else 0)
                segs = sorted(range(len(pts) - 1), key=lambda i: -(abs(pts[i][0] - pts[i + 1][0]) + abs(pts[i][1] - pts[i + 1][1])))
                r = None
                for tries in range(2):
                    for i in segs:
                        a, b = pts[i], pts[i + 1]
                        hor = abs(a[1] - b[1]) < .01
                        for t in (0.5, 0.3, 0.7, 0.15, 0.85):
                            mx_, my_ = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                            if hor:
                                c = (mx_ - lw / 2, my_ - 9, mx_ + lw / 2, my_ + 9)
                            elif tries == 0:
                                c = (mx_ + 5, my_ - 9, mx_ + 5 + lw, my_ + 9)
                            else:
                                c = (mx_ - 5 - lw, my_ - 9, mx_ - 5, my_ + 9)
                            if free(c):
                                r = c
                                break
                        if r:
                            break
                    if r:
                        break
                if not r and text:
                    # label does not fit: keep a number on the line, list the label in the arrow table
                    if ed["num"] is None:
                        s.auto += 1
                        ed["num"] = f"{s.auto}"
                    dropped.append((ed["num"], ed["a"], ed["b"], text))
                    text = ""
                    lw = 21
                    for i in segs:
                        a, b = pts[i], pts[i + 1]
                        for t in (0.5, 0.3, 0.7, 0.15, 0.85):
                            mx_, my_ = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                            c = (mx_ - lw / 2, my_ - 9, mx_ + lw / 2, my_ + 9)
                            if free(c):
                                r = c
                                break
                        if r:
                            break
                if not r:
                    i = segs[0]
                    a, b = pts[i], pts[i + 1]
                    mx_, my_ = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                    r = (mx_ - lw / 2, my_ - 9, mx_ + lw / 2, my_ + 9)
                    WARN.append(f"{s.name}: label overlap '{text}' {ed['a']}->{ed['b']}")
                placed.append(r)
                num = f'<i>{ed["num"]}</i>' if ed["num"] is not None else ""
                g = " grey" if ed["color"] == "#8A9099" else ""
                labels += f'<span class="lbl{g}" style="left:{r[0]:.0f}px;top:{r[1]:.0f}px">{num}{e(text)}</span>'
            for txt_, idx in ((ed["c1"], 0), (ed["c2"], -1)):
                if txt_:
                    a, b = (pts[0], pts[1]) if idx == 0 else (pts[-1], pts[-2])
                    dx = 0 if abs(b[0] - a[0]) < .01 else (1 if b[0] > a[0] else -1)
                    dy = 0 if abs(b[1] - a[1]) < .01 else (1 if b[1] > a[1] else -1)
                    if dx:
                        px, py = a[0] + (4 if dx > 0 else -4 - 7 * len(txt_)), a[1] - 15
                    else:
                        px, py = a[0] + 4, a[1] + (3 if dy > 0 else -16)
                    labels += f'<span class="card2" style="left:{px:.0f}px;top:{py:.0f}px">{e(txt_)}</span>'
        boxes = "".join(s.box(s.nodes[i]) for i in s.order)
        svg = f'<svg width="{W}" height="{H}" aria-hidden="true"><defs>{defs}</defs>{lines}</svg>'
        out = f'<div class="map-wrap"><div class="map" style="width:{W}px;height:{H}px">{back}{svg}{boxes}{labels}</div></div>'
        tbl = ""
        if dropped:
            nm = lambda i: s.nodes[i].title
            tbl = table(["#", "From", "To", "What moves"], [(n, nm(a), nm(b), t) for n, a, b, t in dropped], mono=(1, 2))
        return f"<!--map:{s.name}-->" + out + f"<!--/map:{s.name}-->" + (foot(key, cap) if key else "") + tbl

    def box(s, n):
        pos = f"left:{n.x}px;top:{n.y}px;width:{n.w}px;height:{n.h}px"
        k = n.kind
        if k == "ent":
            def badge(b):
                if not b:
                    return ""
                c = "kpk" if b.startswith("PK") else "kfk" if b.startswith("FK") else "kx"
                return f'<span class="{c}">{e(b)}</span>'
            rows = "".join(f'<tr><td>{e(r[0])}{badge(r[1])}</td><td>{e(r[2]) if len(r) > 2 else ""}</td></tr>' for r in n.rows)
            tag = f'<span class="tag">{e(n.tag)}</span>' if n.tag else ""
            return (f'<div class="b ent" style="{pos}"><div class="eh" style="height:{n.eh}px">{tag}<b>{e(n.title)}</b>'
                    f'<small>{e(n.sub)}</small></div><table>{rows}</table></div>')
        if k == "stepio":
            tl = n.lines[0]
            cls = "stepio" + (" shape" if n.tag == "shape" else "") + (" mono" if n.mono else "")
            chips = n.__dict__.get("chips", "")
            return (f'<div class="b {cls}" style="{pos}"><span class="tag">STEP {n.id}</span><span class="t">{chips}{e(" ".join(tl))}</span>'
                    f'<ul><li>{e(n.sub)}</li></ul><div class="io"><span class="l in">IN</span><span><code>{e(n.extra[0])}</code></span>'
                    f'<span class="l out">OUT</span><span><code>{e(n.extra[1])}</code></span></div></div>')
        if k == "decision":
            t = "<br>".join(e(x) for x in n.lines)
            return (f'<div class="b c decision" style="{pos}"><svg viewBox="0 0 100 100" preserveAspectRatio="none">'
                    f'<polygon points="50,1 99,50 50,99 1,50" fill="#E7E2F5" stroke="#6B4FA8" stroke-width="1.2" vector-effect="non-scaling-stroke"/></svg>'
                    f'<span>{t}</span></div>')
        cls = {"step": "", "actor": "actor", "routine": "routine", "data": "data", "entry": "routine entry", "shape": "shape",
               "tbc": "tbc", "fail": "fail", "dataentry": "data entry"}[k]
        tag = n.tag or ("DATA" if k in ("data", "dataentry") else "ENTRY" if k == "entry" else "")
        t = "<br>".join(e(x) for x in n.tl)
        sub = "".join(f'<span class="sub">{e(x)}</span>' for x in n.sl)
        mono = " mono" if n.mono else ""
        return (f'<div class="b c {cls}{mono}" style="{pos}">' + (f'<span class="tag">{e(tag)}</span>' if tag else "") +
                f'<span class="t">{t}</span>{sub}</div>')


RED, GREY, BLUE, ORANGE = "#A12D2D", "#8A9099", "#1F5FA8", "#C2570C"


# ================================================================== DIAGRAMS: architecture
def d_l0():
    d = Dia("L0", gx=64, gy=50, my=34, mx=16, maxbox=200)
    d.add("B1", 0, 0, "actor", "Engineer", "runs export, deploys")
    d.add("B2", 1, 0, "entry", "Export + load", "tools/first_run.py")
    d.add("B3", 2, 0, "step", "SQL warehouse", "4 statements at a time")
    d.add("B4", 3, 0, "data", "system.* tables", "billing, compute, lakeflow, query, access")
    d.add("B5", 1, 1, "data", "Local DuckDB file", "audit.duckdb")
    d.add("B6", 2, 1, "routine", "Crosshire app", "python -m app.api, :8000")
    d.add("B7", 3, 1, "actor", "Viewer", "browser tabs")
    d.edge("B1", "B2", "runs", num=1)
    d.edge("B2", "B3", "160 checks", num=2)
    d.edge("B4", "B3", "rows", True, num=3)
    d.edge("B3", "B2", "Arrow rows to parquet", True, num=4, force="TT")
    d.edge("B2", "B5", "load", True, num=5)
    d.edge("B5", "B6", "read-only", True, num=6)
    d.edge("B6", "B7", "JSON + UI", True, num=7)
    return d.render("pill = person · teal = code · grey = data · solid = calls · dashed = data moves · numbers = order",
                    "Only the export touches Databricks; browsing reads the local file only. [README.md:21-22]")


def d_l2a():
    d = Dia("L2A", gx=36, gy=66, my=30, mx=14, maxbox=158)
    d.add("A3", 0, 0, "entry", "tools/first_run.py", "export then load")
    d.add("A4", 1, 0, "routine", "export_direct_results.py", "app/direct_sql/*.sql, threads 4")
    d.add("A5", 2, 0, "routine", "snapshot.connect()", "token or OAuth M2M")
    d.add("A2", 4, 1, "step", "SQL warehouse", "DATABRICKS_HTTP_PATH")
    d.add("A1", 4, 0, "data", "system.* tables", "49 sources in sources.yml")
    d.add("A6", 3, 1, "data", "results/<stamp>/", "findings/ dims/ tags/ manifest.json")
    d.add("A7", 0, 1, "routine", "load_direct_results.py", "_load_root, then .tmp")
    d.add("A8", 3, 2, "tbc", "tools/snapshot.py", "system.* to parquet")
    d.add("A9", 2, 2, "tbc", "snapshot/<schema>__<table>/", "*.parquet")
    d.add("A10", 1, 2, "tbc", "dbt_run.py build --target dev", "dbt-duckdb")
    d.add("A11", 0, 2, "data", "<data dir>/audit.duckdb", "findings.f_* tags.* dims.*")
    d.add("A12", 0, 3, "tbc", "data/db_audit.duckdb", "dbt dev build")
    d.add("A14", 1, 3, "routine", "app/core", "data, rollup, tags, rls, materiality")
    d.add("A13", 2, 3, "routine", "app/api", "FastAPI + uvicorn")
    d.add("A15", 3, 3, "routine", "web UI", "React/Vite, app/web/dist")
    d.add("A16", 4, 3, "actor", "Browser")
    d.frame("DATABRICKS ACCOUNT", ["A1", "A2"])
    d.frame("BUILD (laptop or server)", ["A3", "A4", "A5", "A6", "A7"])
    d.frame("BUILD · dbt dev road", ["A8", "A9", "A10"], "dev")
    d.frame("STORE", ["A11", "A12"])
    d.frame("SERVE", ["A14", "A13"])
    d.frame("CLIENT", ["A15", "A16"])
    d.edge("A3", "A4", "subprocess --out", num=1)
    d.edge("A4", "A5", "connect()", num=2)
    d.edge("A5", "A2", "dbsql.connect", num=3)
    d.edge("A1", "A2", "SELECT", True, num=4)
    d.edge("A2", "A6", "Arrow rows", True, num=5)
    d.edge("A3", "A7", "subprocess <dir>", num=6)
    d.edge("A6", "A7", "parquet", True, num=7)
    d.edge("A7", "A11", "os.replace", True, num=8)
    d.edge("A11", "A14", "read-only", True, num=9)
    d.edge("A13", "A14", "calls", num=10)
    d.edge("A13", "A15", "dist + /api JSON", True, num=11)
    d.edge("A16", "A15", "opens :8000", num=12)
    d.edge("A1", "A8", "copy", True, color=GREY, num="D1")
    d.edge("A9", "A10", "read_parquet", True, color=GREY, num="D2")
    d.edge("A10", "A12", "build", True, color=GREY, num="D3")
    d.edge("A8", "A9", "", True, color=GREY)
    return d.render("dashed frame = platform · teal = code · grey = data · solid = calls · dashed = data · D# and grey = dev road",
                    "Both roads write the same findings/tags/dims schemas; the app opens one file. [tools/first_run.py:162-194; tools/export_direct_results.py:1179-1215; tools/load_direct_results.py:471,516-547,853; app/core/data.py:304-329]")




def d_l2b():
    d = Dia("L2B", gx=40, gy=60, my=30, mx=18, maxbox=160)
    d.add("B1", 0, 1, "entry", "tools/deploy_app.py", "stage, sync, deploy")
    d.add("B2", 0, 2, "entry", "build_databricks_dashboards.py", "14 dashboards + notebook")
    d.add("B3", 1, 0, "step", "Apps proxy", "sets X-Forwarded-Email")
    d.add("B4", 1, 1, "routine", "tools/app_start.py", "unpack DuckDB wheel")
    d.add("B5", 2, 1, "routine", "app.api", "0.0.0.0:DATABRICKS_APP_PORT")
    d.add("B6", 2, 0, "routine", "app/api/refresh.py", "export + load thread")
    d.add("B7", 3, 0, "data", "app disk", "audit-<stamp>.duckdb, wiped on restart")
    d.add("B8", 1, 2, "routine", "00 - Snapshot notebook", "159 checks, 4 at a time")
    d.add("B9", 2, 2, "data", "<catalog>.databricks_audit_dashboard", "one table per check")
    d.add("B10", 3, 2, "step", "14 Lakehouse Health dashboards", "read snapshot schema only")
    d.add("B11", 3, 3, "step", "7 system_tables_direct dashboards", "read system.* live")
    d.add("B12", 4, 1, "step", "SQL warehouse")
    d.add("B13", 4, 2, "data", "system.* tables")
    d.add("B14", 0, 0, "actor", "Viewer")
    d.edge("B1", "B4", "app.yaml command", num="A1")
    d.edge("B4", "B5", "runpy app.api", num="A2")
    d.edge("B14", "B3", "GET /, /api/*", num="A3")
    d.edge("B3", "B5", "proxy", num="A3")
    d.edge("B5", "B6", "POST /api/refresh", num="A4")
    d.edge("B6", "B12", "export subprocess", num="A5")
    d.edge("B13", "B12", "rows", True, num="A6")
    d.edge("B12", "B7", "rows to db", True, num="A6")
    d.edge("B7", "B5", "read-only", True, num="A7")
    d.edge("B2", "B8", "import file", num="L1")
    d.edge("B8", "B12", "CREATE OR REPLACE TABLE", num="L2")
    d.edge("B12", "B9", "snapshot tables", True, num="L3")
    d.edge("B9", "B10", "SELECT", True, num="L4")
    d.edge("B13", "B11", "SELECT live", True, num="L5")
    d.frame("DEVELOPER MACHINE", ["B1", "B2"])
    d.frame("DATABRICKS APPS", ["B3", "B4", "B5", "B6", "B7"])
    d.frame("WORKSPACE / LAKEVIEW", ["B8", "B9", "B10", "B11"])
    d.frame("DATABRICKS ACCOUNT", ["B12", "B13"])
    return d.render("A# = Databricks App path · L# = dashboards path · solid = calls · dashed = data",
                    "The App runs the same code; dashboards need no app and no token. [README.md:16-17; tools/deploy_app.py:146-151; tools/app_start.py:21-39; app/api/refresh.py:48-69]")


# ================================================================== swimlane helper
def S(d, id_, col, lane, title, bl, i, o, w=170, mono=True, chips="", shape=False):
    n = d.add(str(id_), col, lane, "stepio", title, bl, mono=mono, w=w, extra=(i, o))
    n.chips = chips
    if shape:
        n.tag = "shape"
    return n


def sw(name, lanes, gx=30):
    return Dia(name, gx=gx, mx=8, lanes=lanes, lane_pad=(24, 12))


RD = dict(color=RED, dashed=True)


def d_f1():
    d = sw("F1", ["TERMINAL", "TOOLS", "DATABRICKS", "DATA DIR", "APP + BROWSER"])
    S(d, 1, 0, 0, "python tools/first_run.py", "Picks results/<UTC stamp>", "argv [--windows N]", "out dir path")
    S(d, 2, 0, 1, "_preflight_env", "Needs host, HTTP path, token; env wins over .env", "3 env names", "ok or exit 2")
    S(d, 3, 1, 1, "window_plan(days)", "One query per distinct days_used", "days 30 (default_window)", "query 7, 30; 90 = alias")
    S(d, 4, 1, 2, "checks via snapshot.connect", "Thread pool 4, LIMIT max_rows + 1", "direct_sql/*.sql", "<= 50,000 rows/check")
    S(d, 5, 2, 3, "write <stamp>.tmp, rename", "Findings, dims, tags, storage, manifest, report", "parquet", "results/<stamp>/")
    S(d, 6, 3, 1, "_load_root", "Findings, aliases, dims, tags, meta", "results/<stamp>", "audit.duckdb.tmp")
    S(d, 7, 3, 3, "os.replace + sidecar + pointer", "Pointer only inside the data dir", ".tmp", "audit.duckdb, run_results.json, current_db.txt")
    S(d, 8, 4, 0, "prints next command", "Tells you what to run next", "rc 0", "python -m app.api + URL")
    S(d, 9, 3, 4, "python -m app.api", "Host 127.0.0.1, port 8000; warms findings cache", "AUDIT_API_HOST/PORT", "uvicorn running")
    S(d, 10, 4, 4, "GET / then GET /api/status", "Ready: /api/meta, /api/workspaces, /api/findings", "none", 'state "ready"')
    d.add("f2", 2, 1, "fail", "exit 2: config (missing credential, folder in repo)", w=170)
    d.add("f4", 0, 2, "fail", "exit 1: connector, auth, network. 130: interrupted", w=170)
    d.add("f7", 4, 3, "fail", "Windows: file held by running app", w=170)
    for a, b in [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9), (9, 10)]:
        d.edge(str(a), str(b))
    d.edge("2", "f2", **RD)
    d.edge("4", "f4", **RD)
    d.edge("7", "f7", **RD)
    return d.render("lane = system · STEP n = order · IN outlined blue, OUT filled green · red dashed = failure",
                    "Export renames .tmp only on success; a failed run leaves no half folder. [tools/export_direct_results.py:1331-1340; tools/first_run.py:24-25,132-156]")


def d_f2():
    d = sw("F2", ["BROWSER", "API (app/api/app.py)", "SERVICE (app/api/service.py)", "STORE (app/core/data.py)", "DUCKDB"])
    S(d, 1, 0, 0, "useFindingData('cost_by_job', 30, ..., 5)", "Memo key includes the tag filter", "tab props", "Api.finding call")
    S(d, 2, 1, 0, "Api.finding -> getJson", "Arrays repeated, tags as tag=key:value", "params", "GET /api/finding/cost_by_job?window=30&limit=5&offset=0")
    S(d, 3, 1, 1, "_PrincipalMiddleware", "Binds x-forwarded-email for row-level access", "headers", "principal or none")
    S(d, 4, 2, 1, "get_finding", "Settings, window 7/30/90, status, tag filter", "query params", "validated args or 422")
    S(d, 5, 2, 2, "load_outcome", "Outcome order in the diamond", "qid, 30", 'Outcome("ok_rows", df, rows_total)')
    S(d, 6, 2, 3, "read_finding", "WHERE window_days = ?, count, ORDER BY, LIMIT ? OFFSET ?", "bound params", "FindingResult")
    S(d, 7, 2, 4, "findings.f_cost_by_job", "One table per check", "SELECT", "5 rows")
    S(d, 8, 3, 1, "shape JSON", "pricing.apply _disc, classify_columns, records (ids as strings)", "df", "200 body")
    S(d, 9, 3, 0, "hook state", "Stores the response for the tab", "body", '{phase:"ready", outcome, data}')
    d.add("d5", 3, 2, "decision", "outcome decided in order", w=170, h=96)
    d.add("br", 4, 2, "step", "Branches", "1 source not built, failed or blocked: not_assessed\n2 window not exported: not_assessed\n3 count 0 in window: ok_empty_window\n4 filters exclude all: ok_empty_filters\n5 else: ok_rows", w=170)
    d.add("f404", 0, 1, "fail", "unknown query_id: 404", w=170)
    d.add("ferr", 1, 3, "fail", "DuckDB or OS error: 200 with outcome error", w=170)
    for a, b in [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9)]:
        d.edge(str(a), str(b))
    d.edge("5", "d5")
    d.edge("d5", "br")
    d.edge("4", "f404", **RD)
    d.edge("6", "ferr", **RD)
    return d.render("lane = layer · diamond = outcome decision · red dashed = error path",
                    "The route never builds SQL; not_assessed and empty-window are decided before rows are read. [app/api/service.py:427-608; app/api/app.py:1164-1321]")


def d_f3():
    d = sw("F3", ["VIEWER / UI", "API", "REFRESH THREAD", "TOOLS (subprocess)", "DATA DIR"])
    S(d, 1, 0, 0, "pick days, click", "Choices 7, 15, 30, 60, 90", "days", "POST /api/refresh?days=15")
    S(d, 2, 0, 1, "post_refresh", "Days 1..90", "days", "refresh.start(days)")
    S(d, 3, 0, 2, "start", "One run at a time, under a lock", "days", '{running:true, step:"export"}')
    S(d, 4, 1, 3, "export", "Local-time stamp", "--windows N --max-rows 500000", "results/<%Y%m%d-%H%M%S>")
    S(d, 5, 2, 3, "load", "New file each run", "results/<stamp>", "audit-<stamp>.duckdb")
    S(d, 6, 2, 2, "split", "Only rls apply=export on an App", "db", "one file per grant set")
    S(d, 7, 3, 4, "switch", "current_db.txt + AUDIT_DB", "new db", "pointer updated")
    S(d, 8, 4, 4, "cleanup", "Keep new + previous db, newest two results", "old files", "deleted")
    S(d, 9, 3, 0, "poll every 3 s", "Reload on done", "GET /api/refresh", "step done or failed")
    S(d, 10, 4, 1, "next request", "New file identity, new shared connection", "request", "reads new db")
    d.add("f", 1, 2, "fail", "any exception: step failed, error text; old data stays", w=170)
    for a, b in [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9), (9, 10)]:
        d.edge(str(a), str(b))
    d.edge("4", "f", **RD)
    return d.render("lane = actor or system · STEP n = order · red dashed = failure",
                    "Refresh never replaces the open file; old handles close after a 120 s grace. Export times out at 4 h, load at 30 min; with rls on only refresh.admins or unrestricted users may start it (else 403). [app/api/refresh.py:20-22,31-95; app/core/data.py:279-339; app/api/app.py:391-412]")


def d_f4():
    U = '<span class="chipUA u">U</span>'
    A = '<span class="chipUA a">A</span>'
    d = sw("F4", ["VIEWER", "APPS PLATFORM", "APP (app_start, app.api, rls)", "EXPORT (refresh subprocess)", "WAREHOUSE"])
    S(d, 1, 0, 1, "start command", "app.yaml env: HOST, HTTP_PATH (+ TOKEN only U)", "app.yaml", "python tools/app_start.py")
    S(d, 2, 0, 2, "app_start.unpack + main", "Joins wheel parts, sets PYTHONPATH, runs app.api", "wheels/*.part*", "DuckDB importable")
    S(d, 3, 1, 2, "__main__", "DATABRICKS_APP_PORT set", "env", "0.0.0.0:<port>; rls.is_app() true")
    S(d, 4, 1, 0, "GET /api/status", "Proxy adds X-Forwarded-Email", "signed-in viewer", 'no_database: "Get" screen')
    S(d, 5, 2, 2, "POST /api/refresh?days=30", "Same as 3.3", "days", "export subprocess")
    S(d, 6, 2, 3, "_preflight_env", "Token needed only when not app principal", "env", "ok")
    S(d, "7U", 3, 3, "snapshot.connect", "DATABRICKS_TOKEN present: access_token. Runs as token owner", "token", "access_token", chips=U)
    S(d, "7A", 4, 3, "snapshot.connect", "No token: OAuth M2M credentials_provider", "CLIENT_ID/SECRET", "OAuth M2M", chips=A)
    S(d, 8, 3, 4, "system.* SELECTs", "Run on the SQL warehouse", "SQL", "rows to results/<stamp>, then audit-<stamp>.duckdb")
    S(d, 9, 4, 2, "serve by rls.mode()", "off: full db; query: scoped views; export: file per grant set", "principal", "JSON")
    for a, b in [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]:
        d.edge(str(a), str(b))
    d.edge("6", "7U", color=BLUE)
    d.edge("6", "7A", color=ORANGE)
    d.edge("7U", "8", color=BLUE)
    d.edge("7A", "8", color=ORANGE)
    d.edge("8", "9")
    return d.render("U (blue) = run_as user · A (orange) = run_as app · U and A differ only at step 7 and the stored token",
                    "The viewer's email only filters rows; queries never run as the viewer. [tools/snapshot.py:485-555]")


# ================================================================== check end to end
def d_c1():
    d = Dia("C1", gx=44, gy=34, mx=14, my=24, maxbox=250)

    def P(i, c, r, t, sub):
        d.add(i, c, r, "step", t, sub)
    P("1", 1, 0, "Write SQL + header", "app/queries/app/cost/cost_period_over_period.sql:1-67")
    P("2", 1, 1, "Build manifest", "tools/build_manifest.py > app/queries/app/manifest.json")
    d.add("3", 1, 2, "decision", "Lint headers pass?", w=190, h=84)
    P("4", 1, 3, "Registry", "app/core/registry.py")
    P("5", 1, 4, "Grain", "config/grains/cost_period_over_period.yml:1")
    P("6", 1, 5, "dbt finding model", "dbt/models/findings/cost/f_cost_period_over_period.sql")
    P("7", 1, 6, "Direct SQL", "app/direct_sql/cost_period_over_period.sql (__WINDOW_DAYS__)")
    d.add("8", 3, 6, "decision", "thresholds.yml unchanged since SQL generated?", w=210, h=96)
    P("9", 3, 5, "Export", "findings/cost_period_over_period.parquet per window run")
    P("10", 3, 4, "Load", 'findings."f_cost_period_over_period" + alias copies')
    P("11", 3, 3, "Read-time status", "capped ids: CRITICAL reads WARN")
    P("12", 3, 2, "API", "GET /api/finding/cost_period_over_period?window=30")
    P("13", 3, 1, "Tab", "Cost > Trend; also Money tab")
    P("14", 3, 0, "Test", "tests/test_findings/test_cost_period_over_period.py")
    d.add("f3", 0, 2, "fail", "exit 1: bad header", w=130)
    d.add("f8", 2, 6, "fail", "export stops: run build_direct_sql", w=140)
    for a, b in [("1", "2"), ("2", "3"), ("3", "4"), ("4", "5"), ("5", "6"), ("6", "7"), ("7", "8"), ("8", "9"), ("9", "10"),
                 ("10", "11"), ("11", "12"), ("12", "13"), ("13", "14")]:
        d.edge(a, b, num=int(a))
    d.edge("3", "f3", **RD)
    d.edge("8", "f8", **RD)
    return d.render("numbered = order · diamond = gate · red dashed = stop",
                    "One SQL file feeds the dbt model, the direct SQL, the API and the tab. [tools/header_schema.py:15-38; tools/lint_headers.py:44,63-68,163-176; app/core/registry.py:179-184,211,239,314]")


# ================================================================== data model
def er(name, ents, rels, key, cap, gx=34, gy=56, maxbox=212):
    """ents: (id,col,row,schema,table,grain,[rows]); rels: (a,b,c1,c2,keys,card,cite)"""
    d = Dia(name, gx=gx, gy=gy, mx=14, my=22, maxbox=maxbox)
    for id_, c, r, sch, tab, grain, rows in ents:
        d.add(id_, c, r, "ent", tab, grain, tag=sch, rows=rows)
    trs = []
    for k, (a, b, c1, c2, keys, card, cite) in enumerate(rels, 1):
        d.edge(a, b, "", True, num=k, arrow=False, c1=c1, c2=c2, color="#5B6068")
        trs.append((k, d.nodes[a].title, d.nodes[b].title, keys, card, cite))
    html_ = d.render(key, cap)
    return html_ + table(["#", "From", "To", "Keys", "Cardinality", "Cite"], trs, mono=(1, 2, 3))


def d_er_a():
    T, I = "tested", "inferred"
    ents = [
        ("usage", 0, 0, "SYSTEM", "billing.usage", "one billed usage record",
         [("record_id", "PK", I), ("workspace_id", "", ""), ("usage_date", "", ""), ("sku_name, cloud", "", ""), ("usage_unit", "", ""), ("custom_tags, usage_meta", "", "")]),
        ("prices", 1, 0, "SYSTEM", "billing.list_prices", "one price row per SKU and period",
         [("sku_name,cloud", "PK", "logical"), ("usage_unit", "PK", "logical"), ("price_start_time", "PK", "logical"), ("effective_list.default", "", "")]),
        ("attr", 2, 0, "SYSTEM", "billing.attributed_usage", "query share per warehouse-day",
         [("workspace_id", "", ""), ("warehouse_id", "", ""), ("usage_date", "", ""), ("query_tags", "", "")]),
        ("jobs", 3, 0, "SYSTEM", "lakeflow.jobs", "SCD2 job versions",
         [("workspace_id", "", ""), ("job_id", "", ""), ("change_time", "", ""), ("tags", "", "")]),
        ("day", 0, 1, "TAGS", "cost_day", "day x workspace x SKU x flags x signature",
         [("11 grain columns", "PK", I), ("usd", "", ""), ("unpriced_quantity", "", "")]),
        ("alloc", 1, 1, "TAGS (ephemeral)", "cost_alloc", "(billing row, part)",
         [("rid", "FK", ""), ("compute_kind, _id", "", ""), ("work_kind, _id", "", ""), ("work_tags", "", ""), ("list_rate, qty", "", ""), ("+ 14 columns", "", "")]),
        ("unit", 2, 1, "TAGS", "cost_unit", "window x allocation unit",
         [("window_days", "PK", T), ("unit_id", "PK", T), ("workspace_id", "", ""), ("compute_kind, _id", "", ""), ("work_kind, _id", "", ""), ("usd, unpriced_qty", "", "")]),
        ("recon", 3, 1, "TAGS", "cost_reconciliation", "window x usage_unit",
         [("window_days", "PK", T), ("usage_unit", "PK", T), ("billing_usd", "", ""), ("unit_usd", "", "")]),
        ("utag", 2, 2, "TAGS", "cost_unit_tag", "unit x level x key x value",
         [("window_days, unit_id", "PK", T), ("level, tag_key", "PK", T), ("tag_value", "PK", T), ("window_days, unit_id", "FK", ""), ("raw_key, source, usd", "", "")]),
        ("hist", 3, 2, "TAGS", "tag_workspace_history", "workspace x key x version",
         [("workspace_id, tag_key", "PK", T), ("valid_from", "PK", T), ("valid_to, tag_value", "", ""), ("is_allocating, is_current", "", "")]),
        ("dw", 0, 3, "DIMS", "dim_workspace", "workspace ever seen", [("workspace_id", "PK", I)]),
        ("dwh", 1, 3, "DIMS", "dim_warehouse", "warehouse, latest row", [("warehouse_id", "PK", T)]),
        ("dj", 2, 3, "DIMS", "dim_job", "job, latest SCD2 row", [("workspace_id", "PK", T), ("job_id", "PK", T)]),
    ]
    rels = [
        ("usage", "alloc", "1", "N", "rid = record_id", "1:N, splits into parts", "dbt/models/tags/cost_alloc.sql:1-6"),
        ("prices", "alloc", "0..1", "N", "sku_name, cloud, usage_unit, time range", "0..1:N", "dbt/models/tags/cost_alloc.sql:38-45"),
        ("attr", "alloc", "N", "N", "workspace_id, warehouse_id, usage_date", "N:N", "dbt/models/tags/cost_alloc.sql:56-67,91-92"),
        ("jobs", "alloc", "1", "N", "workspace_id, job_id (latest row)", "1:N", "dbt/models/tags/cost_alloc.sql:115"),
        ("unit", "alloc", "N", "N", "unit_id, window", "N:N (per window N parts : 1 unit)", "dbt/models/tags/cost_unit.sql:13-22,37"),
        ("day", "alloc", "1", "N", "11 grain columns", "1:N aggregate", "dbt/models/tags/cost_day.sql:17-21,52-60"),
        ("unit", "utag", "1", "N", "window_days, unit_id", "1:N", "app/core/rollup.py:330-333"),
        ("recon", "unit", "1", "N", "window_days, usage_unit", "1:N aggregate", "dbt/models/tags/cost_reconciliation.sql:51-56,147"),
        ("hist", "utag", "N", "N", "workspace_id, usage_date in valid range (inferred)", "N:N", "dbt/models/tags/cost_unit_tag.sql:51-59"),
        ("unit", "dw", "N", "1", "workspace_id (inferred)", "N:1", "dbt/models/tags/cost_unit.sql:26"),
        ("unit", "dwh", "N", "1", "compute_id where compute_kind = warehouse (inferred)", "N:1", "dbt/models/tags/cost_alloc.sql:33-35"),
        ("unit", "dj", "N", "1", "workspace_id, work_id where work_kind = job (inferred)", "N:1", "dbt/models/tags/cost_alloc.sql:108-109"),
    ]
    return er("ERA", ents, rels, "dashed = logical key (join in code) · numbers match the table · 1 / N / 0..1 = cardinality at that end · no solid lines: no key is enforced · PK chip: tested or inferred",
              "Grain changes twice: part, then window x unit, then unit x level x key x value.")


def d_er_b():
    T, I = "tested", "inferred"
    ents = [
        ("tw", 0, 0, "TAGS", "tag_workspace", "current value per workspace x key",
         [("workspace_id, tag_key", "PK", T), ("tag_value, share", "", ""), ("coverage, reason", "", "")]),
        ("th", 1, 0, "TAGS", "tag_workspace_history", "workspace x key x version", [("workspace_id, tag_key", "PK", T), ("valid_from", "PK", T)]),
        ("te", 2, 0, "TAGS", "tag_entity", "tagged object x key",
         [("entity_type", "PK", T), ("workspace_id", "PK", T), ("entity_id", "PK", T), ("tag_key", "PK", T), ("raw_key, tag_value", "", "")]),
        ("ti", 3, 0, "TAGS", "tag_index", "key x value x source",
         [("tag_key, tag_value", "PK", T), ("source", "PK", T), ("object_count, dbus", "", "")]),
        ("hint", 0, 1, "DIMS", "int_workspace_tag_hints", "workspace x canonical key", [("workspace_id", "PK", T), ("canonical_key", "PK", T)]),
        ("dw", 1, 1, "DIMS", "dim_workspace", "workspace ever seen",
         [("workspace_id", "PK", I), ("name, env, env_source", "", ""), ("<top tag key> columns", "", "")]),
        ("seed", 2, 1, "SEEDS", "workspace_env_overrides", "one override per workspace", [("workspace_id", "", "convention")]),
        ("pu", 0, 2, "TAGS", "perf_unit", "window x unit",
         [("window_days, unit_id", "PK", T), ("statements, failed", "", ""), ("duration_ms", "", "")]),
        ("put", 1, 2, "TAGS", "perf_unit_tag", "unit x level x key x value", [("5 key columns", "PK", T)]),
        ("qk", 2, 2, "TAGS", "query_tag_keys", "window x unit x keys x origin", [("7 columns", "PK", I)]),
        ("f", 3, 1, "FINDINGS", "f_<qid> (x160)", "window x grain columns", [("window_days + grains", "PK", T), ("status", "", ""), ("+ check columns", "", "")]),
        ("dj", 0, 3, "DIMS", "dim_job", "job, latest SCD2 row", [("workspace_id", "PK", T), ("job_id", "PK", T)]),
        ("dc", 1, 3, "DIMS", "dim_cluster", "cluster, latest row", [("cluster_id", "PK", T)]),
        ("dwh", 2, 3, "DIMS", "dim_warehouse", "warehouse, latest row", [("warehouse_id", "PK", T)]),
        ("dp", 3, 3, "DIMS", "dim_pipeline", "pipeline, latest row", [("workspace_id", "PK", T), ("pipeline_id", "PK", T)]),
    ]
    rels = [
        ("tw", "th", "1", "N", "workspace_id, tag_key (is_current)", "1:1..N", "dbt/models/tags/tag_workspace.sql:12-15"),
        ("tw", "te", "1", "0..1", "workspace_id, tag_key; entity_type = workspace", "1:0..1", "dbt/models/tags/tag_entity.sql:40-45"),
        ("ti", "te", "1", "N", "tag_key, tag_value, source", "groups", "dbt/models/tags/tag_index.sql:69"),
        ("hint", "tw", "N", "0..1", "workspace_id, canonical_key = tag_key", "N:0..1", "dbt/models/dims/int_workspace_tag_hints.sql:108-109"),
        ("dw", "hint", "1", "N", "workspace_id", "1:N", "dbt/models/dims/dim_workspace.sql:124-128,154"),
        ("dw", "seed", "1", "0..1", "workspace_id", "1:0..1", "dbt/models/dims/dim_workspace.sql:115-123,153"),
        ("pu", "put", "1", "N", "window_days, unit_id", "1:N", "app/core/rollup.py:396-399"),
        ("pu", "qk", "1", "N", "window_days, unit_id", "1:N", "app/core/tag_compliance.py:344"),
        ("put", "th", "N", "N", "workspace_id, query_date range (inferred)", "N:N", "dbt/models/tags/perf_unit_tag.sql:162-176"),
        ("f", "dw", "N", "1", "workspace_id (app filter)", "N:1", "app/core/data.py:517-525"),
        ("f", "dj", "N", "1", "workspace_id, job_id (names)", "N:1", "app/api/app.py:619"),
        ("f", "dwh", "N", "1", "warehouse_id", "N:1", "app/core/data.py:524"),
        ("f", "dc", "N", "1", "cluster_id", "N:1", "app/core/data.py:525"),
    ]
    return er("ERB", ents, rels, "same as the previous diagram · standalone cards have no key to others (listed below)",
              "Findings join dims only by ids at read time; tag tables join on workspace and key.")


# ================================================================== rule diagrams
def mini(name, key, cap, build, gx=40, gy=44, maxbox=190):
    d = Dia(name, gx=gx, gy=gy, mx=14, my=18, maxbox=maxbox)
    build(d)
    return d.render(key, cap)


def r1(d):
    d.add("u", 0, 0, "step", "usage row")
    d.add("q", 1, 0, "decision", "price row active at usage_end_time?")
    d.add("y", 2, 0, "step", "usd = qty x list_rate")
    d.add("n", 2, 1, "shape", "unpriced_quantity += qty, usd NULL")
    d.add("lp", 0, 1, "step", "list_prices")
    d.add("m", 1, 1, "routine", "list_prices() macro", "USD only, overlaps capped")
    d.edge("u", "q"); d.edge("q", "y", "yes"); d.edge("q", "n", "no")
    d.edge("lp", "m"); d.edge("m", "q")


def r2(d):
    d.add("n1", 0, 0, "step", "billing row, priced")
    d.add("d1", 1, 0, "decision", "warehouse AND DBU?")
    d.add("p1", 2, 0, "step", "1 part, latest job or pipeline tags")
    d.add("s1", 1, 1, "routine", "split_f", "LEAST(attr, billed) / billed")
    d.add("q1", 2, 1, "step", "query parts")
    d.add("rem", 1, 2, "shape", "remainder: warehouse_idle or warehouse_unsplit")
    d.add("cu", 3, 1, "step", "cost_unit, cost_unit_tag", "work, compute, workspace")
    d.add("d2", 3, 2, "decision", "first real value: work, compute, workspace?")
    d.add("ut", 2, 2, "step", "__untagged__")
    d.add("d3", 2, 3, "decision", "other non-platform tag on unit?")
    d.add("ot", 1, 3, "step", "other_tags")
    d.add("nt", 3, 3, "step", "no_tags")
    d.edge("n1", "d1"); d.edge("d1", "p1", "no"); d.edge("d1", "s1", "yes")
    d.edge("s1", "q1"); d.edge("s1", "rem"); d.edge("p1", "cu"); d.edge("q1", "cu"); d.edge("rem", "cu", dashed=True)
    d.edge("cu", "d2"); d.edge("d2", "ut", "no"); d.edge("ut", "d3")
    d.edge("d3", "ot", "yes"); d.edge("d3", "nt", "no")


def r3(d):
    d.add("w", 0, 0, "step", "WASTE_ITEMS", "11 checks")
    d.add("d1", 1, 0, "decision", "row CRITICAL or WARN?")
    d.add("x", 1, 1, "tbc", "not counted")
    d.add("d2", 2, 0, "decision", "dollar column (est_wasted_usd_list) non-NULL?")
    d.add("s", 3, 0, "step", "sum once per wasteRowKey x (1 - discount)")
    d.add("t", 3, 1, "step", "total")
    d.add("nn", 2, 1, "step", "DBU or spend shown, not added")
    d.add("fl", 0, 1, "step", "below-floor $ shown separately")
    d.edge("w", "d1"); d.edge("d1", "x", "no"); d.edge("d1", "d2", "yes")
    d.edge("d2", "s", "yes"); d.edge("d2", "nn", "no"); d.edge("s", "t")


def r4(d):
    d.add("e", 0, 0, "step", "exported status")
    d.add("d1", 1, 0, "decision", "admin group grantee?")
    d.add("d2", 2, 0, "decision", "CRIT or WARN and below floor?")
    d.add("d3", 3, 0, "decision", "CRITICAL and capped id?")
    d.add("u", 4, 0, "step", "status unchanged")
    d.add("o1", 1, 1, "step", "OK")
    d.add("o2", 2, 1, "step", "OK + below_floor")
    d.add("o3", 3, 1, "step", "WARN")
    d.edge("e", "d1"); d.edge("d1", "d2", "no"); d.edge("d2", "d3", "no"); d.edge("d3", "u", "no")
    d.edge("d1", "o1", "yes"); d.edge("d2", "o2", "yes"); d.edge("d3", "o3", "yes")


def r5(d):
    d.add("h", 0, 0, "step", "X-Forwarded-Email")
    d.add("p", 1, 0, "routine", "principal", "lower, trim")
    d.add("t", 0, 1, "data", "entitlements table")
    d.add("en", 1, 1, "routine", "rls.entitlements")
    d.add("d1", 2, 0, "decision", "load error, unknown user, columns widened?")
    d.add("nr", 2, 1, "fail", "no rows")
    d.add("d2", 3, 0, "decision", "one grant passes all columns?")
    d.add("v", 3, 1, "step", "scoped view (query) or own file (export)")
    d.edge("h", "p"); d.edge("t", "en"); d.edge("p", "d1"); d.edge("en", "d1")
    d.edge("d1", "nr", "yes", color=RED, dashed=True); d.edge("d1", "d2", "no"); d.edge("d2", "v", "yes")


def r6(d):
    qs = ["account history shorter than 2 x window?", "current side has unpriced usage?", "previous side has unpriced usage?", "current spend < $50?", "previous = 0, or change >= 50 / 25?"]
    rs = ["NOT_ASSESSED previous_window_not_covered", "NOT_ASSESSED current_period_unpriced", "NOT_ASSESSED previous_period_unpriced", "OK", "CRITICAL (previous 0 or >= 50), WARN (>= 25), else OK"]
    for i, (q, r) in enumerate(zip(qs, rs)):
        d.add(f"q{i}", 0, i, "decision", q, w=210, h=64)
        d.add(f"r{i}", 1, i, "step", r)
    d.add("cap", 2, 4, "shape", "app cap: CRITICAL reads WARN")
    for i in range(5):
        d.edge(f"q{i}", f"r{i}", "yes")
        if i < 4:
            d.edge(f"q{i}", f"q{i + 1}", "no")
    d.edge("r4", "cap")


def r7(d):
    d.add("w", 0, 0, "step", "warehouse in window")
    d.add("d1", 1, 0, "decision", "any attributed_usage row?")
    d.add("a", 2, 0, "step", "cost_basis statement", "SUM(active qty x list_rate)")
    d.add("d2", 1, 1, "decision", "billed hours?")
    d.add("f", 0, 1, "fail", "NOT_ASSESSED no_billing_rows")
    d.add("b", 2, 1, "step", "cost_basis run_time_share", "hour_usd x weight / hour total")
    d.add("w2", 3, 0, "step", "FAILED share = est_wasted_usd_list")
    d.add("d3", 3, 1, "decision", ">= $50 CRITICAL, >= $5 WARN")
    d.edge("w", "d1"); d.edge("d1", "a", "yes"); d.edge("d1", "d2", "no"); d.edge("d2", "f", "no", color=RED, dashed=True)
    d.edge("d2", "b", "yes"); d.edge("a", "w2"); d.edge("b", "w2"); d.edge("w2", "d3")


# ================================================================== config, deploy, tests
def d_cfg():
    d = Dia("CFG", gx=56, gy=40, mx=14, my=22, maxbox=210)
    d.add("s", 0, 0, "data", "config/settings.yml", "tracked")
    d.add("l", 0, 1, "data", "config/settings.local.yml", "git-ignored")
    d.add("ls", 1, 0, "routine", "config.load_settings", "local wins key by key")
    d.add("c", 2, 0, "routine", "app/api, app/core, export, load")
    d.add("am", 2, 1, "routine", "dbt_run floor and mask loaders", "via app_config, local wins")
    d.add("a", 0, 2, "data", "config/app.yml", "+ app.local.yml")
    d.add("r", 1, 2, "routine", "rls.load, deploy_app")
    d.add("env", 0, 3, "data", ".env or env vars", "git-ignored")
    d.add("p", 1, 3, "routine", "_preflight_env", "then snapshot.connect")
    d.edge("s", "ls", "", True, num=1); d.edge("l", "ls", "merged over", True, num=2); d.edge("ls", "c", "", True, num=3)
    d.edge("ls", "am", "", True, num=4); d.edge("a", "r", "", True, num=5); d.edge("env", "p", "", True, num=6)
    return d.render("grey = file · teal = code · dashed = data",
                    "Every reader, dbt_run included, merges the local file over settings.yml. [app/core/config.py:534-573; tools/dbt_run.py:140-170; app/core/rls.py:83-98]")


def d_dep():
    d = Dia("DEP", gx=44, gy=56, mx=14, my=30, maxbox=190)
    d.add("d", 0, 0, "entry", "tools/deploy_app.py")
    d.add("cli", 1, 0, "routine", "Databricks CLI", "token passed as DATABRICKS_HOST/TOKEN")
    d.add("st", 0, 1, "data", "<data dir>/app-deploy/<name>", "app config, dbt, tools, wheel parts <= 9 MB, app.yaml")
    d.add("t", 1, 1, "tbc", "Terminal", "prints GRANT SQL (run_as app)")
    d.add("fo", 3, 1, "data", "deployer's workspace home folder / <name>")
    d.add("app", 2, 0, "step", "Databricks App <name>", "default name lakehouse-health")
    d.add("wh", 3, 0, "step", "SQL warehouse", "resource CAN_USE for run_as app")
    d.add("v", 2, 2, "actor", "Viewer", "Permissions: Can use")
    d.edge("d", "st", "stage", num=1); d.edge("st", "fo", "databricks sync", True, num=2); d.edge("d", "cli", "apps get/create/update/start", num=3)
    d.edge("cli", "app", "apps deploy", num=4); d.edge("app", "wh", "export on Get/Refresh", num=5); d.edge("v", "app", "open URL", num=6)
    d.edge("d", "t", "A", True, color=ORANGE, num=7)
    d.frame("DEVELOPER MACHINE", ["d", "cli", "st", "t"])
    d.frame("WORKSPACE", ["fo", "app", "wh"])
    d.frame("PLATFORM", ["v"])
    return d.render("7 (orange) = run_as app only · solid = calls · dashed = files move",
                    "A redeploy wipes the app disk; click Get again. [docs/databricks-app.md:93-97; tools/deploy_app.py:127-216; config/app.yml:4-5]")


def d_t1():
    d = Dia("T1", gx=44, gy=40, mx=14, my=22, maxbox=190)
    d.add("bd", 0, 0, "routine", "build_fixture_ddl.py")
    d.add("ddl", 1, 0, "data", "tests/fixtures/ddl.py", "49 source tables")
    d.add("bf", 2, 0, "routine", "build_fixtures.py")
    d.add("pq", 3, 0, "data", "tests/fixtures/parquet/", "git-ignored")
    d.add("bl", 2, 1, "routine", "tests/fixtures/<builder>.py", "build(con)")
    d.add("dbt", 3, 1, "routine", "dbt_run.py build --target test")
    d.add("db", 3, 2, "data", "tests/db_audit_test.duckdb")
    d.add("cf", 2, 2, "routine", "tests/conftest.py", "tmp config + env")
    d.add("py", 1, 2, "routine", "pytest")
    d.add("fc", 0, 2, "tbc", "fixture_coverage --check", "grain + test per id")
    d.add("fa", 0, 3, "routine", "tests/fake_account/build.py", "the demo account")
    d.add("tf", 1, 3, "routine", "test_fake_account.py", "dbt dev in tmp")
    d.add("ws", 2, 3, "step", "web/src")
    d.add("vt", 3, 3, "routine", "vitest")
    d.edge("bd", "ddl", num=1); d.edge("ddl", "bf", num=2); d.edge("bl", "bf", num=3); d.edge("bf", "pq", "", True, num=4)
    d.edge("pq", "dbt", "", True, num=5); d.edge("dbt", "db", "", True, num=6); d.edge("db", "cf", num=7); d.edge("cf", "py", num=8)
    d.edge("fa", "tf", num=9); d.edge("ws", "vt", num=10); d.edge("fc", "py", "", True, color=GREY)
    return d.render("teal = code · grey = data · dashed = data · dashed grey = coverage gate",
                    "pytest reads a dbt-built DuckDB made from fixture parquet. [tests/fixtures/build_fixtures.py:1-20; tests/conftest.py:118-130]")


# ================================================================== graphviz lineage
SRC = ["billing.usage", "billing.list_prices", "billing.attributed_usage", "lakeflow.jobs", "lakeflow.pipelines",
       "lakeflow.job_run_timeline", "compute.clusters", "compute.warehouses", "compute.instance_pools", "query.history",
       "access.workspaces_latest", "information_schema *_tags", "ai_gateway.usage"]
MODELS = ["cost_alloc", "cost_unit", "cost_unit_tag", "cost_day", "cost_reconciliation", "tag_workspace_history", "tag_workspace",
          "tag_entity", "tag_index", "bill_tag_dates", "perf_unit", "perf_unit_tag", "query_tag_keys", "int_workspace_tag_hints",
          "dim_workspace", "dim_job", "dim_cluster", "dim_warehouse", "dim_pipeline", "dim_notebook"]
LE = [("billing.list_prices", "list_prices()"), ("list_prices()", "cost_alloc"), ("list_prices()", "cost_reconciliation"),
      ("billing.usage", "cost_alloc"), ("billing.attributed_usage", "cost_alloc"), ("lakeflow.jobs", "cost_alloc"),
      ("lakeflow.pipelines", "cost_alloc"), ("cost_alloc", "cost_unit"), ("cost_alloc", "cost_unit_tag"), ("cost_alloc", "cost_day"),
      ("billing.usage", "tag_workspace_history"), ("tag_workspace_history", "cost_unit_tag"), ("tag_workspace_history", "tag_workspace"),
      ("tag_workspace_history", "perf_unit_tag"), ("cost_unit", "cost_reconciliation"), ("billing.usage", "cost_reconciliation"),
      ("billing.attributed_usage", "cost_reconciliation"), ("query.history", "perf_unit"), ("perf_unit", "perf_unit_tag"),
      ("query.history", "perf_unit_tag"), ("compute.warehouses", "perf_unit_tag"), ("compute.clusters", "perf_unit_tag"),
      ("compute.instance_pools", "perf_unit_tag"), ("query.history", "query_tag_keys"), ("tag_workspace", "tag_entity"),
      ("compute.warehouses", "tag_entity"), ("compute.clusters", "tag_entity"), ("compute.instance_pools", "tag_entity"),
      ("lakeflow.jobs", "tag_entity"), ("lakeflow.pipelines", "tag_entity"), ("query.history", "tag_entity"),
      ("information_schema *_tags", "tag_entity"), ("billing.usage", "tag_entity"), ("tag_entity", "tag_index"),
      ("tag_workspace", "tag_index"), ("billing.usage", "tag_index"), ("query.history", "tag_index"),
      ("billing.attributed_usage", "tag_index"), ("ai_gateway.usage", "tag_index"), ("billing.usage", "bill_tag_dates"),
      ("billing.usage", "int_workspace_tag_hints"), ("tag_workspace", "int_workspace_tag_hints"),
      ("int_workspace_tag_hints", "dim_workspace"), ("workspace_env_overrides (seed)", "dim_workspace"),
      ("access.workspaces_latest", "dim_workspace"), ("billing.usage", "dim_workspace"), ("lakeflow.job_run_timeline", "dim_workspace"),
      ("lakeflow.jobs", "dim_job"), ("compute.clusters", "dim_cluster"), ("compute.warehouses", "dim_warehouse"),
      ("lakeflow.pipelines", "dim_pipeline"), ("billing.usage", "dim_notebook")]


def lineage_svg():
    dot = shutil.which("dot") or r"C:\Program Files\Graphviz\bin\dot.exe"
    q = lambda n: '"' + n + '"'
    g = ['digraph L { rankdir=LR; nodesep=0.22; ranksep=0.55; bgcolor="transparent";',
         'node [shape=box, style="rounded,filled", fontname="Courier New", fontsize=11, penwidth=1.2, margin="0.1,0.05"];',
         'edge [color="#8A8F98", penwidth=1.1, style=dashed, arrowsize=0.6];']
    for n in SRC:
        g.append(f'{q(n)} [fillcolor="#ECEDEF", color="#9AA0A8"];')
    g.append('"workspace_env_overrides (seed)" [fillcolor="#FFFFFF", color="#17191D"];')
    g.append('"list_prices()" [fillcolor="#F3EFFB", color="#6B4FA8"];')
    for n in MODELS:
        extra = ', style="rounded,filled,dashed"' if n == "cost_alloc" else ""
        g.append(f'{q(n)} [fillcolor="#E4F1EF", color="#2F7F78"{extra}];')
    for a, b in LE:
        g.append(f"{q(a)} -> {q(b)};")
    g.append("}")
    try:
        r = subprocess.run([dot, "-Tsvg"], input="\n".join(g), capture_output=True, text=True, encoding="utf-8", timeout=60)
        svg = r.stdout
        if "<svg" not in svg:
            raise RuntimeError(r.stderr)
    except Exception as ex_:
        WARN.append(f"graphviz failed: {ex_}")
        return "<p>Lineage graph not rendered: Graphviz not available at build time.</p>"
    svg = svg[svg.index("<svg"):]
    svg = re.sub(r"<svg width=\"[^\"]*\" height=\"[^\"]*\"", "<svg", svg, count=1)
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    return f'<div class="lin">{svg}</div>'


# ================================================================== content
def s_orientation():
    terms = [
        ("check", "One SQL file with a 14-field header [tools/header_schema.py:15-30]"),
        ("finding", "Check whose healthy text is not \"n/a -\" and emits AS status [app/core/registry.py:179-184]"),
        ("window_days", "7, 30 or 90 label on every windowed row [dbt/dbt_project.yml:31]"),
        ("status", "Row verdict: OK, WARN, CRITICAL, NOT_ASSESSED [tools/lint_headers.py:44]"),
        ("effective status", "Status after admin groups, floors, WARN cap at read time [app/core/materiality.py:142-168]"),
        ("outcome", "Per-check API answer: ok_rows, ok_empty_window, ok_empty_filters, not_assessed, error [app/api/service.py:103-107]"),
        ("cost unit", "One allocation unit (compute x work) per window [dbt/models/tags/cost_unit.sql:6-18]"),
        ("export / load", "Warehouse to parquet folder / parquet folder to DuckDB [tools/first_run.py:162-194]"),
    ]
    b = bullets([
        "Three ways, same checks: run anywhere, Databricks App, AI/BI dashboards. [README.md:10-17]",
        "Checks: 100 vendored + 60 app-owned executable SQL files. [app/core/registry.py:239-241; 160 files in dbt/models/findings]",
        "Export only reads (SELECT, DESCRIBE, SHOW); ways 1-2 write nothing to Databricks. [README.md:14; docs/databricks-app.md:4-5; config/settings.yml:96-97]",
        "App windows are always 7, 30 and 90 days. [app/api/service.py:110; tools/export_direct_results.py:219]",
        "Every dollar is list price; discount only adds a what-if twin. [app/core/pricing.py:24-26,48-57]",
        "Not assessed is never a pass; values read \"–\" with a reason. [README.md:360]",
    ])
    return section("orientation", "1. Orientation", d_l0() + b + "<h4>Terms</h4>" + table(["Term", "Meaning"], terms, mono=(0,)))


def s_arch():
    b = bullets([
        "One FastAPI process serves /api/* JSON and the built React page. [app/api/app.py:1461-1496]",
        "app/api never builds SQL; every read goes through app/core. [app/api/__init__.py:1-10]",
        "DuckDB opened read-only; one shared connection per db file. [app/core/data.py:304-329; app/api/__main__.py:21]",
        "Db order: AUDIT_DB, current_db.txt, <data dir>/audit.duckdb, data/db_audit.duckdb. [app/core/data.py:203-216]",
        "UI fetches via web/src/api.ts (getJson, refresh POST) and tag_rollup.tsx. [web/src/api.ts:24-37,78; web/src/components/tag_rollup.tsx:116]",
        "Built UI is committed in app/web/dist; running needs no Node. [README.md:44; web/vite.config.ts:37]",
        "dbt road targets DuckDB only (dev, test); d_* models are never built. [tools/dbt_run.py:4; tools/generate_direct_models.py:15-24]",
        "Dashboards set no refresh schedule; numbers change when the notebook reruns. [databricks_dashboards/README.md:81-82]",
        "<data dir> = OS user data folder + databricks-audit (LOCALAPPDATA, Application Support, XDG_DATA_HOME). [app/core/datasource.py:14-31]",
    ])
    return section("architecture", "2. Architecture", "<h4>Run anywhere and the dbt dev road</h4>" + d_l2a() + "<h4>Databricks-hosted paths</h4>" + d_l2b() + b)


def s_flows():
    # ---------- 3.1
    f1_steps = [
        ("1", "python tools/first_run.py [--windows N]", "argv", "out dir path", "Picks results/<UTC %Y%m%dT%H%M%SZ> [tools/first_run.py:96-106,166]"),
        ("2", "_preflight_env", "3 env names", "ok | exit 2", "Needs host, HTTP path, token (env wins over .env) [tools/export_direct_results.py:816-873]"),
        ("3", "window_plan(days)", "days 30 (default_window)", "query 7, 30; 90 = alias", "One query per distinct days_used [tools/export_direct_results.py:211-243]"),
        ("4", "checks via snapshot.connect", "app/direct_sql/*.sql", "<= 50,000 rows/check", "Thread pool 4, LIMIT max_rows + 1 [tools/export_direct_results.py:183-185,397-420,1179-1215]"),
        ("5", "write <stamp>.tmp, rename", "parquet", "results/<stamp>/", "Findings, dims, tags, storage, manifest, report [tools/export_direct_results.py:1289-1340]"),
        ("6", "load_direct_results._load_root", "results/<stamp>", "audit.duckdb.tmp", "Findings, aliases, dims, tags, meta [tools/load_direct_results.py:471,516-547]"),
        ("7", "os.replace + sidecar + pointer", ".tmp", "audit.duckdb, audit.run_results.json, current_db.txt", "Pointer only inside data dir [tools/load_direct_results.py:853,863-864,927-932]"),
        ("8", "prints next command", "rc 0", "python -m app.api + URL", "[tools/first_run.py:191-194]"),
        ("9", "python -m app.api", "AUDIT_API_HOST/PORT", "uvicorn running", "Host 127.0.0.1, port 8000, warms findings cache [app/api/__main__.py:14-25]"),
        ("10", "GET / then GET /api/status", "none", 'state "ready"', "Ready: /api/meta, /api/workspaces, /api/findings [app/api/app.py:350-378; web/src/App.tsx:841-863]"),
    ]
    steps_tbl = ('<div class="tblwrap"><table class="steps"><tr><th>#</th><th>Step</th><th>In</th><th>Out</th><th>Notes</th></tr>' +
                 "".join(f'<tr><td><span class="n">{a}</span></td><td class="m">{e(b)}</td><td class="m">{e(c)}</td><td class="m">{e(dd)}</td><td>{txt(n)}</td></tr>'
                         for a, b, c, dd, n in f1_steps) + "</table></div>")
    load_int = table(["#", "What", "Cite"], [
        (1, "Window aliases from manifest window_aliases, else inferred", "tools/load_direct_results.py:494-514"),
        (2, 'findings."f_<qid>" CREATE OR REPLACE, then one INSERT per alias label', "tools/load_direct_results.py:575-591"),
        (3, "dims: declared dims, else empty stand-in; env added to dim_workspace", "tools/load_direct_results.py:597-622"),
        (4, "tags: 12 tables; tag_entity, bill_tag_dates trimmed to kept keys", "tools/load_direct_results.py:73-77,625-643"),
        (5, "Windowed tag tables alias-copied", "tools/load_direct_results.py:662-673"),
        (6, "cost_reconciliation gets unit_* from loaded cost_unit", "tools/load_direct_results.py:680-700"),
        (7, "apply_bill_to_perf: bill tags replace perf compute tags", "tools/load_direct_results.py:701-703"),
        (8, "config/workspace.csv applied, top-tag columns on dim_workspace", "tools/load_direct_results.py:705-710"),
        (9, "governance, storage, usage, warehouse schemas", "tools/load_direct_results.py:712-803"),
        (10, "rls.entitlements + rls columns when rls enabled", "tools/load_direct_results.py:804-808"),
        (11, "direct_export_meta, direct_export_findings_meta", "tools/load_direct_results.py:812-846"),
        (12, "Off-app with rls: _keep_own_rows keeps only token user's rows", "tools/load_direct_results.py:449-468,851-852"),
    ], mono=(2,))
    tree = ("<data dir>/results/20261110T081500Z/\n  findings/*.parquet\n  dims/\n  tags/\n  storage/\n  manifest.json\n  report.md\n"
            "<data dir>/audit.duckdb\n<data dir>/audit.run_results.json\n<data dir>/current_db.txt")
    c31 = exset(
        ex("ok", "Two subprocesses", "first_run.main([]) with AUDIT_DATA_DIR = tmp",
           ["call 1: [python, tools/export_direct_results.py, --out, <tmp>/results/<stamp>], no --windows",
            "call 2: [python, tools/load_direct_results.py, <same dir>]", "both cwd = repo root"], "from tests/test_first_run.py:77-98"),
        tcard("Window plan per days exported", ["days", "plan"], [("5", "(7,5) (30,5) (90,5)"), ("10", "(7,7) (30,10) (90,10)"), ("30", "(7,7) (30,30) (90,30)"), ("120", "(7,7) (30,30) (90,90)")],
              "from tests/test_export_direct_results.py:296-301. More than 90 adds nothing."),
        ex("edge", "Row cap", ["10 rows, max_rows 3", "3 rows, max_rows 20"], ["3 kept, truncated True, SQL ends LIMIT 4", "not truncated"], "from tests/test_export_direct_results.py:236-260"),
    )
    s31 = ("<h3 id=\"flow-first-run\">3.1 First run: export, load, serve</h3>" + d_f1() + steps_tbl + details("Load internals: order inside _load_root", load_int) +
           c31 + payload("Data dir after one run", tree, "", "text", "illustrative, from README.md:181-191"))
    # ---------- 3.2
    inp = "GET /api/finding/cost_by_job\nwindow=30, limit=5, offset=0\n# optional: workspace_ids, env, status, columns, tag, job_id"
    out = '''{
  "query_id": "cost_by_job", "outcome": "ok_rows",
  "window_days": 30, "requested_window_days": 30,
  "is_finding": true, "windowed": true,
  "header": {"title": "...", "read_this": "...", "healthy": "...", "investigate_if": "...", "actions": ["..."], "next": ["..."], "caveats": "..."},
  "rows_total": 42, "limit": 5, "offset": 0, "returned": 5,
  "columns": [{"name": "workspace_id", "kind": "id"}, {"name": "est_usd_list", "kind": "money"}],
  "rows": [{"workspace_id": "1111", "est_usd_list": 12.5, "status": "WARN"}],
  "discount_pct": 0.0, "truncated": false, "truncated_max_rows": null
}'''
    c32 = exset(
        ex("ok", "Typical ok_rows", "window=30, limit=5", ["200, outcome ok_rows, 5 rows, returned 5, rows_total > 5",
           "header has read_this, healthy, investigate_if, actions, next, caveats", 'workspace_id kind "id", a string'], "from tests/test_api.py:1133-1153"),
        ex("edge", "ok_empty_filters", "workspace_ids=not-a-real-workspace", "rows_in_window > 0, no \"rows\" key", "from tests/test_api.py:1205-1211"),
        ex("bad", "Rejected window", "window=14", '422 "window must be one of [7, 30, 90], got 14"', "derived from app/api/app.py:235-239"),
        ex("bad", "Rejected id", "/api/finding/no_such_check", "404", "derived from app/api/app.py:1205-1206"),
    )
    s32 = ("<h3 id=\"flow-api\">3.2 API request: tab to JSON</h3>" + d_f2() +
           payloads(payload("GET /api/finding/cost_by_job", inp, "IN", "text"),
                    payload("200 application/json (shape from app/api/app.py:1209-1247,1286-1321; values illustrative)", out, "OUT")) + c32 +
           "<h4>Bootstrap on every page load</h4>" + bullets([
               "GET /api/status first; anything but ready shows the first-run screen. [web/src/App.tsx:841-843]",
               "Then /api/meta and /api/workspaces; meta lists window_options [7, 30, 90]. [tests/test_api.py:444-468]",
               "Then /api/findings (cached by data version, window, scope, tag). [app/api/app.py:1026-1115]",
               "findings row status_counts keys are CRITICAL, WARN, OK, NOT_ASSESSED. [tests/test_api.py:1059-1073]"]))
    # ---------- 3.3
    ref = '{"running": true, "step": "export", "days": 15, "started_at": 1762762500.0, "finished_at": null, "error": null, "elapsed_s": 42}'
    cmp_ = table(["", "first_run.py", "Refresh"], [
        ("Rows kept per check", "50,000 [tools/export_direct_results.py:183]", "500,000 [app/api/refresh.py:54]"),
        ("Db file", "audit.duckdb [app/core/datasource.py:11]", "audit-<stamp>.duckdb [app/api/refresh.py:52]"),
        ("Stamp", "UTC %Y%m%dT%H%M%SZ [tools/first_run.py:96-97]", "local %Y%m%d-%H%M%S [app/api/refresh.py:49]"),
        ("App running?", "stop it first on Windows [README.md:80-81]", "runs inside the app"),
    ])
    c33 = exset(ex("ok", "Refresh to 15 days",
                   ["refresh.start(15)", "old audit-20000101-000000.duckdb", "three results stamps"],
                   ["step done, days 15, calls [export, load]", "AUDIT_DB = new .duckdb; old db deleted", "two results folders left; 19991230-000000 gone"],
                   "from tests/test_refresh.py:21-48"))
    s33 = ("<h3 id=\"flow-refresh\">3.3 Refresh in the app</h3>" + d_f3() + payload("GET /api/refresh (shape from app/api/refresh.py:21-28; values illustrative)", ref, "OUT") + c33 +
           "<h4>first_run vs Refresh</h4>" + cmp_)
    # ---------- 3.4
    yml = "command: [python, tools/app_start.py]\nenv:\n- {name: DATABRICKS_SERVER_HOSTNAME, value: <workspace-host>}\n- {name: DATABRICKS_HTTP_PATH, value: /sql/1.0/warehouses/<id>}\n- {name: DATABRICKS_TOKEN, value: <secret, not shown>}"
    ras = table(["", "user (default)", "app"], [
        ("Queries run as", "token owner [docs/databricks-app.md:30]", "app service principal [docs/databricks-app.md:31]"),
        ("Token stored", "yes, app.yaml env [tools/deploy_app.py:148-149]", "no [tests/test_deploy_app.py:128-133]"),
        ("Connector auth", "access_token", "OAuth M2M"),
        ("Warehouse access", "owner's own", "app resource sql-warehouse, CAN_USE [tools/deploy_app.py:79-84]"),
        ("System tables", "owner's grants", "admin runs printed GRANTs [tools/deploy_app.py:92-103]"),
    ])
    c34 = exset(
        ex("ok", "Stage", "stage(out, ENV, wheel)", ["app.yaml command [python, tools/app_start.py]", "wheel parts rejoin byte-identical", "app/web/dist/index.html copied", "no __pycache__, no config/app.local.yml"], "from tests/test_deploy_app.py:93-112"),
        ex("edge", "run_as app", 'run_as="app"', ["env names exactly [DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH]", "token absent"], "from tests/test_deploy_app.py:128-133"),
        ex("ok", "rls query mode (emails replaced with placeholders)", "rls {enabled, apply: query, columns: [workspace]}, header user-a@example.com",
           ["only workspace 1111", "USER-B@example.com: only 2222 (case-insensitive)", "unknown or missing email: nothing"], "from tests/test_rls_api.py:40-70"),
    )
    s34 = ("<h3 id=\"flow-app\">3.4 Databricks App: auth and run-as</h3>" + d_f4() + "<h4>run_as user vs app</h4>" + ras + bullets([
        "rls modes: off when disabled; query or export on an App; export locally. [app/core/rls.py:451-456]",
        "Query mode caches per user; start-up warm-up is skipped. [app/core/rls.py:459-461; app/api/app.py:1122-1123]",
        "/api/meta.rls reports enabled, apply, user, granted, error. [app/core/rls.py:651-679]"]) + c34 +
        payload("app.yaml (run_as user), illustrative placeholders", yml, "", "text"))
    return section("flows", "3. Main flows", s31 + s32 + s33 + s34)


def s_check():
    hdr = '''-- query_id: cost_period_over_period
-- title: Spend this period versus the equal-length period before it, by workspace and product line
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- empty_if: ingestion_lag, no_activity
-- params: :period_days (default 30); :warn_increase_pct (default 25);
--   :crit_increase_pct (default 50); :min_spend_usd (default 50)
-- confidence: needs_confirmation'''
    case = '''CASE
  WHEN snapshot_start > current_date() - 2 * :period_days THEN 'NOT_ASSESSED'
  WHEN current_unpriced_quantity > 0 THEN 'NOT_ASSESSED'
  WHEN previous_unpriced_quantity > 0 THEN 'NOT_ASSESSED'
  WHEN COALESCE(eff_current_cost, 0) < :min_spend_usd THEN 'OK'
  WHEN eff_previous_cost = 0 THEN 'CRITICAL'
  WHEN change_pct >= :crit_increase_pct THEN 'CRITICAL'
  WHEN change_pct >= :warn_increase_pct THEN 'WARN'
  ELSE 'OK'
END AS status'''
    steps = table(["#", "Step", "In", "Out"], [
        (1, "Write SQL + header", "14 header fields + empty_if, not_assessed_reasons [tools/header_schema.py:15-38]", "body from line 68"),
        (2, "Build manifest", "all headers [tools/build_manifest.py:4-8]", "app/queries/app/manifest.json (never hand-edit)"),
        (3, "Lint headers", "status only OK/WARN/CRITICAL/NOT_ASSESSED; windows via :period_days [tools/lint_headers.py:44,63-68,163-176]", "pass, or exit 1"),
        (4, "Registry", "is_finding = healthy not \"n/a -\" AND \"AS status\"; executable = runnable and reads set [app/core/registry.py:179-184,211,239,314]", "check spec"),
        (5, "Grain", "config/grains/cost_period_over_period.yml:1", "[workspace_id, billing_origin_product]"),
        (6, "dbt finding model", "tools/generate_models.py", "dbt/models/findings/cost/f_cost_period_over_period.sql:1-7, windows 7/30/90 stacked"),
        (7, "Direct SQL", "generate_direct_models, then build_direct_sql [dbt/models/databricks_direct/generated_manifest.json:946-947]", "app/direct_sql/cost_period_over_period.sql:1-3 (__WINDOW_DAYS__)"),
        (8, "Thresholds gate", "thresholds.yml unchanged since SQL generated? [tools/export_direct_results.py:354-358]", "else export stops: run build_direct_sql"),
        (9, "Export", "direct SQL per window [tools/export_direct_results.py:938-985]", "findings/cost_period_over_period.parquet"),
        (10, "Load", "parquet [tools/load_direct_results.py:575-591]", 'findings."f_cost_period_over_period" + alias copies'),
        (11, "Read-time status", "in REPORT_IDS_CAPPED_AT_WARN [app/core/materiality.py:49-52]", "CRITICAL reads WARN"),
        (12, "API", "GET /api/finding/cost_period_over_period?window=30 [app/api/app.py:1164]", "JSON"),
        (13, "Tab", "Cost > Trend sub-tab ids; used by Cost and Money tabs [web/src/components/tab_registry.ts:53-56; web/src/tabs/tab_cost.tsx:282; web/src/tabs/tab_money.tsx:444]", "Change vs previous period"),
        (14, "Test", "tests/test_findings/test_cost_period_over_period.py:104-115; fixture_coverage needs grain + test [tools/fixture_coverage.py:1-30]", "gate result"),
    ], "steps", mono=(1,))
    J = journey([
        ("1 billing.usage (priced)", [("side current", "$130.00 list"), ("side previous", "$100.00 list"), ("unpriced", "0")], "priced at effective list price", False),
        ("2 raw_agg to agg", [("current_cost", "130"), ("previous_cost", "100"), ("unpriced", "0"), ("eff_*", '<span class="calc chg">same, calculated</span>')], "empty side becomes real 0", True),
        ("3 check output row", [("est_current_usd_list", "130.0"), ("est_previous_usd_list", "100.0"), ("est_change_usd_list", "30.0"), ("change_pct", '<span class="calc">30.0</span>'),
                                ("price_basis", "priced"), ("status", '<span class="chg">WARN</span>'), ("not_assessed_reason", "null")], "30% >= 25 warn, < 50 crit", True),
        ("4 app effective status", [("status", "WARN"), ("cap", "only changes CRITICAL"), ("below_floor", "false (no floor for this check)")], "read-time rules", False),
        ("5 UI", [("tab", "Cost > Trend"), ("title", "Change vs previous period")], "labels.ts title + why", False),
    ])
    cards = exset(
        ex("ok", "Typical WARN", "130 vs 100", "+30.0% -> WARN", "from tests/test_findings/test_cost_period_over_period.py:104-115"),
        ex("edge", "Capped CRITICAL", "200 vs 100", "+100% -> CRITICAL in table; app shows WARN", "from tests/test_findings/test_cost_period_over_period.py:118-128; app/core/materiality.py:52"),
        ex("edge", "Spend floor", "40 vs 5", "change_pct 700.0 but OK (current < $50)", "from tests/test_findings/test_cost_period_over_period.py:131-142"),
        ex("edge", "Brand-new spend", "80 vs 0", "change_pct None, CRITICAL (app WARN)", "from tests/test_findings/test_cost_period_over_period.py:145-159"),
        ex("bad", "Unpriced usage", "any unpriced usage on a side", "NOT_ASSESSED current_period_unpriced / previous_period_unpriced", "from tests/test_findings/test_cost_period_over_period.py:200-243"),
    )
    body = (bullets(["Worked example: cost_period_over_period (Cost > Trend, \"Change vs previous period\"). [web/src/components/labels.ts:15]"]) + d_c1() + steps +
            payloads(payload("Header excerpt, trimmed (from app/queries/app/cost/cost_period_over_period.sql:1-15)", hdr, "", "sql"),
                     payload("Status CASE, simplified: change_pct inlined (from the same file:146-160)", case, "", "sql")) +
            "<h4>One row through the pipeline</h4>" + J + '<p class="cite">from tests/test_findings/test_cost_period_over_period.py:104-115</p>' + cards +
            "<h4>Changing a vendored check instead</h4>" + bullets([
                "Edit app/queries/vendored/<domain>/<id>.sql in place. [tools/sync_queries.py:39-56]",
                "Run python tools/sync_queries.py --relock --item <task id>; commit fix and lock together. [tools/sync_queries.py:25-46]",
                "Record the defect in config/library_corrections.yml. [app/core/library_corrections.py:3-12]",
                "Then regenerate models and direct SQL, rebuild dashboards, run the gate. [tools/generate_models.py:4-7; tools/build_direct_sql.py:1-6; tools/gate.py:182-202]"]))
    return section("check", "4. A check end to end", body)


def s_model():
    objs = [
        ("tags.cost_unit", "window x allocation unit", "window_days, unit_id (tested)", "dbt/models/tags/cost_unit.sql; loader"),
        ("tags.cost_unit_tag", "unit x level x key x value", "window_days, unit_id, level, tag_key, tag_value (tested)", "dbt cost_unit_tag.sql; loader"),
        ("tags.cost_day", "day x workspace x SKU x flags x sig", "11 columns (inferred)", "dbt cost_day.sql; loader"),
        ("tags.cost_reconciliation", "window x usage_unit", "window_days, usage_unit (tested)", "dbt; loader adds unit_* [tools/load_direct_results.py:680-700]"),
        ("tags.perf_unit", "window x unit", "window_days, unit_id (tested)", "dbt perf_unit.sql; loader"),
        ("tags.perf_unit_tag", "unit x level x key x value", "5 columns (tested)", "dbt; loader, then apply_bill_to_perf [tools/load_direct_results.py:701-703]"),
        ("tags.query_tag_keys", "window x unit x keys x origin", "7 columns (inferred)", "dbt; loader"),
        ("tags.tag_workspace_history", "workspace x key x version", "workspace_id, tag_key, valid_from (tested)", "dbt; loader"),
        ("tags.tag_workspace", "workspace x key (current)", "workspace_id, tag_key (tested)", "dbt; loader"),
        ("tags.tag_entity", "tagged object x key", "entity_type, workspace_id, entity_id, tag_key (tested)", "dbt; loader trims keys [tools/load_direct_results.py:625-643]"),
        ("tags.tag_index", "key x value x source", "tag_key, tag_value, source (tested)", "dbt; loader"),
        ("tags.bill_tag_dates", "billed object x key", "entity_type, workspace_id, entity_id, tag_key (tested)", "dbt; loader trims keys"),
        ("dims.dim_workspace", "workspace ever seen", "workspace_id (inferred)", "dbt dim_workspace.sql; loader from names/dim_workspace.sql, env added [tools/load_direct_results.py:597-622]"),
        ("dims.dim_job", "job, latest SCD2 row", "workspace_id, job_id (tested)", "dbt; loader"),
        ("dims.dim_cluster", "cluster, latest row", "cluster_id (tested)", "dbt; loader"),
        ("dims.dim_warehouse", "warehouse, latest row", "warehouse_id (tested)", "dbt; loader"),
        ("dims.dim_pipeline", "pipeline, latest row", "workspace_id, pipeline_id (tested)", "dbt; loader"),
        ("dims.dim_notebook", "notebook", "workspace_id, notebook_id (inferred)", "dbt; loader"),
        ("dims.int_workspace_tag_hints", "workspace x canonical key", "workspace_id, canonical_key (tested)", "dbt only"),
        ("findings.f_<qid> (160)", "window x grain columns", "window_days + config/grains (tested by unique_grain)", "dbt generated model; loader [tools/load_direct_results.py:575-591]"),
        ("direct_export_meta", "one row per export", "none", "loader [tools/load_direct_results.py:812-846]"),
        ("direct_export_findings_meta", "one row per check", "query_id (inferred)", "loader [tools/load_direct_results.py:840-846]"),
    ]
    views = [
        ("tags.cost_alloc (ephemeral)", "billing.usage, list_prices(), attributed_usage, lakeflow.jobs, lakeflow.pipelines; usage_date < audit_today()", "one per (billing row, part) [dbt/models/tags/cost_alloc.sql:1-6,46]"),
        ("list_prices() macro", "system.billing.list_prices; USD only; end capped at next start", "one price per SKU, cloud, unit, instant [dbt/macros/list_prices.sql:10-37]"),
        ("app/direct_sql/tags/_cost_alloc.sql (CTE block)", "same five sources; pasted at __COST_ALLOC__", "one per (rid, part) [tools/export_direct_results.py:66-68]"),
        ("app/direct_sql/tags/_workspace_history.sql (CTE block)", "billing.usage; pasted at __WORKSPACE_HISTORY__", "workspace x key x version [tools/export_direct_results.py:63-65]"),
        ("dbt d_<qid> models (enabled=false)", "same check SQL, Databricks branch", "never built [tools/generate_direct_models.py:15-24]"),
    ]
    refs = [
        ("cost_unit", "cost_alloc", "dbt/models/tags/cost_unit.sql:19"), ("cost_unit_tag", "cost_alloc, tag_workspace_history", "dbt/models/tags/cost_unit_tag.sql:19,55"),
        ("cost_day", "cost_alloc", "dbt/models/tags/cost_day.sql:21"), ("cost_reconciliation", "cost_unit", "dbt/models/tags/cost_reconciliation.sql:54"),
        ("perf_unit_tag", "perf_unit, tag_workspace_history", "dbt/models/tags/perf_unit_tag.sql:83,93,172"), ("tag_workspace", "tag_workspace_history", "dbt/models/tags/tag_workspace.sql:14"),
        ("tag_entity", "tag_workspace", "dbt/models/tags/tag_entity.sql:44"), ("tag_index", "tag_workspace, tag_entity", "dbt/models/tags/tag_index.sql:59,69,126,163"),
        ("int_workspace_tag_hints", "tag_workspace", "dbt/models/dims/int_workspace_tag_hints.sql:108"), ("dim_workspace", "seed, int_workspace_tag_hints", "dbt/models/dims/dim_workspace.sql:122,126,132"),
    ]
    lin = (details("dbt ref() lineage (Graphviz)", lineage_svg() +
                   foot("grey = system source · teal = dbt model · dashed box = ephemeral · lavender = macro · dashed arrows = data moves",
                        "Finding models never ref() anything; only tags and dims chain; 9 more dim_workspace sources not drawn. [dbt/models/findings; dbt/models/dims/dim_workspace.sql]") +
                   table(["Child", "ref() parent", "Cite"], refs, mono=(0, 1, 2))))
    b = bullets([
        "One DuckDB file: 160 findings tables, 12 tags tables, dims (7 dbt, 6 direct). [tools/load_direct_results.py:38-77]",
        "No key is enforced; keys come from unique_grain tests, ROW_NUMBER, GROUP BY. [dbt/tests/generic/test_unique_grain.sql:12-34]",
        "f_<qid> grain = window_days + its config/grains columns. [tools/generate_models.py:504-507]",
        "int_workspace_tag_hints exists on the dbt road only. [app/direct_sql/names/; tools/load_direct_results.py:38-69]",
        "Reconciliation must match billing within $0.005 per window and usage_unit. [dbt/tests/tags_cost_reconciles.sql:6-9]",
        "Standalone cards: tags.bill_tag_dates, dims.dim_notebook, direct_export_meta, direct_export_findings_meta.",
    ])
    standalone = (exset(
        tcard("tags.bill_tag_dates", ["column", "key"], [("entity_type, workspace_id", "PK tested"), ("entity_id, tag_key", "PK tested")], "standalone: no key to others", "ok"),
        tcard("dims.dim_notebook", ["column", "key"], [("workspace_id, notebook_id", "PK inferred")], "standalone: no key to others", "ok"),
        tcard("direct_export_meta, direct_export_findings_meta", ["table", "key"], [("direct_export_meta", "none"), ("direct_export_findings_meta", "query_id inferred")], "standalone: loader metadata", "ok")))
    body = ("<h4>Cost allocation chain</h4>" + d_er_a() + "<h4>Tag lookup, performance, findings and dims</h4>" + d_er_b() + standalone + b +
            "<h4>Objects</h4>" + table(["Object", "Grain", "Key", "Written by"], objs, mono=(0,)) +
            "<h4>Views</h4>" + table(["View", "Built from", "Rows"], views, mono=(0,)) + lin)
    return section("data-model", "5. Data model", body)


def rule_sec(id_, title, line, diag, formula, fhead, bl, cards, flang="text"):
    return (f'<h3 id="{id_}">{e(title)}</h3>' + rule(line) + diag + payload(fhead, formula, "", flang) + bullets(bl) + cards)


def s_rules():
    K = "diamond = decision · amber = shape change · red = fail · dashed grey = excluded"
    out = []
    out.append(rule_sec("rule-price", "R1 List-price dollarization", "Dollar = usage_quantity x effective list price active at usage_end_time.",
        mini("R1", "diamond = join test · amber = shape change", "Unpriced usage stays quantity; it is never priced as $0.", r1),
        "list_rate  = CAST(pricing.effective_list.default AS DOUBLE)\nusd        = COALESCE(SUM(CASE WHEN list_rate IS NOT NULL THEN qty * list_rate END), 0)\nunpriced_q = COALESCE(SUM(CASE WHEN list_rate IS NULL THEN qty ELSE 0 END), 0)\n_disc twin = col * (1 - discount_pct)",
        "from dbt/models/tags/cost_alloc.sql:40; dbt/models/tags/cost_unit.sql:32-33", [
            "Join key: sku_name + cloud + usage_unit, price window by usage_end_time. [dbt/models/tags/cost_alloc.sql:38-45]",
            "Macro keeps USD rows; caps each end at next start. [dbt/macros/list_prices.sql:18-23,32,35]",
            "A dollar column always ends in a list-price name, e.g. est_*_usd_list. [app/core/pricing.py:5-8,26]",
            "discount_pct never changes a stored dollar; it adds `_disc` twins. [app/core/pricing.py:48-57]",
            "Cloud VM bill is out of scope; no system table holds it. [README.md:348-350]"],
        exset(ex("ok", "Typical", "tg_cl_a1, 30 DBU at $2", "usd 60", "from tests/fixtures/tagworld.py:24-29"),
              ex("edge", "Unpriced", "5 DBU on tg_SKU_NOPRICE", "unpriced_quantity 5, usd +0", "from tests/fixtures/tagworld.py:24-29"),
              ex("edge", "Price change", "expired + current price rows", "net_list_cost = qty x current, no fan-out", "from tests/test_findings/test_cost_priced.py:317-430"),
              ex("ok", "What-if", "est_usd_list 100.0, discount_pct 0.15", "est_usd_list_disc 85.0", "derived from app/core/pricing.py:48-57"))))
    out.append(rule_sec("rule-alloc", "R2 Cost allocation to tags, with untagged split", "Warehouse DBUs split per query-tag set; remainder is idle or unsplit.",
        mini("R2", "diamond = rule · amber = derived remainder", "Parts sum back to the bill; reconciliation proves it.", r2),
        "split_f    = CASE WHEN billed > 0 AND attributed > 0 THEN LEAST(attributed, billed) / billed ELSE 0 END\nquery part = usage_quantity * split_f * mix_dbus / attributed_dbus\nremainder  = usage_quantity * (1 - split_f)   -- warehouse_idle if attributed > 0, else warehouse_unsplit",
        "from dbt/models/tags/cost_alloc.sql:55-103", [
            "Job and pipeline tags come from their latest SCD2 row. [dbt/models/tags/cost_alloc.sql:76-83]",
            "Rollup takes the first real value: work, then compute, then workspace. [app/core/rollup.py:249-259]",
            "Untagged splits into other_tags (rename/merge) and no_tags (tag them). [app/core/rollup.py:719-745]",
            "Platform keys (vendor, creator, clustername, jobid...) never count as other tags. [app/core/tag_keys.py:21-27]",
            "One query's work tag never moves a whole warehouse unit. [app/core/rollup.py:728-737]"],
        exset(tcard("Typical split", ["day", "billed", "attr", "split_f", "parts"], [("tg_wh_a1 D(1)", "100", "70", "0.7", "q_fin 40, none 20, bi 10, idle 30")], "from tests/fixtures/tagworld.py:17-23; tests/test_tag_rollup_models.py:160-168"),
              tcard("Scaled", ["day", "billed", "attr", "split_f", "parts"], [("tg_wh_a2 D(1)", "40", "50", "1.0", "q_mkt 24, untagged 16, no idle")], "from tests/test_tag_rollup_models.py:364-388", "edge"),
              tcard("Unsplit", ["day", "billed", "attr", "split_f", "parts"], [("tg_wh_a1 D(3)", "50", "0", "0", "not split 50")], "from tests/test_tag_rollup_models.py:391-405", "edge"),
              ex("edge", "Untagged split", "units cl $10 (project on cluster), pf $20 (vendor only), wh $30 (one query's team tag), job $40 (own project tag)", ["other_tags $50 (cl + job)", "no_tags $50 (pf + wh)"], "from tests/test_api_rollup.py:244-264"))))
    out.append(rule_sec("rule-waste", "R3 Waste total without double counting", "Sum est_wasted_usd_list of flagged rows, once per resource, x (1 - discount).",
        mini("R3", "diamond = filter · dashed grey = excluded", "Five checks carry waste $; six are listed without a dollar.", r3),
        "waste_usd(check) = SUM over distinct wasteRowKey of flagged est_wasted_usd_list * (1 - discount_pct)\ntotal            = SUM waste_usd(check) where waste_usd is not null",
        "from web/src/components/waste_total.ts:48-96,187-215", [
            "Priced: warehouse idle minutes, idle node ratio, failed jobs, failed statements, idle serving endpoints. [web/src/components/tab_registry.ts:103-108]",
            "Resource key: workspace:warehouse, workspace:job, cluster, endpoint + served entity. [web/src/components/waste_total.ts:52-67]",
            "All-NULL values read \"unknown\", never $0. [web/src/components/waste_total.ts:73-96]",
            "Overlap note when idle-node and failed-job waste are both priced. [web/src/components/waste_total.ts:187-203]",
            "No monthly projection of waste. [tests/test_waste_contract.py:82-85]"],
        exset(ex("ok", "Typical", "two flagged rows, same ws:warehouse, $30 each", "$30, not $60", "derived from web/src/components/waste_total.ts:75-88"),
              ex("edge", "Discount", "$30, discount_pct 0.2", "$24", "derived from web/src/components/waste_total.ts:94-95"),
              ex("edge", "All NULL", "flagged rows, all est_wasted_usd_list NULL", '"unknown", not added', "derived from web/src/components/waste_total.ts:90-96"),
              ex("bad", "Not a waste item", "compute_warehouse_idle_gaps (whole warehouse spend)", "not a waste item", "from tests/test_waste_contract.py:72-79"))))
    out.append(rule_sec("rule-materiality", "R4 Materiality floor and severity cap", "At read time, admin grants and sub-floor rows become OK; reports cap at WARN.",
        mini("R4", "diamonds in CASE order", "The exported status column is never rewritten. [config/materiality.yml:5]", r4, gx=34),
        "min_eff = unit == 'USD' and window_days > 0 ? min * window_days / 30 : min\nstatus'  = admin hit -> OK | (CRIT/WARN and col < min_eff) -> OK | (CRIT and capped) -> WARN | status",
        "from app/core/materiality.py:131-168", [
            "USD floors scale by window/30; count, DBU, minute floors never scale. [app/core/materiality.py:131-139]",
            "Stale floor column means floor not applied, never an error. [app/core/materiality.py:121-128]",
            "Floors reload on file change; no re-export needed. [app/core/materiality.py:90-118]",
            "17 trend/chargeback checks never read CRITICAL. [app/core/materiality.py:49-73]",
            "Effective status drives counts, worst status, sort and money sums. [config/materiality.yml:2-5]"],
        exset(ex("ok", "Typical", "$50/30d floor, CRITICAL $30 at 30d", "OK, below_floor", "from tests/test_materiality.py:136-155"),
              ex("edge", "Shorter window", "same $30 row at 7d (floor $11.67)", "stays CRITICAL", "from tests/test_materiality.py:136-155"),
              ex("edge", "Count floor", "floor 20 events, 15 at 7d", "OK (not scaled)", "from tests/test_materiality.py:157-173"),
              ex("edge", "Cap", "cost_period_over_period CRITICAL", "WARN", "from app/core/materiality.py:52; tests/test_materiality.py:414-438"))))
    out.append(rule_sec("rule-rls", "R5 Row-level security", "A row shows only if one grant passes every configured column.",
        mini("R5", "red = fail closed", "Errors and unknown users see nothing.", r5),
        "visible(row) = EXISTS grant g: AND over columns c (g.c = '*' OR g.c = row_value(c))\nrow_value(workspace) = NULLIF(workspace_id, '0'); row_value(environment) = lower(env)",
        "from app/core/rls.py:114-141", [
            "Columns: workspace -> workspace_id, warehouse -> warehouse_id, environment -> env. [app/core/rls.py:25]",
            "NULL or account-level '0' rows need '*'. [app/core/rls.py:114-121]",
            "Filtered schemas: findings, dims, tags, usage, storage, warehouse; rows with no workspace need '*'. [app/core/rls.py:29]",
            "Reference tables (list prices, node types, usage/storage/warehouse *_info notes) stay unfiltered. [app/core/rls.py:32-38]",
            "Chargeback shares recomputed per user to sum to 100. [app/core/rls.py:34-45,305-311]",
            "Export mode files hold no bytes of other users' rows. [app/core/rls.py:382-418]"],
        exset(ex("ok", "Typical", "grants (1, dev), (2, prod); 6 rows", "2 rows: 1/dev, 2/prod (not 1/prod)", "from tests/test_rls.py:62-65"),
              ex("edge", "Wildcard and none", ["grant ('*','*','*')", "no grant"], ["all rows", "0 rows"], "from tests/test_rls.py:68-70"),
              ex("edge", "NULL and '0'", ["grant workspace 1; rows NULL and '0'", "same with '*'"], ["0", "2"], "from tests/test_rls.py:73-77"),
              ex("bad", "Load error", "entitlements load error", "no rows (fail closed)", "from tests/test_rls.py:88-96"))))
    out.append(rule_sec("rule-pop", "R6 Period over period", "Last N days vs N before; >= 25% WARN, >= 50% CRITICAL; $50 floor.",
        mini("R6", "diamonds in CASE order · amber = app step", "Coverage is account-wide, so no single workspace is withheld.", r6),
        "change_pct = (eff_current - eff_previous) / NULLIF(eff_previous, 0) * 100",
        "from app/queries/app/cost/cost_period_over_period.sql:138", [
            "Coverage uses the account's earliest usage_date, not the row's own. [app/queries/app/cost/cost_period_over_period.sql:68-74,146-147]",
            "A side with no rows is a real $0; unpriced side stays NULL. [app/queries/app/cost/cost_period_over_period.sql:122-131]",
            "price_basis over both windows: unpriced, free or priced. [app/queries/app/cost/cost_period_over_period.sql:139-145]",
            "Corrections netted: all record types summed. [app/queries/app/cost/cost_period_over_period.sql:63-67]",
            "More cards: see section 4."],
        exset(ex("edge", "Coverage", "account history shorter than 2 x window", "every row NOT_ASSESSED previous_window_not_covered", "from tests/test_findings/test_cost_period_over_period.py:180-197"),
              ex("edge", "Free SKU", "only FREE_USAGE SKUs", "price_basis free, real $0", "from tests/test_findings/test_cost_period_over_period.py:246-278"))))
    out.append(rule_sec("rule-query-cost", "R7 Query cost split", "Statement cost = its attributed DBUs at list, else its hour-share.",
        mini("R7", "diamond = decision · red = not assessed", "Canceled statements are shown, not counted as waste.", r7, gx=40),
        "w(s,h) = weight_ms * (same hour ? 1 : overlap_ms(s,h) / max(duration_ms, 1))\nrun_time_share waste = SUM_h hour_usd(h) * w(s,h) / SUM_s' w(s',h), over FAILED s\nfailed_cost_share_pct = waste * 100 / base_usd",
        "from app/queries/app/cost/cost_failed_statement_waste.sql:114-155", [
            "Weight: total_task_duration_ms, else execution_duration_ms, else 0. [app/queries/app/cost/cost_failed_statement_waste.sql:62-70]",
            "Serverless notebook/job statements without warehouse_id are out of scope. [app/queries/app/cost/cost_failed_statement_waste.sql:6-7]",
            "Failed with no attributed row costs $0. [app/queries/app/cost/cost_failed_statement_waste.sql:24-25]",
            "Hour split may overlap warehouse idle minutes slightly. [app/queries/app/cost/cost_failed_statement_waste.sql:49-51]"],
        exset(tcard("Failed statement cost", ["warehouse", "basis", "waste", "base", "share", "status"], [
            ("iw_wh_fail", "statement", "6.00", "8.50", "70.6%", "WARN"),
            ("iw_hf_wh", "run_time_share", "7.00", "12.00", "58.3%", "WARN"),
            ("iw_wh_fail_noattr", "none", "null", "null", "null", "NOT_ASSESSED no_billing_rows")],
            "from tests/test_findings/test_cost_failed_statement_waste.py:37-60. iw_hf_wh: hour 18 = 300 of 500 ms of $10 = $6; hour 19 = $1"))))
    out.append(rule_sec("rule-asof", "R8 As-of cutoff", "Every read stops before audit_today(); default export cuts at tomorrow.",
        "", "as_of = now + 1 day   when include_today\nas_of = now           otherwise", "from tools/export_direct_results.py:332-341", [
            "Check SQL current_date() is rewritten to audit_today() by pin_time. [app/core/translate.py:700-706]",
            "dbt test target pins 2026-09-21; dev uses var or current date. [dbt/macros/audit_time.sql:12-26]",
            "Export: cutoff = now + 1 day when include_today, else now. [tools/export_direct_results.py:332-341]",
            'UI shows "today partial" and "billing can lag a few hours". [web/src/App.tsx:177-184]'],
        exset(ex("ok", "Example", "run 2026-11-10T08:15Z, include_today true", "as_of_date 2026-11-11; 7-day window 2026-11-04..2026-11-10, last day partial", "illustrative, derived from tools/export_direct_results.py:332-341"))))
    return section("rules", "6. Key business rules", "".join(out))


def s_decisions():
    rows = [
        ("Export to a local DuckDB file; browsing never queries the warehouse", "No warehouse cost while reading; nothing written to Databricks", "README.md:15,21-22"),
        ("One price basis: effective list price; discount is a `_disc` twin", "No contract rates in system tables; one basis everywhere", "README.md:348-350; app/core/pricing.py:24-26,48-57"),
        ("Write each check once; generate dbt model and direct SQL", "One source for three runtimes; gate fails on drift", "tools/generate_models.py:1-12; tools/build_direct_sql.py:1-6; tools/gate.py:75-102"),
        ("Effective status at read time; exported status untouched", "Floors and admin groups change without re-export", "config/materiality.yml:2-9; app/core/materiality.py:142-178"),
        ("Five outcomes (incl. error); not assessed never reads as pass", "Empty result is not proof of health", "app/api/service.py:103-106,427-608; README.md:360-362"),
        ("One dollar split (tags.cost_alloc) feeds unit, unit-tag and day tables", "Totals agree; dbt test proves sum = bill", "dbt/models/tags/cost_alloc.sql:1-6; dbt/tests/tags_cost_reconciles.sql:6-9"),
        ("Refresh writes a new audit-<stamp>.duckdb and switches pointer", "Open files cannot be replaced on Windows; old data stays on failure", "app/api/refresh.py:48-69; README.md:80-81,100"),
        ("Query only distinct window lengths; alias the rest at load", "Fewer warehouse statements for short exports", "tools/export_direct_results.py:219-243; tools/load_direct_results.py:575-591"),
        ("Row-level access fails closed; export mode writes per-grant files", "Errors or unknown users never leak rows", "app/core/rls.py:268-284,382-418"),
        ("Commit the built UI with a source-hash test", "Users need no Node; stale builds fail a test", "README.md:44; web/vite.config.ts:11-32; tests/test_web_dist.py:21-39"),
    ]
    return section("decisions", "7. Key design decisions", table(["Decision", "Why", "Cite"], rows, mono=(2,)))


def s_components():
    rows = [
        ("tools/first_run.py", "Export then load as subprocesses; stops at first failure", "tools/first_run.py:162-194"),
        ("tools/export_direct_results.py", "Runs app/direct_sql on a warehouse; writes parquet, manifest, report", "tools/export_direct_results.py:1179-1340,1457"),
        ("tools/snapshot.py connect()", "SQL connector: token or OAuth M2M, query tags, UTC", "tools/snapshot.py:485-555"),
        ("tools/load_direct_results.py", "Builds DuckDB from an export folder; atomic replace", "tools/load_direct_results.py:471,516-547,908"),
        ("app/api/__main__.py", "Host/port, cache warm-up, uvicorn start", "app/api/__main__.py:14-25"),
        ("app/api/app.py", "Routes, principal middleware, caches, static page", "app/api/app.py:188-206,350-1496"),
        ("app/api/service.py", "Per-check outcome; rows to JSON-safe records", "app/api/service.py:427-608,996,1053"),
        ("app/api/refresh.py", "Background export + load into a new db", "app/api/refresh.py:31-95"),
        ("app/core/data.py", "Read-only store, db resolution, read_finding, read_data_levels", "app/core/data.py:203-216,304-329,822-929,1982-2099"),
        ("app/core/usage.py", "Table usage class; complexity and usage index (0-100, log scale) and signal per workspace, catalog, schema", "app/core/usage.py:13-19,30-33,67-104"),
        ("app/core/config.py", "Load and validate settings with local overlay", "app/core/config.py:374-506,534-573"),
        ("app/core/registry.py", "Check specs; is_finding and executable flags", "app/core/registry.py:179-246,314"),
        ("app/core/materiality.py", "Effective status: admin, floor, WARN cap", "app/core/materiality.py:142-178"),
        ("app/core/pricing.py", "`_disc` what-if twins", "app/core/pricing.py:48-57"),
        ("app/core/rollup.py", "Tag rollup tree, by-value, untagged split", "app/core/rollup.py:249-259,326-352,719-745"),
        ("app/core/rls.py", "Grants, modes, scoped views, split files", "app/core/rls.py:129-141,451-456,510-608"),
        ("app/core/translate.py", "25 ordered Databricks-to-DuckDB rewrite rules; clock pinning", "app/core/translate.py:700-710,731-880"),
        ("tools/generate_models.py, generate_direct_models.py, build_direct_sql.py", "Generate f_*, d_* and app/direct_sql from check SQL", "tools/generate_models.py:1-12; tools/build_direct_sql.py:1-6"),
        ("dbt/models/tags/cost_alloc.sql", "The one dollar split into tag-able parts", "dbt/models/tags/cost_alloc.sql:1-128"),
        ("web/src/api.ts, components/hooks.ts", "Fetch layer and per-check data hooks", "web/src/api.ts:10-37,111-123; web/src/components/hooks.ts:85-103"),
        ("web/src/components/tab_registry.ts, labels.ts", "Tab placement, waste items, titles (also read by dashboard build)", "web/src/components/tab_registry.ts:51-117; labels.ts:15"),
        ("web/src/components/waste_total.ts", "Possible-waste headline, once per resource", "web/src/components/waste_total.ts:48-221"),
        ("tools/deploy_app.py, tools/app_start.py", "Stage, sync, deploy the App; unpack DuckDB at start", "tools/deploy_app.py:127-216; tools/app_start.py:21-39"),
        ("tools/build_databricks_dashboards.py", "14 dashboards + snapshot notebook, byte-identical rebuild", "tools/build_databricks_dashboards.py:8-9,1441-1477"),
        ("tools/gate.py", "Lint, sync, generators, web, dbt, pytest", "tools/gate.py:182-213"),
    ]
    return section("components", "8. Components", table(["Name", "Does", "Where"], rows, mono=(0, 2)))


def s_config():
    st = [
        ("discount_pct", "0.0", "What-if discount on list price, in [0, 1)", "page refresh [app/core/config.py:387-388]"),
        ("default_window", "30", "Days first_run exports; window the app opens", "next export / page [config/settings.yml:18]"),
        ("include_today", "true", "Count today's partial day", "next export [config/settings.yml:22]"),
        ("mandatory_tag_keys", "[cost_center, domain, environment] (code default uses env)", "Up to 5 keys every resource should carry", "page refresh [config/settings.yml:36; app/core/config.py:206]"),
        ("tag_share_floor", "0.6", "Top value share to infer a workspace tag", "next export [config/settings.yml:62]"),
        ("tag_coverage_floor", "0.5", "Key coverage to infer a workspace tag", "next export [config/settings.yml:61]"),
        ("export_tag_keys", "all tags", "Keep only these tags in the export", "next export [config/settings.yml:42]"),
        ("load.memory_limit", "1GB", "DuckDB memory cap in load and app", "next load / restart [config/settings.yml:53]"),
        ("load.threads", "4", "DuckDB threads", "next load / restart [config/settings.yml:54]"),
        ("load.top_tag_keys", "10", "Keep top tag keys by DBUs", "next load [config/settings.yml:55]"),
        ("ui.max_rows", "500", "Default rows a table loads at once", "page refresh [config/settings.yml:27]"),
        ("privacy.mask_user_identities", "false", "Mask user emails/names in export and screen", "next export/load [config/settings.yml:80]"),
        ("query_tags", "{cost_center: lakehouse_health, domain: lakehouse_health}", "Session tags on the audit's own statements", "next export; rebuild dashboards [config/settings.yml:92-94]"),
        ("admin_groups", "[]", "Grantees treated as admins in broad-grants check", "page refresh [config/settings.yml:63-66]"),
        ("sources", "{}", "Point system tables at other locations", "next export [config/settings.yml:117-119]"),
        ("config/thresholds.yml", "header defaults", "Per-check :param overrides, baked into SQL", "after build_direct_sql [README.md:389-392]"),
        ("config/materiality.yml", "25 floors", "Per-check floors at read time", "page refresh [config/materiality.yml:19-147]"),
        ("app.yml run_as", "user", "Query as token owner or app service principal", "next deploy [config/app.yml:7-10]"),
        ("app.yml rls.enabled / apply / columns / table", "false / query / [workspace] / main.governance.audit_entitlements", "Row-level access", "next export / deploy [config/app.yml:14-21]"),
        ("export --max-rows", "50,000 (Refresh passes 500,000)", "Rows kept per check run", "next export [tools/export_direct_results.py:183; app/api/refresh.py:54]"),
        ("export --threads", "4", "Parallel warehouse statements", "next export [tools/export_direct_results.py:185]"),
    ]
    env = table(["Name", "Default", "Effect"], [
        ("AUDIT_DB", "unset", "db the app opens first"), ("AUDIT_DATA_DIR", "OS data folder", "data folder, outside repo"),
        ("AUDIT_CONFIG_DIR", "config/", "config folder"), ("AUDIT_API_HOST", "127.0.0.1 (0.0.0.0 on Apps)", "bind host"),
        ("AUDIT_API_PORT", "8000 (DATABRICKS_APP_PORT on Apps)", "bind port"), ("AUDIT_SNAPSHOT_DIR", "snapshot/", "dbt snapshot folder"),
        ("AUDIT_DBX_CATALOG", "system", "catalog holding system tables"), ("DATABRICKS_APP_PORT", "set by Apps", "marks App mode")], mono=(0, 1))
    sec = table(["Secret name (values never shown)", "Meaning"], [
        ("DATABRICKS_SERVER_HOSTNAME", "host, no https://"), ("DATABRICKS_HTTP_PATH", "starts with /"),
        ("DATABRICKS_TOKEN", "PAT with SELECT on system schemas; redacted in errors; no --token flag"),
        ("DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET", "set by Apps for run_as app")], mono=(0,))
    body = (d_cfg() + bullets([
        "Put changes in config/settings.local.yml; a block replaces the whole block. [config/settings.yml:1-5; app/core/config.py:567]",
        "Config folder = AUDIT_CONFIG_DIR or config/, re-read every call. [app/core/config.py:127-129]",
        "Unknown key or bad type -> SettingsError; API 503; /api/status names key and line. [app/core/config.py:101-119; app/api/app.py:222-232,348-376]",
        "Missing settings.yml is seeded from DEFAULT_SETTINGS. [app/core/config.py:547-557]",
        "Brand keys: brand.name, brand.url, brand.contact_name, brand.contact_email. [config/settings.yml]"]) +
        table(["Setting", "Default", "Purpose", "Takes effect"], st, mono=(0, 1)) +
        details("Environment variables and secret names", env + sec + '<p class="cite">[app/core/data.py:207-221; app/core/datasource.py:29-31; app/core/config.py:127-129; app/api/__main__.py:15-17; tools/export_direct_results.py:187-200; .env.example:1-20; tools/snapshot.py:166-177,526-530]</p>'))
    return section("config", "9. Configuration", body)


def s_deploy():
    envs = table(["Environment", "Runs where", "Database", "Who can read", "Notes"], [
        ("Local / run anywhere", "engineer's machine or a server", "<data dir>/audit.duckdb", "anyone reaching the bind address", "no login; keep host 127.0.0.1 [README.md:508]"),
        ("Databricks App", "Databricks Apps, Python 3.11, Linux x86_64", "app disk audit-<stamp>.duckdb, wiped on restart", "users with Can use", "rls optional [tools/deploy_app.py:44-46; docs/databricks-app.md:131]"),
        ("AI/BI dashboards", "a workspace + snapshot schema", "<catalog>.databricks_audit_dashboard", "dashboard viewers", "published with embed credentials [databricks_dashboards/README.md:65-83]"),
        ("dbt dev (older road)", "developer machine", "data/db_audit.duckdb", "developer", "[tools/dbt_run.py:369-373]"),
        ("Test", "developer machine", "tests/db_audit_test.duckdb", "pytest", "[tests/conftest.py:65]"),
        ("CI", "Not defined in source: confirm with owner (no CI config in repo)", "", "", ""),
    ], mono=(0,))
    cmds = [
        payload("Set up once", "git clone <repo URL>\ncd databricks-audit-app\npython -m venv .venv\n.venv\\Scripts\\activate          # macOS/Linux: source .venv/bin/activate\npip install -r requirements.txt\ncopy .env.example .env          # macOS/Linux: cp .env.example .env", "", "text", "README.md:48-55"),
        payload("Run", "python tools/first_run.py       # last 30 days; --windows 90 for 90\npython -m app.api               # then open http://127.0.0.1:8000/", "", "text", "README.md:67-72"),
        payload("By hand", "python tools/export_direct_results.py --out <folder outside repo>\npython tools/load_direct_results.py <folder> [--db PATH]", "", "text", "README.md:105-124,174-175"),
        payload("dbt dev road", "python tools/snapshot.py --out snapshot\npython tools/dbt_run.py build --target dev", "", "text", "tools/snapshot.py:8-10; tools/dbt_run.py:4"),
        payload("Databricks App", "python tools/deploy_app.py\n# update: git pull, then python tools/deploy_app.py again, then click Get", "", "text", "docs/databricks-app.md:51-55,93-97"),
        payload("Dashboards", "python tools/build_databricks_dashboards.py --catalog <catalog> --out-dir <folder outside repo>\npython tools/build_direct_dashboards.py [--query-tags k=v,...]\n# import each .lvdash.json, pick a warehouse, publish; run the snapshot notebook", "", "text", "tools/build_direct_dashboards.py:8; databricks_dashboards/README.md:47-83"),
    ]
    perms = table(["Object", "Grant", "Needed for"], [
        ("catalog system", "USE CATALOG", "every check [tools/deploy_app.py:95]"),
        ("schemas access, billing, compute, lakeflow, query, serving, storage", "USE SCHEMA, SELECT", "core checks [tools/deploy_app.py:41-43,96]"),
        ("schemas data_classification, ai_gateway", "USE SCHEMA, SELECT, where present", "sensitive data, AI gateway checks [README.md:37]"),
        ("information_schema", "none (readable by everyone)", "UC tags [tools/deploy_app.py:41]"),
        ("entitlements table (rls on)", "USE CATALOG, USE SCHEMA, SELECT", "row-level access [tools/deploy_app.py:99-103]"),
        ("SQL warehouse (run_as app)", "CAN_USE app resource", "export [tools/deploy_app.py:79-84]"),
    ], mono=(0,))
    grant = "GRANT USE CATALOG ON CATALOG system TO `<service principal id>`;\nGRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO `<service principal id>`;\n-- one line per schema in SYSTEM_SCHEMAS"
    status = 'GET http://127.0.0.1:8000/api/status\n{"state": "ready", "settings_error": null, "database": {"state": "ready", "db_path": "<data dir>/audit.duckdb"}, "brand": {"name": "Crosshire", "...": "..."}}'
    body = envs + d_dep() + \
        "<h4>Prerequisites</h4>" + bullets([
            "Python 3.11 or newer (tested 3.12). [README.md:31]",
            "Unity Catalog with system tables; a SQL warehouse HTTP path. [README.md:32-33]",
            "Token user can SELECT the system schemas listed below. [README.md:34-37]",
            "App deploy: Databricks CLI and permission to create apps. [docs/databricks-app.md:17-24]",
            "Node and git only to rebuild the UI. [README.md:44,753]",
            "Secrets by name only: DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN in git-ignored .env. [README.md:57-63]"]) + \
        "<h4>Commands</h4>" + "".join(cmds) + "<h4>Access</h4>" + bullets([
            "Local: http://127.0.0.1:8000/; change port with AUDIT_API_PORT. [app/api/__main__.py:16-17; README.md:717]",
            "App: URL printed by deploy_app.py, or Compute > Apps. [tools/deploy_app.py:214-216; docs/databricks-app.md:86-91]",
            "Exact App URL: Not defined in source: confirm with owner.",
            "Share the App via its Permissions > Can use. [docs/databricks-app.md:86-91]"]) + \
        "<h4>Permissions on system schemas</h4>" + perms + bullets(["A check on an unreadable schema fails alone; others still run. [README.md:39-42]"]) + \
        payload("GRANT SQL printed for run_as app (illustrative; principal placeholder)", grant, "", "sql", "tools/deploy_app.py:95-97") + \
        "<h4>Health checks</h4>" + bullets([
            'App start prints "database: <path>" and the URL. [app/api/__main__.py:18-20]',
            "GET /api/status: ready, no_database, build_incomplete or settings_invalid. [app/api/app.py:350-378; app/core/data.py:383-409]",
            "results/<stamp>/report.md lists failed checks. [README.md:706]",
            "/api/coverage lists truncated_query_ids on a direct export. [app/api/app.py:659,707]",
            "/api/rollup reconciliation.reconciled: rollup within $0.005 of the bill. [app/core/rollup.py:700,805]",
            "Gate: python tools/gate.py --quick (needs the test DB), or full. [tools/gate.py:4-7; README.md:742-747]"]) + \
        payload("Quick verification (shape from app/api/app.py:373-378; values illustrative)", status, "", "json")
    return section("deploy", "10. Deploy & access", body)


def s_tests():
    cmds = table(["Goal", "Command", "Cite"], [
        ("Install dev deps", "pip install -r requirements-dev.txt", "README.md:742"),
        ("Full gate (~30 min)", "python tools/gate.py", "README.md:743"),
        ("Quick gate (needs built test DB)", "python tools/gate.py --quick", "README.md:744-747"),
        ("pytest only", "python -m pytest -q", "tools/gate.py:121"),
        ("Web checks", "cd web && npm ci && npm run typecheck && npm test", "README.md:748"),
        ("Rebuild UI", "cd web && npm ci && npm run build (commit app/web/dist)", "README.md:750-751"),
        ("UI dev server", "npm run dev (proxies /api to AUDIT_API_PORT)", "web/vite.config.ts:38-39"),
        ("New check models", "python tools/generate_models.py --only <id>; generate_direct_models.py --only <id>; build_direct_sql.py", "tools/generate_models.py:5; tools/generate_direct_models.py:6; tools/build_direct_sql.py:1-3"),
        ("Re-vendor library", "python tools/sync_queries.py --commit <sha>", "tools/sync_queries.py:13-37"),
    ], mono=(1, 2))
    gate = table(["Step", "Mode", "Command"], [
        ("lint_headers", "quick", "python tools/lint_headers.py"), ("sync_queries", "quick", "sync_queries.py --check"),
        ("generate_models", "quick", "generate_models.py --check"), ("generate_direct_models", "quick", "generate_direct_models.py --check"),
        ("build_direct_sql", "quick", "build_direct_sql.py --check"), ("web", "quick (if web/node_modules)", "npm typecheck + test"),
        ("pytest", "both", "python -m pytest -q"), ("build_fixtures", "full", "tests/fixtures/build_fixtures.py"),
        ("dbt_build_test", "full", "dbt_run.py build --target test"), ("fixture_coverage", "full", "fixture_coverage.py --check")], mono=(0, 2))
    body = d_t1() + bullets([
        "54 tests/test_*.py modules plus 75 per-check files in tests/test_findings/. [directory listing; pytest.ini:1-3]",
        "Fixed fixture instant 2026-09-21T12:00:00. [tests/conftest.py:68-69]",
        "conftest copies config to a tmp AUDIT_CONFIG_DIR; repo config never written. [tests/conftest.py:145-151]",
        "Stale DB rebuilt only with AUDIT_BUILD_FIXTURES=1; else a warning, tests go on. [tests/conftest.py:10-16,262-267]",
        "Generated-file tests: dashboards, direct SQL, web dist hash must match rebuild. [tests/test_databricks_dashboards.py:294; tests/test_build_direct_sql.py:19; tests/test_web_dist.py:36-39]",
        "The demo account checks hand-worked answers in a tmp dbt build. [tests/test_fake_account.py:24-41]"]) + cmds + "<h4>Gate steps</h4>" + gate + \
        '<p class="cite">[tools/gate.py:56-170,182-202]</p>'
    return section("tests", "11. Tests & dev commands", body)


def s_risks():
    r = [
        "run_as user stores the PAT in synced app.yaml; folder readers can read it. [tools/deploy_app.py:148-149; docs/databricks-app.md:129-130]",
        "App has no login; a non-local AUDIT_API_HOST exposes all data. [README.md:508]",
        "Row-level access trusts X-Forwarded-Email; safe only behind the Apps proxy (unclear: verify direct reachability). [app/api/app.py:188-203]",
        "Export folder on the App disk is unfiltered even with rls on. [docs/databricks-app.md:122-123]",
        "101 of 163 check files say confidence: needs_confirmation (not run live). [app/queries/app/cost/cost_period_over_period.sql:15-16]",
        "Row cap truncates big checks; their totals are a lower bound. [tools/export_direct_results.py:183,397-420]",
        "Dollars are list price only; contract rates and cloud VM cost are absent. [README.md:348-350]",
        "Today's partial day is in by default; billing lags a few hours. [config/settings.yml:22; web/src/App.tsx:177-184]",
        "App runtime uses pre-installed FastAPI 0.115, pandas 2.2, connector 3.4, not requirements pins. [docs/databricks-app.md:134-135; requirements.txt:1-9; tools/deploy_app.py:113-124]",
        "App disk wiped on restart; Medium app has 2 CPUs and 6 GB. [docs/databricks-app.md:131-133]",
    ]
    return section("risks", "12. Risks and limitations", "<ol>" + "".join(f"<li>{txt(x)}</li>" for x in r) + "</ol>")


def s_other():
    f = [
        "Row cap differs by path · first_run 50,000, Refresh 500,000 · one setting; else truncation and totals differ. [tools/export_direct_results.py:183; app/api/refresh.py:54]",
        "Caveats say \"current day is excluded\" · default include_today counts today · reword; else users misread totals. [config/settings.yml:22; tools/export_direct_results.py:332-341; app/queries/app/cost/cost_period_over_period.sql:65-67]",
        "mandatory_tag_keys default differs · code says env, file says environment · align; low impact, avoids reseed confusion. [app/core/config.py:206; config/settings.yml:36; app/core/tag_compliance.py:41; app/core/config.py:547-557]",
        "dbt_target has no reader · validated, never read · remove or wire it; else a dead setting misleads. [app/core/config.py:16,189,389-390]",
        "Stale counts in docstrings · say 113 and 109 (actual 160); build_fixtures says 47 sources (ddl has 49) · drop the numbers. [tools/generate_models.py:4; tools/fixture_coverage.py:9; tests/fixtures/build_fixtures.py:11-13]",
        "Endpoints without tests · 14 of 32 /api routes never called by a test · one smoke test per route; else regressions ship unnoticed. Also no reader test: /api/activity, /api/people, /api/warehouses, /api/warehouse, /api/workspace_tags, /api/guide/sql/{query_id}. [app/api/app.py:866,919,929,940,821,737]",
        "Restricted users see no table sizes or lineage · storage.table_detail has no workspace, so rows need a grant to every workspace · fill workspace_id from the catalog's home workspace; else sizes read empty for them. [app/core/rls.py:1-9,29]",
        "storage.table_detail_info is shown to every user · it holds account-wide table and failure counts · drop it from REFERENCE if that leaks; else /api/table_sizes shows not exported to restricted users. [app/core/rls.py:32-38]",
        "Waste headline sum has no unit test · only column-contract tests exist · test once-per-resource, NULL, discount; else it can regress silently. [web/src/components/waste_total.ts:48-221; tests/test_waste_contract.py:54-88]",
    ]
    return section("other-findings", "13. Other findings (verified in code)", "<ul>" + "".join(f"<li>{txt(x)}</li>" for x in f) + "</ul>")


TOC = [("orientation", "1 Orientation"), ("architecture", "2 Architecture"), ("flow-first-run", "3.1 First run"), ("flow-api", "3.2 API request"),
       ("flow-refresh", "3.3 Refresh"), ("flow-app", "3.4 Databricks App"), ("check", "4 A check end to end"), ("data-model", "5 Data model"),
       ("rules", "6 Business rules"), ("decisions", "7 Design decisions"), ("components", "8 Components"), ("config", "9 Configuration"),
       ("deploy", "10 Deploy & access"), ("tests", "11 Tests & dev"), ("risks", "12 Risks"), ("other-findings", "13 Other findings")]


def build():
    toc = "".join(f'<a class="chip" href="#{i}">{e(t)}</a>' for i, t in TOC)
    chips = "".join(f'<span class="chip">{e(c)}</span>' for c in ["commit 0425164", "Python 3.11+", "duckdb 1.5.1", "fastapi 0.138.2", "React 18.3.1 + Vite"])
    body = f"""
<div class="eyebrow">TECHNICAL VIEW · commit 0425164</div>
<h1>Crosshire: technical view</h1>
<p class="deck">Read-only audit of a Databricks account: 160 SQL checks on system.* tables. <span class="cite">[README.md:1-6]</span></p>
<div class="chips">{chips}</div><p class="cite">[README.md:31; requirements.txt:1,6; web/package.json:11-14]</p>
<nav class="toc" aria-label="Contents"><div class="eyebrow">Contents</div><div class="chips">{toc}</div></nav>
{s_orientation()}{s_arch()}{s_flows()}{s_check()}{s_model()}{s_rules()}{s_decisions()}{s_components()}{s_config()}{s_deploy()}{s_tests()}{s_risks()}{s_other()}
"""
    OUT.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                   f'<title>Crosshire technical view</title><style>{CSS}</style></head><body><main>{body}</main></body></html>', encoding="utf-8")


if __name__ == "__main__":
    build()
    print(OUT, OUT.stat().st_size)
    for w in WARN:
        print("WARN", w)
