"""Builds docs/overview.html (functional view). Stdlib only. Run: python docs/build_overview.py"""
import html
import pathlib

OUT = pathlib.Path(__file__).with_name("overview.html")
e = html.escape

CSS = r"""
:root { --ground:#F6F6F3; --card:#FFFFFF; --line:#D9DBDF; --ink:#17191D; --muted:#5B6068; --soft:#EEEFEC;
        --amber:#FCEBC8; --amber-ink:#8A5A00; --green:#1E6B43; --blue:#1F5FA8;
        --lav:#E7E2F5; --lav-ink:#6B4FA8; --red:#A12D2D; --accent:#1F5FA8; }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--ground); color:var(--ink); font:15px/1.55 "IBM Plex Sans","Segoe UI",Arial,sans-serif; }
main { max-width:1000px; margin:0 auto; padding:32px 0 56px; }
section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:20px 16px; margin:18px 0; }
h1 { font-size:30px; font-weight:600; margin:4px 0 6px; } h2 { font-size:20px; font-weight:600; margin:0 0 8px; }
h3 { font-size:15px; margin:0 0 4px; }
ul { margin:6px 0; padding-left:22px; } li { margin:3px 0; }
code, pre, .mono, .key, .cap, th, .eyebrow, .chip, .cite { font-family:"IBM Plex Mono",Consolas,monospace; }
code { font-size:13px; background:var(--soft); border:1px solid var(--line); border-radius:4px; padding:0 4px; white-space:nowrap; }
.eyebrow { font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }
.deck { margin:0 0 10px; font-size:16px; }
.key, .cap { font-size:12px; color:var(--muted); margin:8px 0 0; } .cap { color:var(--ink); }
.cite { font-size:11px; color:var(--muted); margin:4px 0 0; }
table { width:100%; border-collapse:collapse; margin:10px 0; font-size:14px; }
th, td { text-align:left; vertical-align:top; padding:7px 10px; border:1px solid var(--line); }
th { font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); background:var(--soft); }
.tblwrap { overflow-x:auto; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
.chip { font-size:12px; border:1px solid var(--line); border-radius:999px; padding:2px 10px; background:var(--card); color:var(--ink); text-decoration:none; }
a.chip:hover { border-color:var(--ink); }
a.chip:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }
nav.toc { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin:18px 0; }
nav.toc .eyebrow { margin-bottom:2px; }
details { margin-top:10px; } summary { cursor:pointer; font-weight:600; }
details .cl { font-family:"IBM Plex Mono",Consolas,monospace; font-size:12px; }
details .cl li { margin:6px 0; }

/* map */
.map-wrap { overflow-x:auto; margin-top:10px; }
.map { position:relative; margin:0 auto; font-size:13px; line-height:1.35; }
.map svg { position:absolute; left:0; top:0; overflow:visible; }
.map .b { position:absolute; background:var(--card); border:1px solid var(--ink); border-radius:8px; padding:8px 10px; }
.map .b.data { background:var(--soft); border-color:#B9BCC2; }
.map .b.shape { background:var(--amber); border-color:var(--amber-ink); }
.map .b.tbc { background:var(--soft); border:1px dashed #8A9099; color:var(--muted); }
.map .b .t { display:block; font-weight:600; }
.map .b .tag { font-family:"IBM Plex Mono",Consolas,monospace; font-size:10px; letter-spacing:.08em; color:var(--muted); display:block; }
.map .b ul { margin:4px 0 6px; padding-left:16px; font-size:12px; }
.map .b li { margin:1px 0; }
.io { display:grid; grid-template-columns:34px 1fr; gap:2px 6px; font-size:11.5px; border-top:1px dashed rgba(23,25,29,.25); padding-top:5px; }
.io .l { font-family:"IBM Plex Mono",Consolas,monospace; font-size:10px; font-weight:600; letter-spacing:.06em; border-radius:3px; text-align:center; height:16px; line-height:16px; }
.io .l.in { background:var(--card); border:1px solid var(--blue); color:var(--blue); }
.io .l.out { background:var(--green); color:#fff; }
.io code { font-size:11px; padding:0 3px; background:rgba(255,255,255,.7); white-space:normal; }
.map .num { position:absolute; width:20px; height:20px; border-radius:50%; background:var(--ink); color:#fff; font:600 11px/20px "IBM Plex Mono",Consolas,monospace; text-align:center; transform:translate(-50%,-50%); }
.map .lbl { position:absolute; font-family:"IBM Plex Mono",Consolas,monospace; font-size:11px; color:var(--muted); white-space:nowrap; transform:translate(-50%,-50%); background:var(--card); padding:0 4px; }
.map .lane { position:absolute; left:0; background:var(--soft); border-top:1px solid var(--card); }
.map .lane span { position:absolute; left:10px; top:8px; font-family:"IBM Plex Mono",Consolas,monospace; font-size:11px; letter-spacing:.08em; color:var(--muted); }
.map .b.c { display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; }
.map .b.c .sub { font-size:12px; color:var(--muted); }
.map .b.actor { border-radius:999px; } .map .b.entry { border-width:2px; }
.map .b.decision { background:transparent; border:0; padding:0 22px; font-size:12.5px; }
.map .grp { position:absolute; border:1px dashed var(--lav-ink); border-radius:10px; }
.map .grp span { position:absolute; left:10px; top:6px; font-family:"IBM Plex Mono",Consolas,monospace; font-size:11px; letter-spacing:.04em; color:var(--lav-ink); white-space:nowrap; }
.map polyline.flow { animation:flow 1.1s linear infinite; }
@keyframes flow { to { stroke-dashoffset:-18; } }
@media (prefers-reduced-motion: reduce) { .map polyline.flow { animation:none; } }

/* step table */
.steps td:first-child { width:34px; text-align:center; }
.steps .n { display:inline-block; width:20px; height:20px; border-radius:50%; background:var(--ink); color:#fff; font:600 11px/20px "IBM Plex Mono",Consolas,monospace; text-align:center; }
.steps td.v { font-family:"IBM Plex Mono",Consolas,monospace; font-size:12.5px; }

/* tiles */
.tiles { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:10px; }
.tile { border:1px solid var(--line); border-radius:8px; padding:10px 12px; background:var(--card); min-width:0; }
.tile .eyebrow { font-size:11px; }
.tile p { margin:2px 0 8px; font-size:13.5px; }
.demo { display:inline-block; font-family:"IBM Plex Mono",Consolas,monospace; font-size:12px; border:1px solid var(--line); background:var(--soft); border-radius:4px; padding:2px 6px; }
.demo.red { background:#F6DEDE; border-color:var(--red); color:var(--red); }
.demo.amber { background:var(--amber); border-color:var(--amber-ink); color:var(--amber-ink); }
@media (max-width:820px) { .tiles { grid-template-columns:1fr; } section { padding:16px; } }

/* example card */
.card { border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-top:10px; }
.card .h { padding:6px 10px; background:var(--soft); border-bottom:1px solid var(--line); font-family:"IBM Plex Mono",Consolas,monospace; font-size:11.5px; }
.card .h .dir { font-weight:600; color:var(--green); margin-right:8px; }
.card table { margin:0; } .card th, .card td { border-left:0; border-right:0; }
.card .cite { padding:6px 10px; border-top:1px solid var(--line); margin:0; }
td.crit { color:var(--red); font-weight:600; } td.warn { color:var(--amber-ink); font-weight:600; }
@media print {
  body { background:#fff; } main { padding:0; max-width:none; }
  section { border:0; padding:0; break-inside:avoid-page; }
  .map polyline.flow { animation:none; }
  nav.toc a { text-decoration:none; }
}
"""

HEAD = """<marker id="h" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#17191D"/></marker>"""


def box(cls, l, t, w, h, title, tag="", sub=""):
    inner = (f'<span class="tag">{e(tag)}</span>' if tag else "")
    inner += f'<span class="t">{e(title)}</span>'
    if sub:
        inner += f'<span class="sub">{e(sub)}</span>'
    return f'<div class="b c {cls}" style="left:{l}px;top:{t}px;width:{w}px;height:{h}px">{inner}</div>'


def arrow(pts, dashed=False):
    p = " ".join(f"{x},{y}" for x, y in pts)
    d = ' stroke-dasharray="5 4"' if dashed else ""
    c = "flow" if dashed else ""
    return (f'<polyline class="{c}" points="{p}" fill="none" stroke="#17191D" stroke-width="1.5" '
            f'stroke-linejoin="round"{d} marker-end="url(#h)"/>')


def num(x, y, n):
    return f'<span class="num" style="left:{x}px;top:{y}px">{n}</span>'


def lbl(x, y, text):
    return f'<span class="lbl" style="left:{x}px;top:{y}px">{e(text)}</span>'


_n = [0]


def mapdiv(w, h, boxes, arrows, extra="", back=""):
    _n[0] += 1
    mid = f"h{_n[0]}"
    arrows = [a.replace("url(#h)", f"url(#{mid})") for a in arrows]
    return (f'<div class="map-wrap"><div class="map" style="width:{w}px;height:{h}px">{back}'
            f'<svg width="{w}" height="{h}" aria-hidden="true"><defs>{HEAD.replace('id="h"', f'id="{mid}"')}</defs>{"".join(arrows)}</svg>'
            f'{"".join(boxes)}{extra}</div></div>')


def foot(key, cap):
    return f'<p class="key">{e(key)}</p><p class="cap">{e(cap)}</p>'


def bullets(items):
    return "<ul>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>"


def table(head, rows, cls="", mono_cols=()):
    h = "".join(f"<th>{e(c)}</th>" for c in head)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>"
    return f'<div class="tblwrap"><table class="{cls}"><tr>{h}</tr>{body}</table></div>'


def section(id_, title, body):
    return f'<section id="{id_}"><h2>{e(title)}</h2>{body}</section>'


# ---------------------------------------------------------------- 1. L0
def l0():
    b = [
        box("", 190, 10, 160, 90, "Databricks account", "SYSTEM", "usage, bills, jobs, access records"),
        box("data", 650, 10, 160, 90, "AI/BI dashboards", "REPORT", "inside Databricks"),
        box("actor", 0, 200, 120, 60, "Platform lead"),
        box("entry", 190, 180, 160, 100, "Crosshire", "APP", "160 read-only checks"),
        box("data", 430, 180, 140, 100, "Private results file", "DATA", "on your machine or app"),
        box("data", 650, 180, 140, 100, "Audit pages", "REPORT", "money, waste, setup"),
        box("actor", 840, 200, 120, 60, "Managers and team leads"),
    ]
    a = [
        arrow([(120, 230), (190, 230)]),
        arrow([(270, 100), (270, 180)], True),
        arrow([(350, 230), (430, 230)], True),
        arrow([(570, 230), (650, 230)], True),
        arrow([(790, 230), (840, 230)]),
        arrow([(350, 55), (650, 55)], True),
        arrow([(810, 55), (900, 55), (900, 200)]),
    ]
    x = "".join([
        num(155, 230, 1), lbl(155, 208, "starts"),
        num(270, 140, 2), lbl(350, 140, "records, read-only"),
        num(390, 230, 3), lbl(390, 208, "saves"),
        num(610, 230, 4), lbl(610, 208, "shown"),
        num(815, 230, 5), lbl(815, 208, "act"),
        num(500, 55, 6), lbl(500, 33, "way 3: notebook fills them"),
        num(855, 55, 7), lbl(855, 33, "read"),
    ])
    return mapdiv(960, 300, b, a, x) + foot(
        "Key: pill = people · bold border = the audit app · white = Databricks · grey = data or report · "
        "solid = people act · dashed = data moves · numbers = order",
        "Notice: data only flows out of Databricks; only the dashboards way stores results there.")


GLANCE = [
    "What: Crosshire checks one Databricks account and shows the results on simple pages.",
    "How: 160 read-only checks on Databricks' built-in usage and billing records.",
    "Why: it shows where money goes, what looks wasted, and setup gaps.",
    "Who: FinOps, data engineers, governance, CTO and CFO, each with a lens.",
    "Output: a short fix list with impact, effort and team for each fix.",
    "Money: every dollar is Databricks list price, not your invoice.",
    "Cost to run: warehouse time for each export; browsing the pages costs no queries.",
    "Decision needed: pick a way to use it, and who it runs as (see Decisions).",
]

# ---------------------------------------------------------------- 2. tiles
TILES = [
    ("Cost · Overview", "Where money goes", "Spend by day, month, product, workspace, warehouse and job.", "",
     "Always-on dev warehouse: 4,320 DBUs (Databricks Units) in 30 days", "derived from tests/fake_account/build.py:208-216"),
    ("Waste & savings", "Waste", "What is billed but producing nothing? Five priced checks, each resource counted once.", "red",
     "BI warehouse: $101.93 possible waste in 7 days", "from tests/test_fake_account.py:64"),
    ("Compute", "Warehouses and clusters", "Idle time, auto-stop settings, cluster setup risks.", "red",
     "Shared cluster with no policy: Critical", "from tests/test_fake_account.py:93"),
    ("Jobs", "Jobs and pipelines", "Which jobs fail, slow down, wait for compute or run on oversized machines?", "red",
     "Job failed its last 3 runs: Critical", "from tests/test_fake_account.py:109"),
    ("Queries", "Queries", "Which SQL queries cost most, fail, or wait for a slot?", "amber",
     "BI warehouse: 2 failed queries on each of 3 days: Warn", "from tests/test_fake_account.py:207-208"),
    ("Storage · Data usage", "Storage and data use", "Which tables are unused, have many small files, or miss maintenance; which catalogs and schemas are complex but little used?", "",
     "A dropped table is not a small-files or optimization to-do", "from tests/test_fake_account.py:280-282"),
    ("ML & AI", "Model serving and AI", "Which model serving endpoints are billed with no requests?", "amber",
     "Vector search endpoint: $30 in one day, over the $25 line: Warn", "from tests/test_fake_account.py:259-261"),
    ("Governance & PII", "Access and governance", "Broad grants, admin changes, sensitive data, lineage, sharing.", "",
     "OAuth sign-ins are not counted as admin changes", "from tests/test_fake_account.py:268-270"),
    ("Tags · Cost › Chargeback", "Tags and chargeback", "Which spend has no owner tag? Split spend 8 ways for chargeback.", "red",
     "Dev workspace spend with no cost_center tag: Critical", "from tests/test_fake_account.py:159-165"),
]


def tiles():
    out = '<div class="tiles">'
    for ey, ti, q, st, chip, cite in TILES:
        out += (f'<div class="tile"><div class="eyebrow">{e(ey)}</div><h3>{e(ti)}</h3><p>{e(q)}</p>'
                f'<span class="demo {st}">{e(chip)}</span><p class="cite">{e(cite)}</p></div>')
    return out + "</div>"


LEARN_B = [
    "PII means personally identifiable information, for example names or emails.",
    "Chargeback means sending spend back to the team that caused it.",
    "8 chargeback cuts: workspace, product line, SKU (billing item), warehouse, job, cluster, user, tag value.",
    "Mandatory tags out of the box: cost_center, domain, environment.",
    "Each result has a status: Critical, Warn, OK, Ranked, Not assessed, No data, No data (filtered), Reference.",
    '"Not assessed" is never a pass; the reason is shown instead.',
    "Coverage & Gaps lists checks that failed, did not run, or were cut short.",
]


# ---------------------------------------------------------------- 3. L1 swimlane
LH, BH, TOP = 220, 185, 8


def step(l, lane, w, n, title, bl, i, o, cls=""):
    t = lane * LH + TOP
    ul = "".join(f"<li>{e(x)}</li>" for x in bl)
    return (f'<div class="b {cls}" style="left:{l}px;top:{t}px;width:{w}px;height:{BH}px">'
            f'<span class="tag">STEP {n}</span><span class="t">{e(title)}</span><ul>{ul}</ul>'
            f'<div class="io"><span class="l in">IN</span><span>{e(i)}</span>'
            f'<span class="l out">OUT</span><span>{e(o)}</span></div></div>')


def l1():
    names = ["PERSON", "DATABRICKS WAREHOUSE", "LOCAL FILES", "APP", "BROWSER"]
    lanes = "".join(f'<div class="lane" style="top:{i*LH}px;height:{LH}px;width:960px"><span>{n}</span></div>'
                    for i, n in enumerate(names))
    grp = (f'<div class="grp" style="left:255px;top:{LH+TOP-26}px;width:507px;height:{2*LH+TOP+BH+6-(LH+TOP-26)}px">'
           '<span>Repeats on each Refresh: new file; old data kept if it fails</span></div>')
    s = [
        step(100, 0, 150, 1, "Start the export", ["Pick the days (default 30)", "One command, or Refresh in the app"], "days = 30", "export started"),
        step(270, 1, 150, 2, "Run 160 checks", ["Read-only queries on system tables", "4 at a time, one warehouse"], "account records", "rows per check"),
        step(400, 2, 150, 3, "Save the results", ["Private data folder", "Never inside the project folder"], "rows, max 50,000 per check (Refresh: 500,000)", "results folder"),
        step(600, 2, 150, 4, "Build one database file", ["Many files become one file"], "results folder", "1 database file", "shape"),
        step(790, 3, 150, 5, "Serve the pages", ["Opens the file read-only", "No queries while browsing"], "database file", "pages, numbers"),
        step(790, 4, 150, 6, "Read and act", ["Filter by window, environment, tags", "Download CSV or print to PDF"], "pages", "fix list, CSV, PDF"),
    ]
    mid = lambda lane: lane * LH + TOP + BH // 2
    a = [
        arrow([(175, TOP + BH), (175, mid(1)), (270, mid(1))]),
        arrow([(345, LH + TOP + BH), (345, mid(2)), (400, mid(2))], True),
        arrow([(550, mid(2)), (600, mid(2))], True),
        arrow([(750, mid(2)), (860, mid(2)), (860, 3 * LH + TOP)], True),
        arrow([(860, 3 * LH + TOP + BH), (860, 4 * LH + TOP)], True),
        arrow([(940, mid(4)), (952, mid(4)), (952, mid(1)), (420, mid(1))]),
    ]
    x = "".join([
        num(175, (TOP+BH+mid(1))//2, 1), lbl(130, (TOP+BH+mid(1))//2, "runs on"),
        num(345, (LH+TOP+BH+mid(2))//2, 2), lbl(313, (LH+TOP+BH+mid(2))//2, "rows"),
        num(575, mid(2), 3), lbl(575, mid(2) - 22, "files"),
        num(805, mid(2), 4), lbl(815, mid(2) - 22, "database file"),
        num(860, 3 * LH + TOP + BH + (LH-BH)//2, 5), lbl(822, 3 * LH + TOP + BH + (LH-BH)//2, "pages"),
        num(700, mid(1), 6), lbl(700, mid(1) - 22, "Refresh"),
    ])
    return mapdiv(960, 5 * LH, s, a, x, back=lanes + grp) + foot(
        "Key: band = who or where · white box = step · amber = data changes shape · purple dashed = repeats · "
        "solid = triggers · dashed = data moves · numbers = order",
        "Notice: only step 2 uses the warehouse; reading the pages costs nothing.")


HOW_B = [
    "An export takes minutes to tens of minutes.",
    "Today is included but partial; billing records arrive a few hours late.",
    "Refresh offers 7, 15, 30, 60 or 90 days and keeps the newest two runs.",
    "If a step fails, the run stops there and names the failed step.",
    "Pages show 7-, 30- and 90-day windows; a short export reads \"partial\".",
    "CSV means comma-separated values; PDF means Portable Document Format.",
]
STEPS = [
    ("1", "Start the export", "days = 30", "export started"),
    ("2", "Run 160 checks", "account records", "rows per check"),
    ("3", "Save the results", "rows (max 50,000 per check; Refresh: 500,000)", "results folder"),
    ("4", "Build one database file", "results folder", "1 database file"),
    ("5", "Serve the pages", "database file", "pages, numbers"),
    ("6", "Read and act", "pages", "fix list, CSV, PDF"),
]


def steptable():
    r = "".join(f'<tr><td><span class="n">{n}</span></td><td>{e(s)}</td><td class="v">{e(i)}</td><td class="v">{e(o)}</td></tr>'
                for n, s, i, o in STEPS)
    return (f'<details><summary>Step by step</summary><div class="tblwrap"><table class="steps">'
            f'<tr><th>#</th><th>Step</th><th>In</th><th>Out</th></tr>{r}</table></div></details>')


# ---------------------------------------------------------------- 4. scenario
def scenario():
    w = 128
    xs = [i * 208 for i in range(5)]
    items = [("Runs the export", "Monday 08:00, illustrative", ""), ("Opens Waste & savings", "possible waste", ""),
             ("Opens Compute", "idle warehouses", ""), ("Opens Actions", "fix list", ""),
             ("Refresh next Monday", "illustrative", "tbc")]
    b = [box(c, x, 30, w, 90, t, "", s) for x, (t, s, c) in zip(xs, items)]
    a = [arrow([(x + w, 75), (x + 208, 75)]) for x in xs[:4]]
    labels = ["exported", "drill in", "pick fixes", "next week"]
    ex = "".join(num(x + w + 40, 75, i + 1) + lbl(x + w + 40, 53, labels[i]) for i, x in enumerate(xs[:4]))
    return mapdiv(960, 150, b, a, ex) + foot(
        "Key: white = a step the lead takes · dashed grey = illustrative next step · numbers = order",
        "Notice: one export gives money, setup and job findings in one sitting.")


SCEN_B = [
    "A platform lead runs the export over the demo account on Monday morning (timing illustrative).",
    "The demo account is a fake account built for tests with known answers.",
    "Waste & savings shows the BI (business intelligence) warehouse idle 98.4% of its running time.",
    "Compute shows an always-on dev warehouse: auto-stop off, no query for 7 days.",
    "Compute also lists a warehouse created 200 days ago and never started.",
    "Actions turns these into fixes, each with impact, effort and team.",
    "Next Monday's Refresh plus Cost › Before & after shows the change (illustrative).",
]
EX_ROWS = [
    ("BI warehouse", "Idle 98.4% of running time; 70 short queries", "Critical", "$101.93 possible waste, 7 days"),
    ("Always-on dev warehouse", "Auto-stop off; ran all 7 days with no query", "Critical", "4,320 DBUs billed in 30 days ≈ $2,376"),
    ("Unused warehouse", "Created 200 days ago; never started", "Warn", "$0, never billed"),
    ("Broken job", "Last 3 runs failed (30 days)", "Critical", "not priced on this card"),
    ("Dev workspace", "Spend with no cost_center tag (30 days)", "Critical", "not priced on this card"),
]
SCEN_B2 = [
    "DBU means Databricks Unit, Databricks' billing unit; dollars = DBUs x list price.",
    "Possible waste covers the picked window; the Overview never projects it to a month.",
    "The ETL (extract, transform, load) warehouse stays OK: its gaps are under 60 seconds.",
]


def excard():
    rows = ""
    for r, s, st, m in EX_ROWS:
        c = "crit" if st == "Critical" else "warn"
        rows += f'<tr><td>{e(r)}</td><td>{e(s)}</td><td class="{c}">{e(st)}</td><td>{e(m)}</td></tr>'
    return ('<div class="card"><div class="h"><span class="dir">OUT</span>what the lead sees · demo account, last 7 days unless noted</div>'
            '<div class="tblwrap"><table><tr><th>Resource</th><th>What the app shows</th><th>Status</th><th>Money (list price)</th></tr>'
            f'{rows}</table></div>'
            '<p class="cite">from tests/test_fake_account.py:62-72, 96, 109, 159-165 · $2,376 derived from '
            'tests/fake_account/build.py:73, 77, 208-216 (6 DBUs/hour x 720 hours x $0.55)</p></div>')


# ---------------------------------------------------------------- 5. three ways
def ways():
    cx = [240, 480, 720]
    w = 180
    h = 90
    rt = [10, 115, 220]
    b = [box("", 30, 10, 140, 300, "Databricks account", "SYSTEM", "usage records")]
    rows = [
        [("", "Export from your machine", "WAY 1", "one SQL warehouse"), ("data", "Private file", "DATA", "your machine"),
         ("data", "App in your browser", "REPORT", "this machine only")],
        [("entry", "App inside Databricks", "WAY 2", "opened with a link"), ("data", "App's own disk", "DATA", "wiped on restart"),
         ("data", "Shared pages", "REPORT", "anyone with access")],
        [("", "Notebook runs checks", "WAY 3", "159 checks, 4 at a time"), ("data", "Schema in your catalog", "DATA", "results tables"),
         ("data", "14 AI/BI dashboards", "REPORT", "+7 read live")],
    ]
    a, x = [], ""
    for r, row in enumerate(rows):
        for c, (cls, t, tag, sub) in enumerate(row):
            b.append(box(cls, cx[c], rt[r], w, h, t, tag, sub))
        y = rt[r] + h // 2
        a.append(arrow([(170, y), (cx[0], y)], True))
        a.append(arrow([(cx[0] + w, y), (cx[1], y)], True))
        a.append(arrow([(cx[1] + w, y), (cx[2], y)], True))
        x += num(205, y, 1) + num(cx[0] + w + 30, y, 2) + num(cx[1] + w + 30, y, 3)
    x += lbl(205, rt[0] + h // 2 - 22, "read-only")
    return mapdiv(960, 320, b, a, x) + foot(
        "Key: white = Databricks or a step · bold border = app inside Databricks · grey = data or report · dashed = data moves",
        "Notice: all three run the same checks; only way 3 stores results in Databricks.")


WAYS_T = (["", "1. Local app", "2. Databricks App", "3. AI/BI dashboards"], [
    ("Best for", "One person, a trial", "A team, shared by link", "Teams who live in Databricks dashboards"),
    ("Where it runs", "Your machine or a server", "Inside Databricks", "Inside Databricks"),
    ("Writes to Databricks?", "No", "No; results stay on the app's disk", "Yes, results saved in a schema in your catalog"),
    ("Needs a personal token?", "Yes", "Yes to deploy; stored only if it runs as you", "No"),
    ("Queries the warehouse", "Each export or Refresh", "Each Get or Refresh", "Notebook runs on serverless or a cluster; dashboards on a warehouse"),
    ("Who sees the data", "Whoever uses that machine", "Everyone who opens it, unless limited",
     "People you share them with (Can view); credentials embedded"),
])
WAYS_B = [
    "7 live dashboards: waste, job health, workspace usage, grants, admin changes, data use, dashboard load.",
    "A 2X-Small serverless warehouse is enough for the dashboards.",
    "The Databricks App disk is wiped on restart; click Get or Refresh again.",
]

# ---------------------------------------------------------------- 6-8, 10
WHO_T = (["Lens", "For", "Sees"], [
    ("FinOps (financial operations), the default", "Cloud spend owners", "Overview, Actions, Cost, Waste, ML & AI, Genie, Compute, Tags, Coverage, All findings"),
    ("Data engineer", "Teams running jobs and queries", "Overview, Actions, Jobs, Compute, Queries first, then every other page"),
    ("Governance", "Access and data protection owners", "Overview, Actions, Data usage, Governance & PII, Tags, All findings"),
    ("CTO (chief technology officer)", "Technology leadership", "Every page as a summary, no drill-down (Coverage keeps sub-tabs)"),
    ("CFO (chief financial officer)", "Finance leadership", "One page: spend by month, environment, product, workspace, top resources, top tags"),
])
COST_B = [
    "Software licence: MIT or Apache-2.0, open source.",
    "Each export or Refresh uses SQL warehouse time; browsing does not.",
    "A SQL warehouse is Databricks compute that runs SQL and is billed while running.",
    "The export sends 4 statements at a time to one warehouse.",
    "An export takes minutes to tens of minutes.",
    "Its queries carry query tags cost_center and domain = lakehouse_health.",
    'The Queries page shows "This audit\'s own query cost".',
    "A Medium Databricks App has 2 CPUs and 6 GB of memory.",
    "Databricks App hosting price: Not defined in source: confirm with owner.",
    "Dashboards way: notebook on serverless or any cluster; dashboards on a SQL warehouse.",
]
RISK_T = (["#", "Risk", "Why it matters", "What to do"], [
    ("1", "Dollars are estimates", "List price only; no contract rates; cloud machine bill left out",
     "Treat as relative size; set a discount for a what-if"),
    ("2", "Account data can leak", "Files hold account data; local app has no login; Databricks App shows all by default",
     'Keep files private; run locally; turn on "who sees what" or masking'),
    ("3", "Gaps can hide problems", "Other-region workspaces lack job, query and compute findings; capped checks under-count",
     "Read Coverage & Gaps before acting on totals"),
])
RISK_B = [
    "Possible waste checks can overlap; none of the figures is a forecast.",
    "With run-as-you, the app's token sits in a workspace file others may read.",
    "Each check keeps 50,000 rows, worst first (500,000 from Refresh).",
]


# ---------------------------------------------------------------- 9. decisions
def decide():
    def dia(x, y, w, h, text):
        return (f'<polygon points="{x+w//2},{y} {x+w},{y+h//2} {x+w//2},{y+h} {x},{y+h//2}" fill="#E7E2F5" stroke="#6B4FA8"/>',
                f'<div class="b c decision" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"><span class="t">{e(text)}</span></div>')
    d1p, d1t = dia(0, 95, 170, 110, "Who will read it?")
    d2p, d2t = dia(560, 105, 130, 90, "Runs as?")
    b = [d1t, d2t,
         box("", 330, 10, 170, 60, "Way 1: local app"),
         box("", 330, 120, 170, 60, "Way 2: Databricks App"),
         box("", 330, 230, 170, 60, "Way 3: AI/BI dashboards"),
         box("", 770, 80, 190, 60, "Me (quick, token stored)"),
         box("", 770, 160, 190, 60, "Its own account (no token stored)")]
    a = [d1p, d2p,
         arrow([(170, 150), (205, 150), (205, 40), (330, 40)]),
         arrow([(205, 150), (330, 150)]),
         arrow([(205, 150), (205, 260), (330, 260)]),
         arrow([(500, 150), (560, 150)]),
         arrow([(690, 150), (730, 150), (730, 110), (770, 110)]),
         arrow([(730, 150), (730, 190), (770, 190)])]
    x = lbl(267, 20, "Just me, a trial") + lbl(267, 130, "A team, by link") + lbl(267, 240, "Dashboard users")
    return mapdiv(960, 300, b, a, x) + foot(
        "Key: lavender diamond = decision · white box = option · solid = then",
        "Notice: who it runs as matters only for the Databricks App.")


DEC_T = (["#", "Decision", "Options", "Default"], [
    ("1", "Which way to start with", "Local app · Databricks App · AI/BI dashboards", "Local app (the README's main path)"),
    ("2", "Who the Databricks App runs as", "You (quick trial; token stored) · its own service principal (no token stored; a metastore admin grants read once)", "You"),
    ("3", "Limit what each person sees", "Off · by workspace, warehouse or environment", "Off"),
    ("4", "Hide user emails", "Off · replace names and emails with a short code", "Off"),
    ("5", "Production setup: own warehouse, schema, copy job", "Not built yet: confirm with owner", "n/a"),
])
TERMS_T = (["Term", "Meaning"], [
    ("Databricks account", "Your company's Databricks setup: all its workspaces and bills."),
    ("Workspace", "One Databricks environment, often prod, uat (user acceptance testing) or dev."),
    ("System tables", "Databricks' built-in records of usage, billing, jobs, queries and access."),
    ("SQL warehouse", "Databricks compute that runs SQL; billed while running."),
    ("DBU", "Databricks Unit, the billing unit; dollars = DBUs x list price."),
    ("List price", "Databricks' published price; your contract discount is not known."),
    ("Auto-stop", "Setting that stops an idle warehouse after N minutes."),
    ("Tag", "A label on a resource, e.g. cost_center = bi."),
    ("Chargeback", "Sending spend back to the team that caused it."),
    ("Critical / Warn", "Worst kind of problem / flagged but not the worst."),
    ("Lens", "A preset view per role; not a permission."),
    ("Service principal", "A non-human account an app can run as."),
    ("CSV / PDF", "Comma-separated values file for spreadsheets / Portable Document Format for printing."),
])
SOURCES = [
    "At a glance: README.md:1-19, 283-292, 348-350; web/src/tabs/tab_actions.tsx:1-4; app/core/pricing.py:24-26",
    "What you learn: README.md:266-280, 352-377; web/src/tabs/tab_cost.tsx:1355-1365; web/src/components/tab_registry.ts:102-115; web/src/components/roles.ts:32-39; web/src/tabs/tab_actions.tsx:25-57; config/settings.yml:36; tests/test_fake_account.py:62-64, 93, 109, 159-165, 207-208, 259-261, 268-270, 280-282",
    "How a report gets made: README.md:67-102, 177-202, 297-338, 412; app/core/data.py:1-6, 304-329; web/src/api.ts:8; app/api/refresh.py:48-69; tools/load_direct_results.py:516-547",
    "Demo account: tests/fake_account/build.py:1-36, 73-77, 170-221; tests/test_fake_account.py:53-100; tests/test_waste_contract.py:82-85",
    "Three ways: README.md:10-19, 204-244; docs/databricks-app.md:1-35, 127-132; databricks_dashboards/README.md:42-60, 79-83",
    "Costs, risks, decisions: README.md:33, 76-77, 202, 404-413, 435, 508, 755-758; config/settings.yml:13-15, 80, 87-94; config/app.yml:10; docs/databricks-app.md:21-31, 151-165; web/src/tabs/tab_query.tsx:319",
]

TOC = [("glance", "At a glance"), ("learn", "What you learn"), ("how", "How a report gets made"),
       ("scenario", "A Monday with the demo account"), ("ways", "Three ways to use it"), ("who", "Who uses it"),
       ("cost", "What it costs"), ("risks", "Risks"), ("decide", "Decisions needed"), ("terms", "Terms"),
       ("sources", "Sources")]


def build():
    toc = "".join(f'<a class="chip" href="#{i}">{e(t)}</a>' for i, t in TOC)
    meta = "".join(f'<span class="chip">{e(c)}</span>' for c in
                   ["160 checks", "read-only", "3 ways to use it", "repo commit c2e5f91"])
    body = f"""
<div class="eyebrow">Functional view · for business, management and newcomers</div>
<h1>Crosshire overview</h1>
<p class="deck">A read-only audit of a Databricks account: where money goes, what looks wasted, how things are set up.</p>
<div class="chips">{meta}</div>
<nav class="toc" aria-label="Contents"><div class="eyebrow">Contents</div><div class="chips">{toc}</div></nav>
{section("glance", "1. At a glance", l0() + bullets(GLANCE))}
{section("learn", "2. What you learn", tiles() + foot("Key: tile = one area of the account · chip = a demo account result · red = Critical · amber = Warn · grey = no status", "Notice: every area answers a money or a setup question.") + bullets(LEARN_B))}
{section("how", "3. How a report gets made", l1() + bullets(HOW_B) + steptable())}
{section("scenario", "4. A Monday with the demo account", scenario() + bullets(SCEN_B) + excard() + bullets(SCEN_B2))}
{section("ways", "5. Three ways to use it", ways() + table(*WAYS_T) + bullets(WAYS_B))}
{section("who", "6. Who uses it", bullets(['A "lens" picks which pages show; it is not a permission.']) + table(*WHO_T) + bullets(["The person who runs the export needs read access to the system schemas.", 'The "How it works" page explains every screen, check and status.']))}
{section("cost", "7. What it costs", bullets(COST_B))}
{section("risks", "8. Risks", table(*RISK_T) + bullets(RISK_B))}
{section("decide", "9. Decisions needed", decide() + table(*DEC_T) + bullets(["A service principal is a non-human Databricks account owned by the app."]))}
{section("terms", "10. Terms in plain words", table(*TERMS_T))}
<section id="sources"><h2>11. Where these facts come from</h2><details><summary>Where these facts come from</summary>
<ul class="cl">{"".join(f"<li>{e(s)}</li>" for s in SOURCES)}</ul></details>
<p class="cite">Built from the repo at commit c2e5f91. Demo figures come from the test suite, not a real account.</p></section>
"""
    OUT.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                   f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                   f'<title>Crosshire overview</title><style>{CSS}</style></head><body><main>{body}</main></body></html>',
                   encoding="utf-8")


if __name__ == "__main__":
    build()
    print(OUT, OUT.stat().st_size)
