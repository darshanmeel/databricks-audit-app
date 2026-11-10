"""Mechanical structural extraction of the repo into graph/nodes.base.jsonl + graph/edges.base.jsonl.

Stdlib + PyYAML (a repo dependency; used only for line marks of dbt yml). Rerunnable:
    python databricks-audit-app-knowledge-graph/_scripts/extract.py
ID rules: every tracked text file is "file:<path>"; extra semantic nodes (document:, config:,
ui_screen:, example_data:, dbt_model:, ...) hang off their file with a `contains` edge.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

import yaml

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / "graph"
REPO_NAME = REPO.name
BIN_EXT = {".woff", ".woff2", ".ttf", ".otf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
           ".zip", ".parquet", ".duckdb", ".wal", ".pyc", ".exe", ".dll", ".so"}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
TS_EXT = (".ts", ".tsx")

nodes: dict[str, dict] = {}
edges: dict[tuple, dict] = {}
texts: dict[str, list[str]] = {}
skipped: list[str] = []


def node(id_, type_, name, path=None, ls=None, le=None, summary="", tags=None, status="extracted"):
    if id_ in nodes:
        n = nodes[id_]
        for t in tags or []:
            if t not in n["tags"]:
                n["tags"].append(t)
        if summary and not n["summary"]:
            n["summary"] = summary
        return n
    n = {"id": id_, "type": type_, "name": name, "path": path, "line_start": ls, "line_end": le,
         "summary": (summary or "")[:400], "tags": list(tags or []), "status": status}
    nodes[id_] = n
    return n


def edge(frm, to, type_, path, line, status="extracted"):
    if frm == to:
        return
    key = (frm, to, type_)
    if key not in edges:
        edges[key] = {"from": frm, "to": to, "type": type_, "evidence": f"{path}:{line}", "status": status}


def fid(path):
    return f"file:{path}"


# ---------------------------------------------------------------- files / folders

def tracked_files():
    # Working tree: tracked + untracked-not-ignored, minus files deleted but not yet committed.
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], cwd=REPO, capture_output=True, check=True).stdout.decode("utf-8")
    return sorted({p for p in out.split("\0") if p and (REPO / p).is_file()})


SECRET_RE = re.compile(r"(^|/)(\.env[^/]*|[^/]*\.(pem|key|p12|tfvars)|id_rsa[^/]*|credentials[^/]*|secrets?[^/]*|"
                       r"\.npmrc|\.pypirc|kubeconfig|profiles\.yml)$", re.I)


def is_secret(path):
    # Secret files are never opened; only their existence is recorded.
    return bool(SECRET_RE.search(path))


def is_text(path):
    p = PurePosixPath(path)
    if is_secret(path):
        return False
    if p.suffix.lower() in BIN_EXT or p.name in LOCK_FILES or path.startswith("app/web/dist/"):
        return False
    if path.startswith(REPO_NAME + "-knowledge-graph/"):
        return False
    raw = (REPO / path).read_bytes()
    return b"\0" not in raw[:8192]


def first_comment_line(lines):
    for ln in lines[:15]:
        s = ln.strip().lstrip("#/-*<!{ ").strip()
        if len(s) > 10 and not s.startswith("!") and "GENERATED" not in s:
            return s
    return ""


def map_files(files):
    node("repo:" + REPO_NAME, "repo", REPO_NAME, ".", None, None,
         "Crosshire: read-only audit of a Databricks account (system tables -> DuckDB -> app).")
    for path in files:
        if not is_text(path):
            skipped.append(path)
            continue
        lines = (REPO / path).read_text(encoding="utf-8", errors="replace").splitlines()
        texts[path] = lines
        ext = PurePosixPath(path).suffix.lower()
        type_ = "module" if ext == ".py" else "file"
        node(fid(path), type_, PurePosixPath(path).name, path, 1, max(1, len(lines)),
             first_comment_line(lines), [ext.lstrip(".") or "noext"])
        parent, child = str(PurePosixPath(path).parent), fid(path)
        while True:
            pid = "repo:" + REPO_NAME if parent == "." else f"folder:{parent}"
            if parent != ".":
                node(pid, "folder", PurePosixPath(parent).name, parent)
            edge(pid, child, "contains", path, 1)
            if parent == "." or pid in nodes and nodes[pid].get("_linked"):
                break
            nodes[pid]["_linked"] = True
            child, parent = pid, str(PurePosixPath(parent).parent)


# ---------------------------------------------------------------- python

py_defs: dict[str, dict[str, str]] = {}   # path -> {qualname: node id}
py_mod_by_dotted: dict[str, str] = {}
py_trees: dict[str, ast.Module] = {}


def py_files():
    return [p for p in texts if p.endswith(".py")]


def dotted(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def is_test_fn(path, name):
    return name.startswith("test_") and PurePosixPath(path).name.startswith("test_")


def signature(fn):
    a = fn.args
    params = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        params.append("*" + a.vararg.arg)
    params += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        params.append("**" + a.kwarg.arg)
    ret = ast.unparse(fn.returns) if fn.returns else None
    return params, ret


def doc1(n):
    d = ast.get_docstring(n)
    return d.strip().splitlines()[0] if d else ""


def py_pass1():
    for path in py_files():
        src = "\n".join(texts[path])
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            nodes[fid(path)]["tags"].append("syntax_error")
            nodes[fid(path)]["status"] = "unclear"
            print("syntax error", path, e, file=sys.stderr)
            continue
        py_trees[path] = tree
        py_mod_by_dotted[dotted(path)] = path
        nodes[fid(path)]["name"] = dotted(path)
        if doc1(tree):
            nodes[fid(path)]["summary"] = doc1(tree)[:400]
        defs = py_defs.setdefault(path, {})

        def add_fn(fn, qual, parent_id):
            params, ret = signature(fn)
            sig = f"{qual}({', '.join(params)})" + (f" -> {ret}" if ret else "")
            if is_test_fn(path, fn.name):
                nid = f"test_case:{path}:{qual}"
                node(nid, "test_case", qual, path, fn.lineno, fn.end_lineno, f"{sig}. {doc1(fn)}".strip(),
                     ["pytest"])
            else:
                nid = f"function:{path}:{qual}"
                tags = ["async"] if isinstance(fn, ast.AsyncFunctionDef) else []
                if qual.startswith("_") or "._" in qual:
                    tags.append("private")
                n = node(nid, "function", qual, path, fn.lineno, fn.end_lineno, f"{sig}. {doc1(fn)}".strip(), tags)
                n["signature"], n["params"], n["returns"] = sig, params, ret
            defs[qual] = nid
            edge(parent_id, nid, "contains", path, fn.lineno)
            return nid

        for st in tree.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_fn(st, st.name, fid(path))
            elif isinstance(st, ast.ClassDef):
                cid = f"class:{path}:{st.name}"
                bases = [ast.unparse(b) for b in st.bases]
                node(cid, "class", st.name, path, st.lineno, st.end_lineno,
                     (f"bases({', '.join(bases)}). " if bases else "") + doc1(st),
                     ["test_class"] if st.name.startswith("Test") else [])
                defs[st.name] = cid
                edge(fid(path), cid, "contains", path, st.lineno)
                for sub in st.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add_fn(sub, f"{st.name}.{sub.name}", cid)


def resolve_module(name, path, level=0):
    """Repo path for an imported module name, or None (third-party / stdlib)."""
    if level:
        base = PurePosixPath(path).parent
        for _ in range(level - 1):
            base = base.parent
        cand = (str(base).replace("/", ".") + ("." + name if name else "")).lstrip(".")
        return py_mod_by_dotted.get(cand)
    if name in py_mod_by_dotted:
        return py_mod_by_dotted[name]
    # sys.path-style sibling imports (tests/dbutil.py, tests/fixtures/base.py, tools/*.py)
    for d in (str(PurePosixPath(path).parent), "tests", "tests/fixtures", "tools", "app"):
        cand = (d.replace("/", ".") + "." + name) if d != "." else name
        if cand in py_mod_by_dotted:
            return py_mod_by_dotted[cand]
    return None


ENDPOINT_METHODS = {"get", "post", "put", "delete", "patch"}
endpoint_ids: dict[str, str] = {}   # "METHOD path" -> id


def py_pass2():
    for path, tree in py_trees.items():
        defs = py_defs[path]
        mod_alias: dict[str, str] = {}     # local name -> repo path (module)
        name_alias: dict[str, tuple] = {}  # local name -> (repo path, name)
        for st in ast.walk(tree):
            if isinstance(st, ast.Import):
                for a in st.names:
                    tgt = resolve_module(a.name, path)
                    if tgt:
                        edge(fid(path), fid(tgt), "imports", path, st.lineno)
                        mod_alias[a.asname or a.name] = tgt
            elif isinstance(st, ast.ImportFrom):
                base = resolve_module(st.module or "", path, st.level)
                for a in st.names:
                    sub = resolve_module(((st.module + ".") if st.module else "") + a.name, path, st.level)
                    if sub:   # `from app.core import data`
                        edge(fid(path), fid(sub), "imports", path, st.lineno)
                        mod_alias[a.asname or a.name] = sub
                    elif base:
                        edge(fid(path), fid(base), "imports", path, st.lineno)
                        name_alias[a.asname or a.name] = (base, a.name)

        def target(call):
            f = call.func
            if isinstance(f, ast.Name):
                if f.id in defs:
                    return defs[f.id]
                if f.id in name_alias:
                    m, nm = name_alias[f.id]
                    return py_defs.get(m, {}).get(nm)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                v = f.value.id
                if v in mod_alias:
                    return py_defs.get(mod_alias[v], {}).get(f.attr)
                if v in name_alias:   # imported class: Cls.method
                    m, nm = name_alias[v]
                    return py_defs.get(m, {}).get(f"{nm}.{f.attr}")
                if v in defs and defs[v].startswith("class:"):
                    return defs.get(f"{v}.{f.attr}")
            return None

        def scan(body_owner, owner_id, cls=None):
            for sub in ast.walk(body_owner):
                if isinstance(sub, ast.Call):
                    t = target(sub)
                    if t is None and cls and isinstance(sub.func, ast.Attribute) and \
                            isinstance(sub.func.value, ast.Name) and sub.func.value.id == "self":
                        t = defs.get(f"{cls}.{sub.func.attr}")
                    if t:
                        is_test = owner_id.startswith("test_case:")
                        etype = "illustrates" if is_test and not t.split(":", 2)[1].startswith("tests/") else "calls"
                        edge(owner_id, t, etype, path, sub.lineno)

        mod_level = []
        for st in tree.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan(st, defs[st.name])
                route_decorators(path, st, defs[st.name])
                test_endpoint_refs(path, st, defs[st.name])
            elif isinstance(st, ast.ClassDef):
                for sub in st.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        q = f"{st.name}.{sub.name}"
                        scan(sub, defs[q], st.name)
                        test_endpoint_refs(path, sub, defs[q])
            else:
                mod_level.append(st)
        for st in mod_level:
            scan(st, fid(path))


def route_decorators(path, fn, fn_id):
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and \
                dec.func.attr in ENDPOINT_METHODS and isinstance(dec.func.value, ast.Name) and \
                dec.args and isinstance(dec.args[0], ast.Constant):
            method, route = dec.func.attr.upper(), dec.args[0].value
            eid = f"endpoint:{method} {route}"
            tags = ["fastapi"] + ([] if route.startswith("/api") else ["static_shell"])
            node(eid, "endpoint", f"{method} {route}", path, dec.lineno, fn.end_lineno,
                 f"Handled by {fn.name}(). {doc1(fn)}".strip(), tags)
            endpoint_ids[f"{method} {route}"] = eid
            edge(fn_id, eid, "exposes", path, dec.lineno)
            edge(fid(path), eid, "contains", path, dec.lineno)


def route_regex(route):
    parts = re.split(r"\{[^}]+\}", route)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


def match_endpoint(url, method=None):
    url = url.split("?")[0].split("${qs(")[0]
    url = re.sub(r"\$\{[^}]*\}", "X", url)
    for key, eid in endpoint_ids.items():
        m, r = key.split(" ", 1)
        if method and m != method:
            continue
        if r.startswith("/api") and route_regex(r).match(url):
            return eid
    return None


pending_test_urls: list = []


def test_endpoint_refs(path, fn, owner_id):
    if not owner_id.startswith("test_case:"):
        return
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.startswith("/api/"):
            pending_test_urls.append((owner_id, sub.value, path, sub.lineno))
        elif isinstance(sub, ast.JoinedStr) and sub.values and isinstance(sub.values[0], ast.Constant) \
                and str(sub.values[0].value).startswith("/api/"):
            url = "".join(v.value if isinstance(v, ast.Constant) else "X" for v in sub.values)
            pending_test_urls.append((owner_id, url, path, sub.lineno))


def resolve_test_urls():
    for owner, url, path, line in pending_test_urls:
        eid = match_endpoint(url)
        if eid:
            edge(owner, eid, "illustrates", path, line)


# ---------------------------------------------------------------- SQL table reads

SQL_TABLE_RE = re.compile(
    r"\b(?:from|join)\s+(`?system`?\s*\.\s*`?\w+`?\s*\.\s*`?\w+`?|`?databricks_audit_dashboard`?\.`?\w+`?)",
    re.I)


def strip_sql_comments(lines):
    out, in_block = [], False
    for ln in lines:
        s, res, i = ln, "", 0
        while i < len(s):
            if in_block:
                j = s.find("*/", i)
                if j < 0:
                    i = len(s)
                else:
                    in_block, i = False, j + 2
            elif s.startswith("/*", i) or s.startswith("{#", i):
                in_block, i = True, i + 2
            elif s.startswith("--", i):
                break
            else:
                res += s[i]
                i += 1
        out.append(res.replace("#}", " "))
    return out


def table_id(raw):
    name = re.sub(r"[`\s]", "", raw).lower()
    return f"table:{name}", name


def sql_reads(path, owner_id, lines=None):
    lines = strip_sql_comments(lines if lines is not None else texts[path])
    for i, ln in enumerate(lines, 1):
        for m in SQL_TABLE_RE.finditer(ln):
            tid, name = table_id(m.group(1))
            node(tid, "table", name, None, None, None,
                 "Databricks system table" if name.startswith("system.") else "Lakeview snapshot table",
                 ["system_table" if name.startswith("system.") else "dashboard_snapshot"])
            edge(owner_id, tid, "reads", path, i)


SQL_HEADER_RE = re.compile(r"^--\s*(query_id|title|domain):\s*(.+)$")
GEN_FROM_RE = re.compile(r"(?:generated from|GENERATED by \S+ from)\s+([\w./-]+\.sql)", re.I)
query_file_by_id: dict[str, str] = {}


def sql_files():
    for path in [p for p in texts if p.endswith(".sql")]:
        lines = texts[path]
        hdr = {}
        for ln in lines[:12]:
            m = SQL_HEADER_RE.match(ln.strip())
            if m:
                hdr.setdefault(m.group(1), m.group(2).strip())
        n = nodes[fid(path)]
        if hdr.get("title"):
            n["summary"] = hdr["title"]
        if hdr.get("query_id"):
            n["query_id"] = hdr["query_id"]
            n["tags"].append("query")
            if path.startswith("app/queries/"):
                query_file_by_id.setdefault(hdr["query_id"], path)
        if hdr.get("domain"):
            n["tags"].append("domain:" + hdr["domain"].split()[0])
        for i, ln in enumerate(lines[:6], 1):
            for m in GEN_FROM_RE.finditer(ln):
                src = m.group(1)
                if src in texts:
                    edge(fid(path), fid(src), "derived_from", path, i)
        if path.startswith(("app/queries/", "app/direct_sql/")):
            n["tags"].append(path.split("/")[1] if path.startswith("app/direct_sql") else
                             "queries:" + path.split("/")[2])
            sql_reads(path, fid(path))


# ---------------------------------------------------------------- dbt

MACRO_DEF_RE = re.compile(r"\{%-?\s*(macro|test)\s+(\w+)\s*\(")
REF_RE = re.compile(r"\bref\(\s*['\"](\w+)['\"]\s*\)")
SRC_RE = re.compile(r"\bsource\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)")
dbt_model_ids: dict[str, str] = {}
macros: dict[str, str] = {}


def yaml_docs(path):
    with open(REPO / path, encoding="utf-8") as f:
        return yaml.compose(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def yend(n):
    """1-based last line of a yaml node (end_mark points just past it)."""
    m = n.end_mark
    return m.line if m.column == 0 else m.line + 1


def ynode_get(mapping, key):
    if not isinstance(mapping, yaml.MappingNode):
        return None
    for k, v in mapping.value:
        if k.value == key:
            return v
    return None


def yval(n):
    return yaml.safe_load(yaml.serialize(n)) if n is not None else None


def dbt():
    # models
    for path in texts:
        if path.startswith("dbt/models/") and path.endswith(".sql"):
            name = PurePosixPath(path).stem
            mid = f"dbt_model:{name}"
            layer = path.split("/")[2]
            m = re.search(r"materialized\s*=\s*['\"](\w+)", "\n".join(texts[path][:3]))
            node(mid, "dbt_model", name, path, 1, max(1, len(texts[path])), "",
                 ["dbt", "layer:" + layer] + (["materialized:" + m.group(1)] if m else []))
            dbt_model_ids[name] = mid
            edge(fid(path), mid, "contains", path, 1)
    for path in texts:
        if path.startswith("dbt/seeds/") and path.endswith(".csv"):
            name = PurePosixPath(path).stem
            sid = f"example_data:{path}"
            node(sid, "example_data", name, path, 1, max(1, len(texts[path])),
                 "dbt seed; header: " + (texts[path][0] if texts[path] else ""), ["dbt_seed"])
            dbt_model_ids[name] = sid
            edge(fid(path), sid, "contains", path, 1)
    # macros
    for path in texts:
        if (path.startswith("dbt/macros/") or path.startswith("dbt/tests/generic/")) and path.endswith(".sql"):
            lines = texts[path]
            defs = [(i, m) for i, ln in enumerate(lines, 1) for m in MACRO_DEF_RE.finditer(ln)]
            for k, (i, m) in enumerate(defs):
                end = next((j for j in range(i, len(lines) + 1)
                            if re.search(r"\{%-?\s*end(macro|test)", lines[j - 1])), len(lines))
                kind, name = m.group(1), m.group(2)
                mac = f"dbt_macro:{name}"
                node(mac, "dbt_macro", name, path, i, end, first_comment_line(lines) or f"dbt {kind} {name}",
                     ["dbt", "generic_test" if kind == "test" else "macro"])
                macros[name] = mac
                edge(fid(path), mac, "contains", path, i)
    # sources
    src_path = "dbt/models/sources/sources.yml"
    root = yaml_docs(src_path)
    for s in ynode_get(root, "sources").value:
        sname = ynode_get(s, "name").value
        db, sch = yval(ynode_get(s, "database")), yval(ynode_get(s, "schema"))
        for t in ynode_get(s, "tables").value:
            tn = ynode_get(t, "name")
            line = tn.start_mark.line + 1
            sid = f"dbt_source:{sname}.{tn.value}"
            meta = yval(ynode_get(t, "meta")) or {}
            node(sid, "dbt_source", f"{sname}.{tn.value}", src_path, line, max(line, yend(t)),
                 f"source('{sname}','{tn.value}') = {db}.{sch}.{tn.value}; time_column={meta.get('time_column')}, "
                 f"retention_days={meta.get('retention_days')}", ["dbt"])
            edge(fid(src_path), sid, "contains", src_path, line)
            tid = f"table:{db}.{sch}.{tn.value}"
            node(tid, "table", f"{db}.{sch}.{tn.value}", None, None, None, "Databricks system table", ["system_table"])
            edge(sid, tid, "derived_from", src_path, line)
    # model bodies: ref / source / macro calls
    macro_call = re.compile(r"\b(" + "|".join(map(re.escape, macros)) + r")\s*\(") if macros else None
    for path in texts:
        if not (path.startswith("dbt/") and path.endswith(".sql")):
            continue
        if path.startswith("dbt/models/"):
            owner = dbt_model_ids[PurePosixPath(path).stem]
        else:
            mdefs = [macros[m.group(2)] for ln in texts[path] for m in MACRO_DEF_RE.finditer(ln)]
            owner = mdefs[0] if len(mdefs) == 1 else fid(path)
            if path.startswith("dbt/tests/") and not path.startswith("dbt/tests/generic/"):
                owner = f"dbt_test:{PurePosixPath(path).stem}"
                node(owner, "dbt_test", PurePosixPath(path).stem, path, 1, len(texts[path]),
                     first_comment_line(texts[path]), ["dbt", "singular_test"])
                edge(fid(path), owner, "contains", path, 1)
        clean = strip_sql_comments(texts[path])
        for i, ln in enumerate(clean, 1):
            for m in REF_RE.finditer(ln):
                tgt = dbt_model_ids.get(m.group(1))
                if tgt:
                    edge(owner, tgt, "ref", path, i)
                else:
                    stub = f"dbt_model:{m.group(1)}"
                    node(stub, "dbt_model", m.group(1), None, None, None, "ref target not found in dbt/models", [], "unclear")
                    edge(owner, stub, "ref", path, i, "unclear")
            for m in SRC_RE.finditer(ln):
                sid = f"dbt_source:{m.group(1)}.{m.group(2)}"
                if sid not in nodes:
                    node(sid, "dbt_source", f"{m.group(1)}.{m.group(2)}", None, None, None,
                         "not declared in sources.yml", ["dbt"], "unclear")
                edge(owner, sid, "reads", path, i)
            if macro_call and ("{{" in ln or "{%" in ln or "}}" in ln):
                for m in macro_call.finditer(ln):
                    if not MACRO_DEF_RE.search(ln):
                        edge(owner, macros[m.group(1)], "calls", path, i)
        if path.startswith("dbt/models/"):
            sql_reads(path, owner)
            for i, ln in enumerate(texts[path][:6], 1):
                for m in GEN_FROM_RE.finditer(ln):
                    if m.group(1) in texts:
                        edge(owner, fid(m.group(1)), "derived_from", path, i)
    # model yml: descriptions, meta, tests
    for path in texts:
        if path.startswith("dbt/models/") and path.endswith((".yml", ".yaml")) and "sources/" not in path:
            dbt_yml(path)


def dbt_yml(path):
    root = yaml_docs(path)
    for section in ("models", "seeds"):
        seq = ynode_get(root, section)
        if not isinstance(seq, yaml.SequenceNode):
            continue
        for m in seq.value:
            name = ynode_get(m, "name").value
            mid = dbt_model_ids.get(name)
            line = m.start_mark.line + 1
            if not mid:
                mid = f"dbt_model:{name}"
                node(mid, "dbt_model", name, None, None, None, f"declared in {path} but no .sql found", [], "unclear")
            n = nodes[mid]
            desc = yval(ynode_get(m, "description"))
            meta = yval(ynode_get(m, "meta")) or {}
            if desc and not n["summary"]:
                n["summary"] = str(desc)[:400]
            if meta.get("title"):
                n["title"] = meta["title"]
            if meta.get("query_id"):
                n["query_id"] = meta["query_id"]
                if meta.get("is_finding"):
                    n["tags"].append("finding")
                if meta.get("domain"):
                    n["tags"].append("domain:" + str(meta["domain"]))
            edge(fid(path), mid, "documents", path, line)
            rn = ynode_get(ynode_get(m, "meta"), "reads")
            for r in rn.value if isinstance(rn, yaml.SequenceNode) else []:
                if isinstance(r, yaml.ScalarNode) and r.value.startswith("system."):
                    tid = f"table:{r.value.lower()}"
                    node(tid, "table", r.value.lower(), None, None, None, "Databricks system table", ["system_table"])
                    edge(mid, tid, "reads", path, r.start_mark.line + 1)
            tests_in(path, mid, name, ynode_get(m, "data_tests") or ynode_get(m, "tests"), None)
            cols = ynode_get(m, "columns")
            if isinstance(cols, yaml.SequenceNode):
                for c in cols.value:
                    cname = ynode_get(c, "name").value
                    tests_in(path, mid, name, ynode_get(c, "data_tests") or ynode_get(c, "tests"), cname)


def tests_in(path, mid, model, seq, column):
    if not isinstance(seq, yaml.SequenceNode):
        return
    for t in seq.value:
        if isinstance(t, yaml.ScalarNode):
            tname, args = t.value, {}
        else:
            k, v = t.value[0]
            tname, args = k.value, yval(v) or {}
        line = t.start_mark.line + 1
        tid = f"dbt_test:{model}:{tname}" + (f":{column}" if column else "")
        if tid in nodes:
            tid += f"@{line}"
        a = args.get("arguments", args) if isinstance(args, dict) else {}
        summ = f"{tname} on {model}" + (f".{column}" if column else "")
        if isinstance(a, dict) and a:
            summ += " " + json.dumps(a, default=str)[:250]
        node(tid, "dbt_test", tname, path, line, max(line, yend(t)), summ, ["dbt", "schema_test"])
        edge(tid, mid, "depends_on", path, line)
        if tname in macros:
            edge(tid, macros[tname], "calls", path, line)
        if tname == "relationships" and isinstance(a, dict):
            m = REF_RE.search(str(a.get("to", "")))
            if m and m.group(1) in dbt_model_ids:
                edge(mid, dbt_model_ids[m.group(1)], "fk", path, line)


# ---------------------------------------------------------------- web (TS/TSX)

TS_IMPORT_RE = re.compile(r"""^\s*(?:import|export)\s+(?:type\s+)?(?:[^;'"]*?\s+from\s+)?["']([^"']+)["']""", re.M | re.S)
TS_EXPORT_RE = re.compile(r"^export\s+(default\s+)?(async\s+)?(function|const|let|class)\s+(\w+)")
API_MEMBER_RE = re.compile(r"^  (\w+)(?::|,)")
API_CALL_RE = re.compile(r"\bApi\.(\w+)\b")
URL_RE = re.compile(r"""["'`](/api/[^"'`?$]*(?:\$\{[^}]*\}[^"'`?$]*)*)""")


def ts_resolve(path, spec):
    if not spec.startswith("."):
        return None
    base = PurePosixPath(path).parent
    p = str(PurePosixPath(*(base / spec).parts))
    parts = []
    for x in p.split("/"):
        if x == "..":
            parts.pop()
        elif x != ".":
            parts.append(x)
    p = "/".join(parts)
    for cand in (p, p + ".ts", p + ".tsx", p + "/index.ts", p + "/index.tsx", p + ".d.ts"):
        if cand in texts:
            return cand
    return None


def ts_block_end(lines, start):
    """Line where a top-level declaration starting at `start` ends (prettier-style: closing at col 0)."""
    first = lines[start - 1]
    if first.rstrip().endswith(";") and first.count("{") == first.count("}") and first.count("(") == first.count(")"):
        return start
    for j in range(start, len(lines)):
        if re.match(r"^[}\)\]]", lines[j]):
            return j + 1
    return start


def web():
    ts = [p for p in texts if p.startswith("web/") and p.endswith(TS_EXT)]
    exports: dict[str, list] = {}
    for path in ts:
        lines = texts[path]
        src = "\n".join(lines)
        for m in TS_IMPORT_RE.finditer(src):
            tgt = ts_resolve(path, m.group(1))
            line = src.count("\n", 0, m.start()) + 1
            if tgt:
                edge(fid(path), fid(tgt), "imports", path, line)
        ex = []
        for i, ln in enumerate(lines, 1):
            m = TS_EXPORT_RE.match(ln)
            if m:
                name, kind = m.group(4), m.group(3)
                end = ts_block_end(lines, i)
                is_comp = path.endswith(".tsx") and name[:1].isupper() and kind in ("function", "const")
                nid = f"function:{path}:{name}"
                node(nid, "function", name, path, i, end, ln.strip()[:200],
                     ["ts", "react_component" if is_comp else kind] + (["default_export"] if m.group(1) else []))
                edge(fid(path), nid, "contains", path, i)
                ex.append((i, end, nid))
        exports[path] = ex
        stem = PurePosixPath(path).stem
        if path.startswith("web/src/tabs/") and stem.startswith("tab_"):
            uid = f"ui_screen:{stem}"
            node(uid, "ui_screen", stem, path, 1, len(lines), first_comment_line(lines), ["web_tab"])
            edge(fid(path), uid, "contains", path, 1)
    # Api client members -> endpoints
    api = "web/src/api.ts"
    if api in texts:
        lines = texts[api]
        start = next(i for i, ln in enumerate(lines, 1) if ln.startswith("export const Api"))
        end = ts_block_end(lines, start)
        members = [(i, API_MEMBER_RE.match(lines[i - 1]).group(1)) for i in range(start + 1, end)
                   if API_MEMBER_RE.match(lines[i - 1])]
        for k, (i, name) in enumerate(members):
            last = (members[k + 1][0] - 1) if k + 1 < len(members) else end - 1
            mid = f"function:{api}:Api.{name}"
            node(mid, "function", f"Api.{name}", api, i, last, lines[i - 1].strip()[:200], ["ts", "api_client"])
            edge(f"function:{api}:Api", mid, "contains", api, i)
            for j in range(i, last + 1):
                for m in URL_RE.finditer(lines[j - 1]):
                    method = "POST" if "method: \"POST\"" in lines[j - 1] else "GET"
                    eid = match_endpoint(m.group(1), method)
                    if eid:
                        edge(mid, eid, "calls", api, j)
                    else:
                        stub = f"endpoint:{method} {m.group(1).split('${qs(')[0]}"
                        node(stub, "endpoint", stub[9:], None, None, None, "called by web, no route found", [], "unclear")
                        edge(mid, stub, "calls", api, j, "unclear")
    # Api.x(...) usage across the web
    for path in ts:
        if path == api:
            continue
        for i, ln in enumerate(texts[path], 1):
            if not ln.lstrip().startswith(("//", "*", "/*")):
                for m in URL_RE.finditer(ln):   # direct fetches outside the Api client (tag_rollup.tsx)
                    eid = match_endpoint(m.group(1), "POST" if 'method: "POST"' in ln else "GET")
                    if eid:
                        owner = next((nid for (s, e, nid) in exports[path] if s <= i <= e), fid(path))
                        edge(owner, eid, "calls", path, i)
            for m in API_CALL_RE.finditer(ln):
                tgt = f"function:{api}:Api.{m.group(1)}"
                if tgt not in nodes:
                    continue
                owner = next((nid for (s, e, nid) in exports[path] if s <= i <= e), fid(path))
                edge(owner, tgt, "calls", path, i)


# ---------------------------------------------------------------- config, env, docs, dashboards, fixtures

def python_string_refs():
    """(path, line, owner_id, value) for every non-docstring string constant in python."""
    out = []
    for path, tree in py_trees.items():
        docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        ranges = [(n["line_start"], n["line_end"], n["id"]) for n in nodes.values()
                  if n["path"] == path and n["type"] in ("function", "test_case")]
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                owner = min((r for r in ranges if r[0] <= n.lineno <= r[1]), key=lambda r: r[1] - r[0],
                            default=(0, 0, fid(path)))[2]
                out.append((path, n.lineno, owner, n.value))
    return out


SECRET_HINT = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL", re.I)


def config_env_docs(strings):
    # config files
    for path in texts:
        if path.startswith("config/"):
            cid = f"config:{path}"
            tags = ["grain"] if path.startswith("config/grains/") else ["config"]
            node(cid, "config", PurePosixPath(path).name, path, 1, len(texts[path]), first_comment_line(texts[path]), tags)
            edge(fid(path), cid, "contains", path, 1)
    models_by_qid = {}
    for mid in dbt_model_ids.values():
        if nodes[mid].get("query_id"):
            models_by_qid.setdefault(nodes[mid]["query_id"], []).append(mid)
    gid = "config:config/grains"
    node(gid, "config", "grains", "config/grains", None, None,
         "Per-query grain (key columns) files; tools generate dbt unique_grain tests from them (inferred)", ["grain"])
    for path in texts:
        if path.startswith("config/grains/") and path.endswith(".yml"):
            edge(f"config:{path}", gid, "part_of", path, 1)
            for i, ln in enumerate(texts[path], 1):
                m = re.match(r"^(\w+):", ln)
                if m:
                    q = m.group(1)
                    tgt = query_file_by_id.get(q)
                    mids = models_by_qid.get(q, [])
                    if tgt:
                        edge(f"config:{path}", fid(tgt), "configures", path, i)
                    for mid in mids:
                        edge(f"config:{path}", mid, "configures", path, i, "inferred")
    basenames = {PurePosixPath(p).name: f"config:{p}" for p in texts if p.startswith("config/") and "/grains/" not in p}
    basenames.update({"settings.local.yml": None, "app.local.yml": None})
    for path, line, owner, val in strings:
        if len(val) > 80:
            continue
        for b, cid in basenames.items():
            if cid and b in val:
                edge(cid, owner, "configures", path, line)
        if val == "grains":
            edge(gid, owner, "configures", path, line)
    # env vars: .env.example names + os.environ/getenv names
    env_names = {}
    if ".env.example" in texts:
        for i, ln in enumerate(texts[".env.example"], 1):
            m = re.match(r"^([A-Z][A-Z0-9_]+)=", ln)
            if m:
                env_names[m.group(1)] = i
    for path, tree in py_trees.items():
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = ast.unparse(n.func)
                if f in ("os.environ.get", "os.getenv", "environ.get") and n.args and isinstance(n.args[0], ast.Constant) \
                        and isinstance(n.args[0].value, str):
                    env_names.setdefault(n.args[0].value, None)
            elif isinstance(n, ast.Subscript) and ast.unparse(n.value) == "os.environ" and \
                    isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                env_names.setdefault(n.slice.value, None)
    for name, line in env_names.items():
        secret = bool(SECRET_HINT.search(name))
        eid = f"secret_ref:{name}" if secret else f"config:env:{name}"
        node(eid, "secret_ref" if secret else "config", name, ".env.example" if line else None, line, line,
             "Environment variable (name only; value never stored)" + ("; declared in .env.example" if line else ""),
             ["env_var"] + (["secret"] if secret else []))
        if line:
            edge(fid(".env.example"), eid, "contains", ".env.example", line)
    env_ids = {n["name"]: n["id"] for n in nodes.values() if "env_var" in n["tags"]}
    for path, line, owner, val in strings:
        if val in env_ids:
            edge(env_ids[val], owner, "configures", path, line)
    # documents
    for path in texts:
        if path.endswith((".md", ".html")) and not path.startswith("web/"):
            did = f"document:{path}"
            title = next((ln.lstrip("# ").strip() for ln in texts[path] if ln.startswith("#")), "")
            if path.endswith(".html"):
                m = re.search(r"<title>(.*?)</title>", "\n".join(texts[path][:40]), re.S)
                title = m.group(1).strip() if m else ""
            tags = ["review"] if path.startswith("reviews/") else ["doc"]
            node(did, "document", PurePosixPath(path).name, path, 1, len(texts[path]), title, tags)
            edge(fid(path), did, "contains", path, 1)


def dashboards():
    for path in texts:
        if not path.endswith(".lvdash.json"):
            continue
        lines = texts[path]
        try:
            d = json.loads("\n".join(lines))
        except json.JSONDecodeError:
            nodes[fid(path)]["status"] = "unclear"
            continue
        stem = PurePosixPath(path).name.removesuffix(".lvdash.json")
        slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
        uid = f"ui_screen:lakeview:{slug}"
        ds = [x.get("displayName") or x.get("name") for x in d.get("datasets", [])]
        pages = [p.get("displayName") for p in d.get("pages", [])]
        n = node(uid, "ui_screen", stem, path, 1, len(lines),
                 f"Lakeview dashboard; pages: {', '.join(map(str, pages))}; {len(ds)} datasets",
                 ["lakeview", "dashboard"])
        n["datasets"] = ds
        n["pages"] = pages
        edge(fid(path), uid, "contains", path, 1)
        sql_reads(path, uid)   # queryLines are JSON strings; FROM/JOIN regex still finds tables per line


def fixtures():
    ins = re.compile(r"INSERT INTO\s+(\w+?)__(\w+)", re.I)
    for path in texts:
        if path.startswith(("tests/fixtures/", "tests/fake_account/")) and path.endswith(".py"):
            for i, ln in enumerate(texts[path], 1):
                for m in ins.finditer(ln):
                    tid = f"table:system.{m.group(1).lower()}.{m.group(2).lower()}"
                    node(tid, "table", tid[6:], None, None, None, "Databricks system table", ["system_table"])
                    edge(fid(path), tid, "writes", path, i, "inferred")
            nodes[fid(path)]["tags"].append("fixture_builder")
        if path.startswith("tests/") and path.endswith((".csv", ".json")) or path == "config/workspace.example.csv":
            eid = f"example_data:{path}"
            node(eid, "example_data", PurePosixPath(path).name, path, 1, len(texts[path]),
                 ("header: " + texts[path][0][:200]) if path.endswith(".csv") and texts[path] else "", ["fixture"])
            edge(fid(path), eid, "contains", path, 1)


# ---------------------------------------------------------------- validate + write

def finalize():
    for n in nodes.values():
        n.pop("_linked", None)
    for e in list(edges.values()):
        for end in (e["from"], e["to"]):
            if end not in nodes:
                node(end, end.split(":", 1)[0], end.split(":")[-1], None, None, None,
                     "stub: referenced by an edge, not extracted", ["stub"], "unclear")
    bad = 0
    for n in nodes.values():
        p = n["path"]
        if p and p != "." and not (REPO / p).exists():
            n["status"], bad = "unclear", bad + 1
        if p in texts and n["line_end"] and n["line_end"] > max(1, len(texts[p])):
            n["line_end"], bad = len(texts[p]), bad + 1
    for e in edges.values():
        p, _, ln = e["evidence"].rpartition(":")
        if p not in texts or not (1 <= int(ln) <= max(1, len(texts[p]))):
            e["status"], bad = "unclear", bad + 1
    return bad


def main():
    files = tracked_files()
    map_files(files)
    py_pass1()
    py_pass2()
    sql_files()
    dbt()
    web()
    resolve_test_urls()
    config_env_docs(python_string_refs())
    dashboards()
    fixtures()
    bad = finalize()
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "nodes.base.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for n in sorted(nodes.values(), key=lambda n: n["id"]):
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    with open(OUT / "edges.base.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for e in sorted(edges.values(), key=lambda e: (e["from"], e["type"], e["to"])):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"tracked={len(files)} text={len(texts)} skipped={len(skipped)} fixups={bad}")
    print(f"nodes={len(nodes)} edges={len(edges)}")
    print("nodes by type:", dict(Counter(n["type"] for n in nodes.values()).most_common()))
    print("edges by type:", dict(Counter(e["type"] for e in edges.values()).most_common()))
    print("status:", dict(Counter(n["status"] for n in nodes.values())), dict(Counter(e["status"] for e in edges.values())))


if __name__ == "__main__":
    main()
