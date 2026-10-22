#!/usr/bin/env python3
"""Tidy Sloth — a local-first file tidier that conserves energy.

Sloths move slowly because moving fast costs energy. This does the same:
cheap deterministic rules handle the obvious files, and a local open-weight
model is only woken up for the ones that are genuinely ambiguous.

    scan  → what is here?
    plan  → propose categories + cleaner names (writes plan.json + report.html)
    apply → execute the plan, write a manifest
    undo  → put everything back

Safety: never deletes. Dry-run by design. Every move is logged to a manifest
that `undo` replays in reverse.

Env: OLLAMA, MODEL, TIDY_DB (unused by default), CATEGORIES.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("MODEL", "llama3.2:1b")

# The model may only choose from this list. Anything else falls back to Other.
CATEGORIES = [
    "Invoices", "Receipts", "Certificates", "Resumes", "Screenshots",
    "Images", "Data", "Code", "Documents", "Notes", "Archives",
    "Installers", "Media", "Config", "Other",
]

# Cheap rules: extension → category. These cost zero model tokens.
EXT_RULES = {
    "png": "Images", "jpg": "Images", "jpeg": "Images", "webp": "Images",
    "gif": "Images", "svg": "Images", "bmp": "Images", "heic": "Images",
    "pdf": "Documents", "doc": "Documents", "docx": "Documents",
    "odt": "Documents", "rtf": "Documents", "epub": "Documents",
    "xlsx": "Data", "xls": "Data", "csv": "Data", "tsv": "Data",
    "json": "Data", "xml": "Data", "parquet": "Data", "db": "Data",
    "sqlite": "Data", "ipynb": "Data",
    "js": "Code", "ts": "Code", "py": "Code", "java": "Code", "c": "Code",
    "cpp": "Code", "rs": "Code", "go": "Code", "sh": "Code", "kt": "Code",
    "html": "Code", "css": "Code", "sql": "Code", "yml": "Code", "yaml": "Code",
    "md": "Notes", "txt": "Notes", "log": "Notes",
    "mermaid": "Data", "ics": "Data", "srt": "Media",
    "env": "Config", "local": "Config", "pem": "Config", "key": "Config",
    "conf": "Config", "cfg": "Config", "ini": "Config", "toml": "Config",
    "crt": "Config", "cer": "Config",
    "zip": "Archives", "tar": "Archives", "gz": "Archives", "7z": "Archives",
    "rar": "Archives", "bz2": "Archives", "xz": "Archives",
    "deb": "Installers", "rpm": "Installers", "appimage": "Installers",
    "exe": "Installers", "msi": "Installers", "dmg": "Installers",
    "mp4": "Media", "mkv": "Media", "mov": "Media", "webm": "Media",
    "mp3": "Media", "wav": "Media", "flac": "Media", "ogg": "Media",
    "m4a": "Media", "avi": "Media",
}

# Filenames that are clearly machine-generated and carry no information.
NOISE = re.compile(
    r"^(img|image|photo|screenshot|untitled|download|file|document|scan|"
    r"whatsapp[-_ ]?image|telegram[-_ ]?document|"
    r"\d{10,}.*|[\w-]{20,}_\d+)$", re.I)

# Purpose words beat the extension: an invoice PDF is not just "Documents".
PURPOSE_RULES = [
    ("Invoices", re.compile(r"invoice|bill|statement|tax[-_ ]?invoice", re.I)),
    ("Receipts", re.compile(r"receipt|order[-_ ]?confirm|payment[-_ ]?conf", re.I)),
    ("Resumes", re.compile(r"resume|curriculum|cv[-_ ]|biodata", re.I)),
    ("Certificates", re.compile(r"certificat|award|completion|diploma|badge", re.I)),
    ("Screenshots", re.compile(r"screen[-_ ]?shot|screenshot|grab", re.I)),
]

TDIR = ".tidysloth"


# --------------------------------------------------------------------------
# ollama
# --------------------------------------------------------------------------
def ollama_up() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
            return bool(json.load(r).get("models"))
    except Exception:
        return False


def ask_model(batch: list) -> dict:
    """Send a batch of ambiguous files, get {index: {category, name}} back."""
    lines = []
    for i, f in enumerate(batch):
        lines.append(f'{i}. "{f["name"]}" | .{f["ext"]} | {f["kb"]} KB')
    prompt = f"""You are tidying a Downloads folder.

Files:
{chr(10).join(lines)}

For each file choose ONE category from this exact list:
{", ".join(CATEGORIES)}

Also propose a clear, humans-readable filename stem (no extension, no path,
lowercase, words separated by hyphens, max 60 characters). Keep any brand,
person or project name already present. If the name is already clear, keep it.

Reply with JSON only:
{{"files":[{{"i":0,"category":"Documents","name":"project-proposal"}}]}}

One entry per file, same order, no explanation."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": 120 * len(batch) + 60,
                    "num_ctx": 4096},
    }
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        raw = json.load(r).get("message", {}).get("content", "")
    data = extract_json(raw)
    rows = data.get("files")
    # Positional only, and only if the model answered for every file.
    # Trusting a model-supplied index is how names leak across rows.
    if not isinstance(rows, list) or len(rows) != len(batch):
        return {}
    out = {}
    for j, row in enumerate(rows):
        if isinstance(row, dict):
            out[j] = (str(row.get("category", "")).strip(),
                      str(row.get("name", "")).strip())
    return out


def extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    if start < 0:
        return {}
    depth, end = 0, -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {}
    chunk = text[start:end]
    for cand in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
        try:
            v = json.loads(cand)
            return v if isinstance(v, dict) else {}
        except Exception:
            continue
    return {}


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------
def clean_stem(stem: str) -> str:
    s = stem.strip()
    # Drop leading message/photo IDs like "1786076876258_" or "1762857107887 ".
    # Deterministic noise removal, not invention — but only when a real name
    # survives the strip, or "4446563839" would become nothing.
    stripped = re.sub(r"^\d{10,}[_\-\s.]*", "", s)
    if stripped.strip("-_ ."):
        s = stripped
    s = re.sub(r"[_\s]+", "-", s)
    s = re.sub(r"[^A-Za-z0-9.\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s[:60].strip("-.")


def has_semantic_words(stem: str) -> bool:
    """Does this filename contain a word a human could actually use?"""
    stop = {"img", "image", "photo", "new", "copy", "final", "file",
            "document", "scan", "the", "and", "untitled", "download"}
    for w in re.split(r"[^A-Za-z]+", stem):
        if len(w) >= 3 and w.lower() not in stop:
            return True
    return False


KNOWN_EXT = set(EXT_RULES) | {"kra", "jfif", "heif", "avif", "webp"}


def _tokens(s: str) -> set:
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", s) if len(w) >= 3}


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def safe_model_name(name: str, src_stem: str) -> str:
    """Accept a model-proposed filename only when it cannot be wrong.

    A small model batching many filenames will happily attach one row's words
    to another row's file (SoulTree-brochure.pdf → "AC2.pdf"). So a proposal
    is only accepted if it is built from words that were already in the
    original name. Rename, do not invent.
    """
    if not name:
        return ""
    # strip every trailing extension the model may have echoed back
    n = name.strip()
    for _ in range(4):
        ext = n.rsplit(".", 1)[-1].lower() if "." in n else ""
        if ext in KNOWN_EXT:
            n = n.rsplit(".", 1)[0]
        else:
            break
    n = clean_stem(n)
    if not n:
        return ""
    # Never invent meaning for a name that had none to begin with.
    if not has_semantic_words(src_stem) and n.lower() != clean_stem(src_stem).lower():
        return ""
    # Provenance: the proposal must reuse the original's own words.
    same = bool(_tokens(n) & _tokens(src_stem))
    if not same:
        a, b = _compact(n), _compact(src_stem)
        same = bool(a and b and (a in b or b in a))
    if not same:
        return ""
    # Reject a name that is just another file's full name with an extension.
    if re.search(r"\.(" + "|".join(KNOWN_EXT) + r")$", n, re.I):
        return ""
    return n


def rule_verdict(f: dict):
    """Return (category, name, confident) using free rules only."""
    stem, ext, name = f["stem"], f["ext"], f["name"]

    for cat, rx in PURPOSE_RULES:
        if rx.search(name):
            return cat, clean_stem(re.sub(rx, "", stem)) or clean_stem(stem), True

    cat = EXT_RULES.get(ext)
    if not cat:
        return "Other", clean_stem(stem) or "file", False

    # A meaningful name is good enough on its own — no model needed.
    meaningful = (
        not NOISE.match(stem)
        and len(stem) >= 4
        and not re.fullmatch(r"[\d\-_. ]+", stem)
        and not re.search(r"(copy|\(\d\)|final-?final)", stem, re.I)
    )
    if meaningful:
        return cat, clean_stem(stem), True
    return cat, "", False


def inventory(root: Path) -> list:
    out = []
    for p in sorted(root.iterdir()):
        if p.name.startswith(".") or p.name == TDIR:
            continue
        if not p.is_file() or p.is_symlink():
            continue
        stat = p.stat()
        out.append({
            "path": str(p), "name": p.name,
            "stem": p.stem, "ext": p.suffix.lower().lstrip("."),
            "bytes": stat.st_size, "kb": round(stat.st_size / 1024),
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
        })
    return out


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def build_plan(root: Path, batch_size: int, limit: int | None,
               use_model: bool) -> dict:
    files = inventory(root)
    if limit:
        files = files[:limit]
    t0 = time.time()

    plan, ambiguous = [], []
    for f in files:
        cat, name, confident = rule_verdict(f)
        row = dict(f, category=cat, new_stem=name,
                   source="rule" if confident else "model")
        plan.append(row)
        if not confident:
            ambiguous.append(row)

    calls = 0
    if use_model and ambiguous and ollama_up():
        for i in range(0, len(ambiguous), batch_size):
            chunk = ambiguous[i:i + batch_size]
            try:
                verdicts = ask_model([{"name": c["name"], "ext": c["ext"],
                                       "kb": c["kb"]} for c in chunk])
            except urllib.error.URLError as e:
                print(f"  ! model unavailable ({e}); leaving {len(chunk)} files on rules",
                      file=sys.stderr)
                break
            calls += 1
            for j, c in enumerate(chunk):
                cat, name = verdicts.get(j, ("", ""))
                if cat in CATEGORIES:
                    c["category"] = cat
                safe = safe_model_name(name, c["stem"])
                if safe:
                    c["new_stem"] = safe

    # second safety net: model gave nothing → keep a usable name
    for c in plan:
        if not c["new_stem"]:
            c["new_stem"] = clean_stem(c["stem"]) or "file"

    # resolve collisions inside the plan
    used = set()
    for c in plan:
        ext = ("." + c["ext"]) if c["ext"] else ""
        dest = Path(c["category"]) / f'{c["new_stem"]}{ext}'
        n = 2
        while str(dest).lower() in used:
            dest = Path(c["category"]) / f'{c["new_stem"]}-{n}{ext}'
            n += 1
        used.add(str(dest).lower())
        c["dest"] = str(dest)

    by_rule = sum(1 for c in plan if c["source"] == "rule")
    return {
        "root": str(root),
        "created": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL if calls else None,
        "model_calls": calls,
        "seconds": round(time.time() - t0, 1),
        "files": plan,
        "stats": {
            "total": len(plan),
            "by_rule": by_rule,
            "by_model": len(plan) - by_rule,
            "model_skipped_pct": round(100 * by_rule / len(plan), 1) if plan else 0,
            # 8 s per file is a deliberately conservative manual estimate:
            # open it, decide a category, type a better name, drag it.
            "manual_minutes": round(len(plan) * 8 / 60),
        },
    }


def write_report(plan: dict, root: Path) -> Path:
    groups = defaultdict(list)
    for f in plan["files"]:
        groups[f["category"]].append(f)
    rows = []
    for cat in sorted(groups, key=lambda c: (-len(groups[c]), c)):
        rows.append(f'<h2>{html.escape(cat)} <span class="n">{len(groups[cat])}</span></h2><div class="grid">')
        for f in sorted(groups[cat], key=lambda x: x["dest"]):
            rows.append(
                f'<div class="item"><div class="src">{html.escape(f["name"])}</div>'
                f'<div class="arrow">→</div>'
                f'<div class="dst">{html.escape(f["dest"])}</div>'
                f'<div class="meta">{f["kb"]} KB · {html.escape(f["mtime"])} · {f["source"]}</div></div>')
        rows.append("</div>")
    st = plan["stats"]
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Tidy Sloth plan</title>
<style>
:root{{--bg:#0e1311;--ink:#e7efe9;--mut:#8fa39a;--moss:#7bcf9b;--faint:#1b2621}}
*{{box-sizing:border-box}}body{{margin:0;padding:34px 40px;background:var(--bg);color:var(--ink);
font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}}
h1{{font-size:26px;margin:0 0 6px}}h1 span{{color:var(--moss)}}
.sub{{color:var(--mut);margin-bottom:22px}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:26px}}
.stat{{border:1px solid var(--faint);background:#121a16;border-radius:12px;padding:12px 18px}}
.stat b{{display:block;font-size:22px;color:var(--moss)}}
.stat span{{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.07em}}
h2{{font-size:15px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);
margin:26px 0 10px;border-bottom:1px solid var(--faint);padding-bottom:8px}}
h2 .n{{color:var(--moss);font-size:13px;margin-left:6px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:8px}}
.item{{border:1px solid var(--faint);border-radius:10px;padding:9px 12px;background:#121a16;overflow:hidden}}
.src{{color:var(--mut);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.arrow{{color:var(--moss);font-size:11px;margin:1px 0}}
.dst{{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.meta{{color:#5f736a;font-size:11px;margin-top:3px}}
</style></head><body>
<h1>Tidy <span>Sloth</span> — proposed plan</h1>
<div class="sub">{html.escape(plan['root'])} · {st['total']} files · generated {plan['created']}</div>
<div class="stats">
  <div class="stat"><b>{st['total']}</b><span>files</span></div>
  <div class="stat"><b>{st['by_rule']}</b><span>sorted by rules</span></div>
  <div class="stat"><b>{st['by_model']}</b><span>needed the model</span></div>
  <div class="stat"><b>{st['model_skipped_pct']}%</b><span>model work avoided</span></div>
  <div class="stat"><b>{plan['seconds']}s</b><span>planning time</span></div>
  <div class="stat"><b>{('~' + str(st['manual_minutes'])) if st.get('manual_minutes') else '&lt;1'} min</b><span>manual sorting avoided</span></div>
</div>
{''.join(rows)}
</body></html>"""
    p = root / TDIR / "report.html"
    p.parent.mkdir(exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# apply / undo
# --------------------------------------------------------------------------
def apply_plan(plan_path: Path) -> None:
    plan = json.loads(plan_path.read_text())
    root = Path(plan["root"])
    moves = []
    for f in plan["files"]:
        src = Path(f["path"])
        if not src.exists():
            continue
        dest = root / f["dest"]
        dest.parent.mkdir(exist_ok=True)
        n = 2
        while dest.exists():
            dest = dest.with_name(f'{dest.stem}-{n}{dest.suffix}')
            n += 1
        shutil.move(str(src), str(dest))
        moves.append({"src": str(src), "dest": str(dest)})
        print(f"  {f['name']}  →  {f['dest']}")

    manifest = root / TDIR / f"manifest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text(json.dumps(
        {"root": str(root), "when": datetime.now().isoformat(timespec="seconds"),
         "moves": moves}, indent=2))
    print(f"\nMoved {len(moves)} files. Undo with:\n  python3 tidysloth.py undo {manifest}")


def undo(manifest_path: Path) -> None:
    data = json.loads(manifest_path.read_text())
    done = 0
    for m in reversed(data["moves"]):
        src, dest = Path(m["dest"]), Path(m["src"])
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            n = 2
            while dest.exists():
                dest = dest.with_name(f"{dest.stem}-restored-{n}{dest.suffix}")
                n += 1
            shutil.move(str(src), str(dest))
            done += 1
    # remove now-empty category folders we created
    for cat in CATEGORIES:
        d = Path(data["root"]) / cat
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    print(f"Restored {done} files to {data['root']}")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def cmd_scan(args):
    root = Path(args.folder).expanduser().resolve()
    files = inventory(root)
    if not files:
        print("Nothing loose in there. Peak sloth.")
        return
    ext = Counter(f["ext"] or "no extension" for f in files)
    size = sum(f["bytes"] for f in files) / 1e6
    print(f"{root}\n{len(files)} loose files · {size:.1f} MB\n")
    print("by type:")
    for e, n in ext.most_common(12):
        print(f"  {n:4d}  {e}")
    noisy = [f for f in files if NOISE.match(f["stem"])]
    print(f"\n{len(noisy)} files have machine-generated names (img.png, 1762…png)")
    print("Run `plan` to see where everything would go.")


def cmd_plan(args):
    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")
    if not args.no_model and not ollama_up():
        print("! Ollama is not reachable — planning with rules only.\n"
              "  (that is still a complete, working plan)")
    print(f"Planning {root} …")
    plan = build_plan(root, args.batch, args.limit, not args.no_model)
    (root / TDIR).mkdir(exist_ok=True)
    out = root / TDIR / "plan.json"
    out.write_text(json.dumps(plan, indent=2))
    report = write_report(plan, root)
    st = plan["stats"]
    print(f"\n  {st['total']} files")
    print(f"  {st['by_rule']} handled by rules, {st['by_model']} needed the model "
          f"({st['model_skipped_pct']}% avoided)")
    print(f"  {plan['model_calls']} model call(s), {plan['seconds']}s")
    mins = st["manual_minutes"]
    print(f"  ~{mins} min of manual sorting avoided" if mins
          else "  under a minute of manual sorting avoided")
    print(f"\n  plan:   {out}")
    print(f"  report: {report}   ← open this to review")
    print(f"\nApply with:\n  python3 tidysloth.py apply {out}")


def main():
    ap = argparse.ArgumentParser(
        prog="tidysloth",
        description="Tidy a messy folder with rules first and a local model second.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="summarise a folder without changing anything")
    s.add_argument("folder")
    s.set_defaults(func=cmd_scan)

    p = sub.add_parser("plan", help="propose categories and names")
    p.add_argument("folder")
    p.add_argument("--limit", type=int, default=None, help="only the first N files")
    p.add_argument("--batch", type=int, default=12, help="files per model call")
    p.add_argument("--no-model", action="store_true", help="rules only, zero tokens")
    p.set_defaults(func=cmd_plan)

    a = sub.add_parser("apply", help="execute a plan.json")
    a.add_argument("plan")
    a.set_defaults(func=lambda x: apply_plan(Path(x.plan)))

    u = sub.add_parser("undo", help="reverse a manifest")
    u.add_argument("manifest")
    u.set_defaults(func=lambda x: undo(Path(x.manifest)))

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
