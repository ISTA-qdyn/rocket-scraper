#!/usr/bin/env python3
"""
rocket_export.py -- archive your Rocket.Chat conversations before the server is retired.

Pulls every room you can see (public channels you joined, private groups, and
direct messages), the full message history of each, and every attached file
(pdf, png, ...). Writes each room as messages.json (complete) and chat.html
(readable, images inline, files linked).

Standard library only -- no pip install needed. Runs on Python 3.8+.

Usage:
    python3 rocket_export.py --server https://chat.ista.ac.at

You will be asked for your username and password (and a 2FA code if your
account uses two-factor). Nothing is stored; credentials are used once to get a
session token that lives only in memory for the run.

Alternatively, create a Personal Access Token in Rocket.Chat
(avatar -> My Account -> Personal Access Tokens) and pass it:

    python3 rocket_export.py --server https://chat.ista.ac.at \
        --user-id <your user id> --token <your token>

See README.md for details.
"""

import argparse
import getpass
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE = 100          # messages per API page
RETRY = 4           # retries on transient errors
TIMEOUT = 60        # seconds per request


# ----------------------------------------------------------------------------
# Low-level HTTP against the Rocket.Chat REST API
# ----------------------------------------------------------------------------

class Rocket:
    def __init__(self, server):
        self.server = server.rstrip("/")
        self.headers = {"User-Agent": "rocket-export/1.0"}

    def _url(self, path, params=None):
        url = self.server + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _request(self, path, params=None, method="GET", data=None):
        url = self._url(path, params)
        body = None
        headers = dict(self.headers)
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(RETRY):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                payload = e.read().decode(errors="replace")
                if e.code == 429:  # rate limited: back off and retry
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                # Return parsed error body so callers can inspect it (e.g. 2FA).
                try:
                    return json.loads(payload)
                except ValueError:
                    raise RuntimeError("HTTP %s on %s: %s" % (e.code, path, payload[:300]))
            except urllib.error.URLError as e:
                if attempt < RETRY - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("Network error on %s: %s" % (path, e))
        raise RuntimeError("Giving up on %s after %d retries" % (path, RETRY))

    def get(self, path, params=None):
        return self._request(path, params=params, method="GET")

    # -- authentication ------------------------------------------------------

    def login_password(self, user, password):
        data = {"user": user, "password": password}
        res = self._request("/api/v1/login", method="POST", data=data)
        if res.get("status") == "success":
            self._store_token(res["data"])
            return
        # Two-factor: the server tells us a code is required.
        err = (res.get("errorType") or res.get("error") or "")
        if "totp" in str(err).lower() or res.get("error") == "totp-required":
            code = input("Two-factor code (from your authenticator / email): ").strip()
            self.headers["X-2fa-code"] = code
            self.headers["X-2fa-method"] = "totp"
            res = self._request("/api/v1/login", method="POST", data=data)
            # Clean up so the 2FA headers are not sent on every later call.
            self.headers.pop("X-2fa-code", None)
            self.headers.pop("X-2fa-method", None)
            if res.get("status") == "success":
                self._store_token(res["data"])
                return
        raise RuntimeError("Login failed: %s" % json.dumps(res)[:300])

    def login_token(self, user_id, token):
        self.headers["X-User-Id"] = user_id
        self.headers["X-Auth-Token"] = token
        res = self.get("/api/v1/me")
        if not res.get("success", res.get("_id")):
            raise RuntimeError("Token rejected: %s" % json.dumps(res)[:300])
        return res

    def _store_token(self, data):
        self.headers["X-User-Id"] = data["userId"]
        self.headers["X-Auth-Token"] = data["authToken"]

    # -- room listing --------------------------------------------------------

    def list_all(self, path, key):
        """Page through an endpoint that returns {key: [...], total, offset}."""
        out, offset = [], 0
        while True:
            res = self.get(path, {"count": PAGE, "offset": offset})
            if res.get("success") is False:
                raise RuntimeError(res.get("error") or res.get("message") or json.dumps(res)[:200])
            items = res.get(key, [])
            out.extend(items)
            total = res.get("total", len(out))
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    # -- message history -----------------------------------------------------

    def history(self, path, room_id):
        """Full history of one room, oldest-first, via the `latest` cursor."""
        msgs, latest = [], None
        while True:
            params = {"roomId": room_id, "count": PAGE, "inclusive": "false"}
            if latest:
                params["latest"] = latest
            res = self.get(path, params)
            batch = res.get("messages", [])
            if not batch:
                break
            msgs.extend(batch)
            latest = batch[-1]["ts"]        # history returns newest-first
            if len(batch) < PAGE:
                break
        msgs.sort(key=lambda m: m.get("ts", ""))
        return msgs

    def download(self, path, dest):
        url = self.server + path
        req = urllib.request.Request(url, headers=self.headers)
        for attempt in range(RETRY):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                return True
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 ** attempt)
                    continue
                return False
            except urllib.error.URLError:
                if attempt < RETRY - 1:
                    time.sleep(2 ** attempt)
                    continue
                return False
        return False


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def safe_name(s):
    s = re.sub(r"[^\w.\-]+", "_", s or "unknown").strip("_")
    return s[:120] or "unnamed"


def file_refs(msg):
    """Collect (server_path, suggested_filename) for every file in a message."""
    refs = []
    seen = set()

    def add(fid, name):
        if not fid:
            return
        path = "/file-upload/%s/%s" % (fid, urllib.parse.quote(name or fid))
        if path not in seen:
            seen.add(path)
            refs.append((path, name or fid))

    for f in ([msg["file"]] if msg.get("file") else []) + (msg.get("files") or []):
        add(f.get("_id"), f.get("name"))
    for a in msg.get("attachments") or []:
        link = a.get("title_link") or a.get("image_url") or ""
        m = re.match(r"/file-upload/([^/]+)/(.+)", link)
        if m:
            name = urllib.parse.unquote(m.group(2))
            add(m.group(1), name)
    return refs


def render_html(room_label, msgs, file_map):
    """file_map: server_path -> local relative path (or None if download failed)."""
    def esc(s):
        return html.escape(s or "")

    rows = []
    for m in msgs:
        who = esc((m.get("u") or {}).get("username", "?"))
        ts = esc(m.get("ts", ""))
        text = esc(m.get("msg", "")).replace("\n", "<br>")
        body = [text] if text else []
        for path, name in file_refs(m):
            local = file_map.get(path)
            if local and re.search(r"\.(png|jpg|jpeg|gif|webp|bmp)$", name, re.I):
                body.append('<div><img src="%s" alt="%s"></div>' % (esc(local), esc(name)))
            elif local:
                body.append('<div>[file] <a href="%s">%s</a></div>' % (esc(local), esc(name)))
            else:
                body.append('<div>[file, not downloaded] %s</div>' % esc(name))
        rows.append(
            '<div class="m"><span class="t">%s</span> '
            '<span class="u">%s</span><div class="b">%s</div></div>'
            % (ts, who, "".join(body))
        )
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>%s</title>"
        "<style>body{font:14px/1.5 -apple-system,sans-serif;max-width:820px;"
        "margin:2rem auto;padding:0 1rem;color:#222}"
        ".m{padding:.4rem 0;border-bottom:1px solid #eee}"
        ".t{color:#999;font-size:12px}.u{font-weight:600;color:#0a5}"
        ".b{margin:.2rem 0 0}img{max-width:100%%;border-radius:6px;margin:.3rem 0}"
        "a{color:#06c}</style>"
        "<h1>%s</h1><p>%d messages</p>%s"
        % (esc(room_label), esc(room_label), len(msgs), "".join(rows))
    )


# ----------------------------------------------------------------------------
# Main export
# ----------------------------------------------------------------------------

# Rocket.Chat room types, from the `t` field on a subscription:
#   c = public channel, p = private group, d = direct message.
# Each maps to a kind label and its history endpoint.
TYPE_KIND = {"c": "channel", "p": "group", "d": "direct"}
TYPE_HIST = {
    "c": "/api/v1/channels.history",
    "p": "/api/v1/groups.history",
    "d": "/api/v1/im.history",
}


def list_subscriptions(rc):
    """Every room the user is subscribed to (sidebar), regardless of type.

    subscriptions.get is what the web client itself calls, so it works for a
    normal account even when the per-type list endpoints (groups.list, im.list)
    are restricted and return nothing.
    """
    res = rc.get("/api/v1/subscriptions.get")
    if res.get("success") is False:
        raise RuntimeError(res.get("error") or res.get("message") or json.dumps(res)[:200])
    return res.get("update", []) or []


def sub_label(sub):
    """Human name for a room. For a DM, `name`/`fname` is the counterpart."""
    return sub.get("fname") or sub.get("name") or sub.get("rid")


def export(rc, outdir, skip_files=False):
    os.makedirs(outdir, exist_ok=True)
    index = []

    subs = list_subscriptions(rc)
    by_type = {}
    for s in subs:
        by_type.setdefault(s.get("t"), []).append(s)
    for t in sorted(by_type):
        print("%s: %d room(s)" % (TYPE_KIND.get(t, t), len(by_type[t])))

    for sub in subs:
        t = sub.get("t")
        hist_path = TYPE_HIST.get(t)
        if not hist_path:
            continue  # livechat / other room types: skip
        kind = TYPE_KIND[t]
        room_id = sub.get("rid")
        label = sub_label(sub) or room_id
        prefix = "dm" if t == "d" else kind
        folder = os.path.join(outdir, "%s_%s" % (prefix, safe_name(label)))
        os.makedirs(folder, exist_ok=True)
        print("  - %s" % label, end=" ", flush=True)
        try:
            msgs = rc.history(hist_path, room_id)
        except RuntimeError as e:
            print("[history failed: %s]" % e)
            continue

        file_map = {}
        if not skip_files:
            fdir = os.path.join(folder, "files")
            for m in msgs:
                for path, name in file_refs(m):
                    if path in file_map:
                        continue
                    os.makedirs(fdir, exist_ok=True)
                    # Prefix the file id to avoid name collisions across messages.
                    fid = path.split("/")[2]
                    dest = os.path.join(fdir, safe_name(fid + "_" + name))
                    ok = rc.download(path, dest)
                    file_map[path] = os.path.relpath(dest, folder) if ok else None

        with open(os.path.join(folder, "messages.json"), "w") as f:
            json.dump(msgs, f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "chat.html"), "w") as f:
            f.write(render_html(label, msgs, file_map))

        n_files = sum(1 for v in file_map.values() if v)
        print("(%d msgs, %d files)" % (len(msgs), n_files))
        index.append({"kind": kind, "label": label, "id": room_id,
                      "messages": len(msgs), "files": n_files})

    with open(os.path.join(outdir, "index.json"), "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    total_m = sum(r["messages"] for r in index)
    total_f = sum(r["files"] for r in index)
    print("\nDone: %d rooms, %d messages, %d files -> %s"
          % (len(index), total_m, total_f, os.path.abspath(outdir)))


def main():
    ap = argparse.ArgumentParser(description="Archive Rocket.Chat conversations.")
    ap.add_argument("--server", required=True, help="e.g. https://chat.ista.ac.at")
    ap.add_argument("--out", default="rocket_archive", help="output directory")
    ap.add_argument("--user-id", help="Personal Access Token: your user id")
    ap.add_argument("--token", help="Personal Access Token value")
    ap.add_argument("--user", help="username or email (password login)")
    ap.add_argument("--skip-files", action="store_true",
                    help="export text only, do not download attachments")
    args = ap.parse_args()

    rc = Rocket(args.server)
    if args.token and args.user_id:
        me = rc.login_token(args.user_id, args.token)
        print("Authenticated as %s (token)" % me.get("username", args.user_id))
    else:
        user = args.user or input("Username or email: ").strip()
        password = getpass.getpass("Password: ")
        rc.login_password(user, password)
        print("Authenticated as %s" % user)

    export(rc, args.out, skip_files=args.skip_files)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
    except RuntimeError as e:
        sys.exit("Error: %s" % e)
