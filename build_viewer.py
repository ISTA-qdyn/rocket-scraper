#!/usr/bin/env python3
"""
build_viewer.py -- turn an archive made by rocket_export.py into one viewer.html.

Reads the archive folder (default: rocket_archive) and writes viewer.html at its
root: a sidebar listing every room, grouped into channels, groups, and direct
messages, with a search box; clicking a room shows its conversation with images
inline and other files linked.

The messages are embedded inside viewer.html, so it opens by double-clicking the
file, with no local server needed. Attached files are referenced by relative
path, so keep viewer.html at the top of the archive next to the room folders.

Usage:
    python3 build_viewer.py                 # uses ./rocket_archive
    python3 build_viewer.py path/to/archive

Standard library only.
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse

IMG_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)$", re.I)


def safe_name(s):
    s = re.sub(r"[^\w.\-]+", "_", s or "unknown").strip("_")
    return s[:120] or "unnamed"


def file_refs(msg):
    """Same file discovery as the exporter: (file_id, filename) per attachment."""
    refs, seen = [], set()

    def add(fid, name):
        if fid and fid not in seen:
            seen.add(fid)
            refs.append((fid, name or fid))

    for f in ([msg["file"]] if msg.get("file") else []) + (msg.get("files") or []):
        add(f.get("_id"), f.get("name"))
    for a in msg.get("attachments") or []:
        link = a.get("title_link") or a.get("image_url") or ""
        m = re.match(r"/file-upload/([^/]+)/(.+)", link)
        if m:
            add(m.group(1), urllib.parse.unquote(m.group(2)))
    return refs


def fmt_ts(ts):
    """ISO timestamp -> readable local-ish string; fall back to the raw value."""
    if not ts:
        return ""
    try:
        t = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(t)
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def collect(archive):
    rooms = []
    for entry in sorted(os.listdir(archive)):
        folder = os.path.join(archive, entry)
        mj = os.path.join(folder, "messages.json")
        if not os.path.isfile(mj):
            continue
        with open(mj) as f:
            msgs = json.load(f)
        fdir = os.path.join(folder, "files")
        existing = set(os.listdir(fdir)) if os.path.isdir(fdir) else set()
        kind, _, label = entry.partition("_")
        if kind == "dm":            # exporter names DM folders dm_<name>
            kind = "direct"
        label = label or entry

        out = []
        for m in msgs:
            files = []
            for fid, name in file_refs(m):
                fn = safe_name(fid + "_" + name)
                if fn in existing:
                    files.append({
                        "n": name,
                        "p": entry + "/files/" + fn,   # relative to archive root
                        "img": bool(IMG_RE.search(name)),
                    })
                else:
                    files.append({"n": name, "p": None, "img": False})
            out.append({
                "t": fmt_ts(m.get("ts", "")),
                "u": (m.get("u") or {}).get("username", "?"),
                "m": m.get("msg", ""),
                "f": files,
            })
        rooms.append({"kind": kind, "label": label, "n": len(out), "msgs": out})
    return rooms


PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rocket.Chat archive</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 -apple-system, system-ui, sans-serif; color: #1a1a1a; }
  #app { display: flex; height: 100vh; }
  #side { width: 300px; flex: 0 0 300px; border-right: 1px solid #e2e2e2;
          display: flex; flex-direction: column; background: #fafafa; }
  #search { margin: 10px; padding: 7px 9px; border: 1px solid #ccc; border-radius: 6px; font: inherit; }
  #list { overflow-y: auto; flex: 1; }
  .grp { padding: 10px 12px 4px; font-size: 11px; letter-spacing: .05em;
         text-transform: uppercase; color: #999; }
  .room { padding: 6px 12px; cursor: pointer; display: flex; justify-content: space-between; gap: 8px; }
  .room:hover { background: #eee; }
  .room.sel { background: #dde9ff; }
  .room .cnt { color: #aaa; font-size: 12px; }
  #main { flex: 1; overflow-y: auto; padding: 0 24px 40px; }
  #head { position: sticky; top: 0; background: #fff; padding: 16px 0 10px;
          border-bottom: 1px solid #eee; margin-bottom: 8px; }
  #head h2 { margin: 0; font-size: 18px; }
  #head .sub { color: #999; font-size: 12px; }
  .m { padding: 6px 0; border-bottom: 1px solid #f2f2f2; }
  .m .t { color: #aaa; font-size: 12px; }
  .m .u { font-weight: 600; color: #0a7; margin-left: 4px; }
  .m .b { margin-top: 2px; white-space: pre-wrap; word-wrap: break-word; }
  .m img { max-width: 460px; max-height: 460px; display: block; margin: 6px 0;
           border-radius: 6px; border: 1px solid #eee; }
  .m a.file { color: #06c; }
  .empty { color: #999; padding: 40px 0; }
</style>
<div id="app">
  <div id="side">
    <input id="search" placeholder="Filter rooms...">
    <div id="list"></div>
  </div>
  <div id="main"><div class="empty">Select a conversation on the left.</div></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const ROOMS = JSON.parse(document.getElementById('data').textContent);
const KINDS = [['channel','Channels'],['group','Groups'],['direct','Direct messages']];
const listEl = document.getElementById('list');
const mainEl = document.getElementById('main');
let selected = -1;

function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}

function buildList(filter) {
  listEl.innerHTML = '';
  const f = (filter || '').toLowerCase();
  KINDS.forEach(([kind, title]) => {
    const rooms = ROOMS
      .map((r, i) => ({ r, i }))
      .filter(x => x.r.kind === kind && x.r.label.toLowerCase().includes(f));
    if (!rooms.length) return;
    listEl.appendChild(el('div', 'grp', title));
    rooms.forEach(({ r, i }) => {
      const row = el('div', 'room' + (i === selected ? ' sel' : ''));
      row.appendChild(el('span', 'name', r.label));
      row.appendChild(el('span', 'cnt', r.n));
      row.onclick = () => show(i);
      listEl.appendChild(row);
    });
  });
}

function show(i) {
  selected = i;
  const r = ROOMS[i];
  buildList(document.getElementById('search').value);
  mainEl.innerHTML = '';
  const head = el('div', 'head'); head.id = 'head';
  head.appendChild(el('h2', null, r.label));
  head.appendChild(el('div', 'sub', r.kind + ' · ' + r.n + ' messages'));
  mainEl.appendChild(head);
  r.msgs.forEach(m => {
    const box = el('div', 'm');
    const meta = el('div');
    meta.appendChild(el('span', 't', m.t));
    meta.appendChild(el('span', 'u', '@' + m.u));
    box.appendChild(meta);
    if (m.m) box.appendChild(el('div', 'b', m.m));
    (m.f || []).forEach(file => {
      if (file.p && file.img) {
        const img = el('img'); img.src = file.p; img.alt = file.n; img.loading = 'lazy';
        box.appendChild(img);
      } else if (file.p) {
        const a = el('a', 'file', '📎 ' + file.n);
        a.href = file.p; a.target = '_blank';
        const d = el('div'); d.appendChild(a); box.appendChild(d);
      } else {
        box.appendChild(el('div', 'file', '[file, not downloaded] ' + file.n));
      }
    });
    mainEl.appendChild(box);
  });
  mainEl.scrollTop = 0;
}

document.getElementById('search').addEventListener('input', e => buildList(e.target.value));
buildList('');
</script>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Build viewer.html for a Rocket.Chat archive.")
    ap.add_argument("archive", nargs="?", default="rocket_archive",
                    help="archive folder (default: rocket_archive)")
    args = ap.parse_args()

    if not os.path.isdir(args.archive):
        sys.exit("No such archive folder: %s" % args.archive)

    rooms = collect(args.archive)
    if not rooms:
        sys.exit("No rooms found in %s (expected folders with messages.json)." % args.archive)

    data = json.dumps(rooms, ensure_ascii=False)
    # Guard against the embedded JSON prematurely closing the <script> tag.
    data = data.replace("</", "<\\/")
    out = os.path.join(args.archive, "viewer.html")
    with open(out, "w") as f:
        f.write(PAGE.replace("__DATA__", data))

    total = sum(r["n"] for r in rooms)
    print("Wrote %s (%d rooms, %d messages)." % (out, len(rooms), total))
    print("Open it in a browser:  open '%s'" % out)


if __name__ == "__main__":
    main()
