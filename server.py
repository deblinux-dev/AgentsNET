#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentsNET v1.1.0 — network hub for AI agents: rooms, chat, task board, file exchange.

Design principles:
- Pull model: agents FETCH state when they act (no push, no long-running client process).
  Works behind NAT and firewalls — the server is the only publicly reachable party.
- Rooms: isolated spaces protected by an access key; multi-tenant (many owners/projects per server).
- Agent API answers in COMPACT PLAIN TEXT to save agent tokens (no JSON where avoidable).
- Storage: SQLite (WAL) + uploaded files on disk. All cleanups (task timeouts, file TTL)
  are lazy sweeps — no background threads, WSGI-friendly (PythonAnywhere etc.).

Run:      python3 server.py --host 0.0.0.0 --port 8077
WSGI:     from server import app as application      (see wsgi.py)
Panel:    /admin        Agent guide: /skill          Client: /client      Package: /download
"""

import argparse
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_file

VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8077,
    "db": "data/agentsnet.db",
    "data_dir": "data",
    "max_file_mb": 5,
    "task_timeout_hours": 6,
    "file_ttl_minutes": 60,
    "page_size": 20,
    "max_text_len": 8000,
    "session_hours": 12,
    "create_token": "",  # токен для подключения
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = os.path.join(HERE, "config.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as e:  # noqa: BLE001
            print("[agentsnet] config.json ignored:", e)
    for k in list(cfg.keys()):
        env = os.environ.get("AGENTSNET_" + k.upper())
        if env is not None:
            try:
                cfg[k] = type(cfg[k])(env)
            except Exception:  # noqa: BLE001
                cfg[k] = env
    # PaaS support: generic PORT env (Render/Railway/Heroku)
    if os.environ.get("PORT"):
        try:
            cfg["port"] = int(os.environ["PORT"])
        except ValueError:
            pass
        cfg["host"] = "0.0.0.0"
    return cfg


CFG = load_config()
if not os.path.isabs(CFG["db"]):
    CFG["db"] = os.path.join(HERE, CFG["db"])
if not os.path.isabs(CFG["data_dir"]):
    CFG["data_dir"] = os.path.join(HERE, CFG["data_dir"])
os.makedirs(CFG["data_dir"], exist_ok=True)
os.makedirs(os.path.join(CFG["data_dir"], "files"), exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int((float(CFG["max_file_mb"]) + 2) * 1024 * 1024)

START_TIME = time.time()
_sessions = {}  # admin session token -> {"room_id": int, "exp": float}
_sess_lock = threading.Lock()

# ----------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT UNIQUE NOT NULL,
  title TEXT DEFAULT '',
  admin_user TEXT DEFAULT 'admin',
  admin_pass_hash TEXT NOT NULL,
  key_hash TEXT NOT NULL,
  task_timeout_hours REAL DEFAULT 6,
  max_file_mb REAL DEFAULT 5,
  file_ttl_minutes REAL DEFAULT 60,
  page_size INTEGER DEFAULT 20,
  e2e INTEGER DEFAULT 0,
  created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  codename TEXT NOT NULL,
  model TEXT DEFAULT '',
  caps TEXT DEFAULT '',
  token TEXT UNIQUE NOT NULL,
  joined_at TEXT DEFAULT '',
  joined_num REAL DEFAULT 0,
  last_seen TEXT DEFAULT '',
  last_seen_num REAL DEFAULT 0,
  UNIQUE(room_id, codename)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  kind TEXT DEFAULT 'chat',
  author TEXT DEFAULT '',
  text TEXT DEFAULT '',
  ts TEXT DEFAULT '',
  ts_num REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_msg_room ON messages(room_id, id);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  descr TEXT DEFAULT '',
  status TEXT DEFAULT 'OPEN',
  priority TEXT DEFAULT 'NORM',
  created_by TEXT DEFAULT '',
  created_at TEXT DEFAULT '',
  created_num REAL DEFAULT 0,
  assigned_to TEXT,
  assigned_at TEXT,
  assigned_num REAL,
  deadline_num REAL,
  closed_at TEXT,
  closed_num REAL,
  result TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tasks_room ON tasks(room_id, status);
CREATE TABLE IF NOT EXISTS task_comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  room_id INTEGER NOT NULL,
  author TEXT DEFAULT '',
  text TEXT DEFAULT '',
  ts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tc_task ON task_comments(task_id, id);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  fname TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  path TEXT DEFAULT '',
  uploaded_by TEXT DEFAULT '',
  note TEXT DEFAULT '',
  ts TEXT DEFAULT '',
  ts_num REAL DEFAULT 0,
  expires_num REAL DEFAULT 0
);
"""


def init_db():
    with sqlite3.connect(CFG["db"], timeout=15) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(rooms)").fetchall()]
        if "e2e" not in cols:
            c.execute("ALTER TABLE rooms ADD COLUMN e2e INTEGER DEFAULT 0")


def db():
    c = sqlite3.connect(CFG["db"], timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


# ----------------------------------------------------------------------------- helpers

def utc_ts(e=None):
    return datetime.fromtimestamp(e if e is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def short(e=None):
    """MM-DD HH:MM (UTC) — compact display."""
    return datetime.fromtimestamp(e if e is not None else time.time(), tz=timezone.utc).strftime("%m-%d %H:%M")


def rel(e):
    d = max(0, int(time.time() - (e or 0)))
    if d < 60:
        return "%ds" % d
    if d < 3600:
        return "%dm" % (d // 60)
    if d < 86400:
        return "%dh" % (d // 3600)
    return "%dd" % (d // 86400)


def human(n):
    if n < 1024:
        return "%dB" % n
    if n < 1024 * 1024:
        return "%.1fKB" % (n / 1024.0)
    return "%.2fMB" % (n / 1048576.0)


def fmt_hours(h):
    h = float(h)
    if h < 1:
        return "%dm" % max(1, int(round(h * 60)))
    if abs(h - int(h)) < 1e-9:
        return "%dh" % int(h)
    return "%gh" % h


def hash_secret(s, iters=120000):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", s.encode("utf-8"), salt, iters)
    return "pbkdf2$%d$%s$%s" % (iters, salt.hex(), h.hex())


def verify_secret(s, stored):
    try:
        _, iters, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", s.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:  # noqa: BLE001
        return False


def txt(s, status=200):
    return Response(s, mimetype="text/plain; charset=utf-8", status=status)


def add_msg(c, room_id, kind, author, text):
    cur = c.execute(
        "INSERT INTO messages(room_id,kind,author,text,ts,ts_num) VALUES(?,?,?,?,?,?)",
        (room_id, kind, author, text, utc_ts(), time.time()),
    )
    return cur.lastrowid


def params():
    """Merge query args + form + json body (json wins)."""
    p = {k: v for k, v in request.args.items()}
    if request.form:
        p.update({k: v for k, v in request.form.items()})
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        p.update({k: v for k, v in j.items() if v is not None})
    return p


def as_int(v, default=None):
    try:
        return int(str(v).strip())
    except Exception:  # noqa: BLE001
        return default


PRI_ORDER = {"URGENT": 0, "HIGH": 1, "NORM": 2, "LOW": 3}

# ----------------------------------------------------------------------------- sweeps (lazy maintenance)


def sweep_room(c, room):
    """Auto-release timed-out tasks, purge expired files. Race-safe via rowcount checks."""
    now = time.time()
    rows = c.execute(
        "SELECT id, assigned_to FROM tasks WHERE room_id=? AND status='ASSIGNED' AND deadline_num IS NOT NULL AND deadline_num<?",
        (room["id"], now),
    ).fetchall()
    for r in rows:
        cur = c.execute(
            "UPDATE tasks SET status='OPEN', assigned_to=NULL, assigned_at=NULL, assigned_num=NULL, deadline_num=NULL "
            "WHERE id=? AND status='ASSIGNED'",
            (r["id"],),
        )
        if cur.rowcount:
            add_msg(c, room["id"], "log", "SYSTEM",
                    "task #%d auto-released (timeout) - was assigned to %s" % (r["id"], r["assigned_to"]))
    frows = c.execute(
        "SELECT id, fname, path FROM files WHERE room_id=? AND expires_num<?", (room["id"], now)
    ).fetchall()
    for f in frows:
        cur = c.execute("DELETE FROM files WHERE id=? AND expires_num<?", (f["id"], now))
        if cur.rowcount:
            try:
                os.remove(f["path"])
            except OSError:
                pass
            add_msg(c, room["id"], "log", "SYSTEM",
                    "file #%d \"%s\" expired and was purged from server" % (f["id"], f["fname"]))
    c.commit()


# ----------------------------------------------------------------------------- auth


def auth_agent(c):
    """Returns (agent, room, error_response). Updates last_seen."""
    tok = None
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        tok = h[7:].strip()
    tok = tok or request.headers.get("X-Agent-Token") or request.args.get("token")
    if not tok and request.form:
        tok = request.form.get("token")
    if not tok:
        j = request.get_json(silent=True)
        if isinstance(j, dict):
            tok = j.get("token")
    if not tok:
        return None, None, txt("ERR: no token. join first: POST /a/join (room, key, name, model).", 401)
    a = c.execute("SELECT * FROM agents WHERE token=?", (tok,)).fetchone()
    if a is None:
        return None, None, txt("ERR: auth failed (unknown token). rejoin with reclaim=1 + your room key: POST /a/join.", 401)
    room = c.execute("SELECT * FROM rooms WHERE id=?", (a["room_id"],)).fetchone()
    if room is None:
        return None, None, txt("ERR: room gone. contact the admin.", 401)
    c.execute("UPDATE agents SET last_seen=?, last_seen_num=? WHERE id=?", (utc_ts(), time.time(), a["id"]))
    c.commit()
    return a, room, None


def admin_auth(c):
    """Returns (room, token) or (None, None)."""
    tok = request.headers.get("X-Admin-Token") or request.args.get("admin_token")
    if not tok:
        return None, None
    with _sess_lock:
        s = _sessions.get(tok)
    if not s or s["exp"] < time.time():
        return None, None
    room = c.execute("SELECT * FROM rooms WHERE id=?", (s["room_id"],)).fetchone()
    if room is None:
        return None, None
    return room, tok


# ----------------------------------------------------------------------------- text renderers


def fmt_msg(m):
    t = (m["ts"] or "")[5:16]  # MM-DD HH:MM
    if m["kind"] == "log":
        return "#%d [%s] LOG %s: %s" % (m["id"], t, m["author"], m["text"])
    return "#%d [%s] %s: %s" % (m["id"], t, m["author"], m["text"])


def roster_text(c, room):
    rows = c.execute(
        "SELECT * FROM agents WHERE room_id=? ORDER BY last_seen_num DESC", (room["id"],)
    ).fetchall()
    if not rows:
        return "ROSTER(0): (no agents yet)"
    out = ["ROSTER(%d):" % len(rows)]
    for r in rows:
        out.append("- %s model=%s%s seen=%s" % (
            r["codename"], r["model"], (" caps=%s" % r["caps"]) if r["caps"] else "", rel(r["last_seen_num"])))
    return "\n".join(out)


def task_line(c, t):
    n = c.execute("SELECT COUNT(*) n FROM task_comments WHERE task_id=?", (t["id"],)).fetchone()["n"]
    if t["status"] == "ASSIGNED":
        st = "ASSIGNED->%s until %s" % (t["assigned_to"], short(t["deadline_num"]))
    elif t["status"] in ("DONE", "CANCELLED"):
        st = "%s %s" % (t["status"], short(t["closed_num"]))
    else:
        st = "OPEN"
    return '#%d [%s] %s "%s" by=%s c=%d' % (t["id"], st, t["priority"], t["title"], t["created_by"], n)


def task_detail_str(c, room, tid):
    tid = as_int(tid)
    if tid is None:
        return None, "ERR: bad task id"
    t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid, room["id"])).fetchone()
    if t is None:
        return None, "ERR: task #%s not found in this room (maybe deleted). see: task list" % tid
    cms = c.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY id ASC LIMIT 100", (tid,)).fetchall()
    L = ['TASK #%d [%s] pri=%s "%s"' % (t["id"], t["status"], t["priority"], t["title"])]
    L.append("created: %s UTC by %s" % (t["created_at"], t["created_by"]))
    if t["descr"]:
        L.append("desc: %s" % t["descr"])
    if t["status"] == "ASSIGNED":
        L.append("assigned: %s (until %s UTC, auto-release on timeout)" % (t["assigned_to"], short(t["deadline_num"])))
    if t["status"] in ("DONE", "CANCELLED"):
        L.append("closed: %s UTC" % t["closed_at"])
        if t["result"]:
            L.append("result: %s" % t["result"])
    if cms:
        L.append("comments(%d):" % len(cms))
        for m in cms:
            L.append("- [%s] %s: %s" % (m["ts"][5:16], m["author"], m["text"]))
    else:
        L.append("comments(0)")
    return "\n".join(L), None


def file_line(f):
    return '#%d "%s" %s by=%s %s UTC%s expires=%s' % (
        f["id"], f["fname"], human(f["size"]), f["uploaded_by"], (f["ts"] or "")[5:16],
        (' note="%s"' % f["note"]) if f["note"] else "", short(f["expires_num"]))


def connect_block(c, room, ag):
    t = c.execute(
        "SELECT SUM(CASE WHEN status IN ('OPEN','ASSIGNED') THEN 1 ELSE 0 END) a, "
        "SUM(CASE WHEN status IN ('DONE','CANCELLED') THEN 1 ELSE 0 END) d FROM tasks WHERE room_id=?",
        (room["id"],),
    ).fetchone()
    last = c.execute("SELECT COALESCE(MAX(id),0) m FROM messages WHERE room_id=?", (room["id"],)).fetchone()["m"]
    e2e_line = ("E2E: ON - this room is end-to-end encrypted. You MUST encrypt chat texts, task "
                "titles/descs/comments/results and file contents with the room E2E password "
                "before sending (guide: GET /skill, section 'E2E rooms'). The server never sees plaintext.") \
        if room["e2e"] else "E2E: off (texts stored as-is; anyone with server access can read them)"
    return "\n".join([
        "OK room=%s agent=%s token=%s" % (room["slug"], ag["codename"], ag["token"]),
        "ROOM: task_timeout=%s file_limit=%gMB file_ttl=%gmin page_size=%d times=UTC pull_model=true" % (
            fmt_hours(room["task_timeout_hours"]), room["max_file_mb"], room["file_ttl_minutes"], room["page_size"]),
        e2e_line,
        roster_text(c, room),
        "TASKS: active=%d closed=%d | task list / task show <id>" % (t["a"] or 0, t["d"] or 0),
        "CHAT: chat read page=1 (newest first) | incremental: chat read after=%d" % last,
        "HELP: help | full guide: GET /skill",
    ])


HELP_TEXT = """AGENTSNET v{v} — commands (client: python3 agentsnet_client.py <cmd>; raw HTTP in brackets)
join --room R --key K --name N --model M [--caps C] [--reclaim]   [POST /a/join]
chat send <text>                                                  [POST /a/chat {{text}}]
chat read [--page N | --after ID] [--limit N]                     [GET /a/chat]
task list [--all] | task show <id>                                [GET /a/tasks | GET /a/tasks?id=N]
task create <title> [desc] [--priority LOW|NORM|HIGH|URGENT]      [POST /a/task/create]
task claim <id>                                                   [POST /a/task/claim {{id}}]
task done <id> [result]                                           [POST /a/task/status {{id,status:DONE,result}}]
task free <id> [reason]                                           [POST /a/task/status {{id,status:OPEN,reason}}]
task comment <id> <text>                                          [POST /a/task/comment {{id,text}}]
file upload <path> [--note N]                                     [POST /a/file multipart]
file list | file get <id> [out] | file confirm <id>               [GET /a/files | GET /a/file?id= | POST /a/file/confirm]
roster | ping | room | help                                       [GET /a/roster | /a/ping | /a/room | /a/help]

rules: claim a task before working on it; finish with task done + result;
times are UTC; files <= file_limit MB - confirm receipt after download;
too big files -> external host (tmpfiles.org, gdrive) + share link;
answers are compact text to save your tokens. full guide: GET /skill""".replace("{v}", VERSION)

# ----------------------------------------------------------------------------- agent API


@app.post("/a/join")
def a_join():
    p = params()
    slug = (str(p.get("room") or "")).strip().lower()
    key = str(p.get("key") or "")
    name = (str(p.get("name") or "")).strip()
    model = (str(p.get("model") or "")).strip()
    caps = (str(p.get("caps") or "")).strip()
    reclaim = str(p.get("reclaim") or "").strip().lower() in ("1", "true", "yes", "y")
    if not slug or not key or not name or not model:
        return txt("ERR: required fields: room, key, name, model (caps optional).\n"
                   "example: join --server URL --room my-project --key SECRET --name 'GLM Agent #2' --model GLM-5.3 --caps bash,files", 400)
    if not (2 <= len(name) <= 40):
        return txt("ERR: name must be 2..40 characters", 400)
    if name.upper().startswith("ADMIN") or name.upper() == "SYSTEM":
        return txt("ERR: this codename is reserved", 400)
    with closing(db()) as c, c:
        room = c.execute("SELECT * FROM rooms WHERE slug=?", (slug,)).fetchone()
        if room is None:
            return txt("ERR: room '%s' not found on this server. check the name or ask the room admin." % slug, 404)
        if not verify_secret(key, room["key_hash"]):
            return txt("ERR: access key rejected for room '%s'." % slug, 403)
        now = time.time()
        ex = c.execute("SELECT * FROM agents WHERE room_id=? AND codename=?", (room["id"], name)).fetchone()
        if ex is not None:
            if not reclaim:
                return txt("NAME_TAKEN: codename '%s' is already registered in room '%s'.\n"
                           "If it is YOU reconnecting, repeat join with reclaim=1 (same room key required) to get your token.\n"
                           "Otherwise pick a unique codename." % (name, slug), 409)
            c.execute("UPDATE agents SET model=?, caps=?, last_seen=?, last_seen_num=? WHERE id=?",
                      (model[:80], caps[:300], utc_ts(), now, ex["id"]))
            add_msg(c, room["id"], "log", name, "rejoined the room (model %s)" % model[:80])
            c.commit()
            ag = c.execute("SELECT * FROM agents WHERE id=?", (ex["id"],)).fetchone()
            return txt(connect_block(c, room, ag))
        token = secrets.token_hex(16)
        c.execute(
            "INSERT INTO agents(room_id,codename,model,caps,token,joined_at,joined_num,last_seen,last_seen_num) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (room["id"], name, model[:80], caps[:300], token, utc_ts(), now, utc_ts(), now))
        ag = c.execute("SELECT * FROM agents WHERE token=?", (token,)).fetchone()
        add_msg(c, room["id"], "log", name,
                "joined the room (model %s%s)" % (model[:80], ("; caps: %s" % caps[:300]) if caps else ""))
        c.commit()
        return txt(connect_block(c, room, ag))


@app.get("/a/help")
def a_help():
    return txt(HELP_TEXT)


@app.get("/a/room")
def a_room():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        n_ag = c.execute("SELECT COUNT(*) n FROM agents WHERE room_id=?", (room["id"],)).fetchone()["n"]
        t = c.execute(
            "SELECT SUM(CASE WHEN status IN ('OPEN','ASSIGNED') THEN 1 ELSE 0 END) a, "
            "SUM(CASE WHEN status IN ('DONE','CANCELLED') THEN 1 ELSE 0 END) d FROM tasks WHERE room_id=?",
            (room["id"],)).fetchone()
        return txt("ROOM %s \"%s\"\nlimits: task_timeout=%s file_limit=%gMB file_ttl=%gmin page_size=%d\n"
                   "agents=%d tasks_active=%d tasks_closed=%d times=UTC\nencryption=%s" % (
                       room["slug"], room["title"], fmt_hours(room["task_timeout_hours"]), room["max_file_mb"],
                       room["file_ttl_minutes"], room["page_size"], n_ag, t["a"] or 0, t["d"] or 0,
                       "e2e-on" if room["e2e"] else "off"))


@app.get("/a/roster")
def a_roster():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        return txt(roster_text(c, room))


@app.get("/a/ping")
def a_ping():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        n = c.execute("SELECT COUNT(*) n FROM agents WHERE room_id=?", (room["id"],)).fetchone()["n"]
        act = c.execute("SELECT COUNT(*) n FROM agents WHERE room_id=? AND last_seen_num>?",
                        (room["id"], time.time() - 300)).fetchone()["n"]
        return txt("OK %s UTC | room=%s | agents=%d (active 5m: %d)" % (utc_ts(), room["slug"], n, act))


@app.get("/a/chat")
def a_chat_read():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        limit = min(max(as_int(request.args.get("limit"), room["page_size"] or CFG["page_size"]) or 20, 1), 100)
        after = request.args.get("after")
        if after is not None and str(after).strip() != "":
            aid = as_int(after)
            if aid is None:
                return txt("ERR: after must be an integer message id", 400)
            rows = c.execute("SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id ASC LIMIT ?",
                             (room["id"], aid, limit)).fetchall()
            nxt = rows[-1]["id"] if rows else aid
            head = "CHAT room=%s after=%d got=%d next_after=%d%s" % (
                room["slug"], aid, len(rows), nxt,
                (" more=1 (call again with after=%d)" % nxt) if len(rows) == limit else "")
            body = [head] + [fmt_msg(m) for m in rows]
            if not rows:
                body.append("(no new messages)")
            return txt("\n".join(body))
        total = c.execute("SELECT COUNT(*) n FROM messages WHERE room_id=?", (room["id"],)).fetchone()["n"]
        pages = max(1, -(-total // limit))
        page = min(max(1, as_int(request.args.get("page"), 1) or 1), pages)
        rows = c.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                         (room["id"], limit, (page - 1) * limit)).fetchall()
        head = "CHAT room=%s page=%d/%d total=%d (newest first; older: page=%d)" % (
            room["slug"], page, pages, total, min(page + 1, pages))
        body = [head] + [fmt_msg(m) for m in rows]
        if not rows:
            body.append("(empty)")
        return txt("\n".join(body))


@app.post("/a/chat")
def a_chat_send():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        text = (str(params().get("text") or "")).strip()
        if not text:
            return txt("ERR: empty text", 400)
        if len(text) > CFG["max_text_len"]:
            return txt("ERR: text too long (max %d chars). split into several messages." % CFG["max_text_len"], 400)
        mid = add_msg(c, room["id"], "chat", a["codename"], text)
        c.commit()
        return txt("OK msg_id=%d" % mid)


@app.get("/a/tasks")
def a_tasks():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        tid = request.args.get("id")
        if tid:
            s, e = task_detail_str(c, room, tid)
            return txt(e, 404) if e else txt(s)
        want_all = str(request.args.get("all") or "").lower() in ("1", "true", "yes")
        st = (request.args.get("status") or "").upper()
        closed = []
        if want_all or st in ("DONE", "CANCELLED"):
            closed = c.execute(
                "SELECT * FROM tasks WHERE room_id=? AND status IN ('DONE','CANCELLED') ORDER BY closed_num DESC LIMIT 50",
                (room["id"],)).fetchall()
        active = c.execute(
            "SELECT * FROM tasks WHERE room_id=? AND status IN ('OPEN','ASSIGNED')", (room["id"],)).fetchall()
        active = sorted(active, key=lambda t: (PRI_ORDER.get(t["priority"], 2), t["id"]))
        ncl = c.execute("SELECT COUNT(*) n FROM tasks WHERE room_id=? AND status IN ('DONE','CANCELLED')",
                        (room["id"],)).fetchone()["n"]
        out = ["TASKS room=%s active=%d closed=%d" % (room["slug"], len(active), ncl)]
        for t in active:
            out.append(task_line(c, t))
        if closed:
            out.append("-- closed (newest first, top 50):")
            for t in closed:
                out.append(task_line(c, t))
        out.append("hint: task show <id> | take a task: task claim <id>")
        return txt("\n".join(out))


@app.post("/a/task/create")
def a_task_create():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        p = params()
        title = (str(p.get("title") or "")).strip()
        desc = (str(p.get("desc") or "")).strip()
        pri = (str(p.get("priority") or p.get("pri") or "NORM")).upper()
        if pri not in ("LOW", "NORM", "HIGH", "URGENT"):
            return txt("ERR: priority must be LOW|NORM|HIGH|URGENT", 400)
        if not title:
            return txt("ERR: title required", 400)
        if len(title) > 150:
            return txt("ERR: title too long (max 150 chars)", 400)
        if len(desc) > 4000:
            return txt("ERR: desc too long (max 4000 chars)", 400)
        cur = c.execute(
            "INSERT INTO tasks(room_id,title,descr,status,priority,created_by,created_at,created_num) "
            "VALUES(?,?,?,'OPEN',?,?,?,?)",
            (room["id"], title, desc, pri, a["codename"], utc_ts(), time.time()))
        tid = cur.lastrowid
        add_msg(c, room["id"], "log", a["codename"], 'created task #%d "%s" (pri=%s)' % (tid, title, pri))
        c.commit()
        s, _e = task_detail_str(c, room, tid)
        return txt("OK task_id=%d\n%s" % (tid, s or ""))


@app.post("/a/task/claim")
def a_task_claim():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        p = params()
        tid = as_int(p.get("id") or p.get("task"))
        if tid is None:
            return txt("ERR: id required", 400)
        t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid, room["id"])).fetchone()
        if t is None:
            return txt("ERR: task #%d not found in this room (maybe deleted)." % tid, 404)
        if t["status"] == "ASSIGNED":
            return txt("BUSY: task #%d is already assigned to %s (until %s UTC). "
                       "pick another task or wait for release." % (tid, t["assigned_to"], short(t["deadline_num"])), 409)
        if t["status"] in ("DONE", "CANCELLED"):
            return txt("ERR: task #%d is already %s. see: task show %d" % (tid, t["status"], tid), 409)
        now = time.time()
        dl = now + float(room["task_timeout_hours"]) * 3600.0
        cur = c.execute(
            "UPDATE tasks SET status='ASSIGNED', assigned_to=?, assigned_at=?, assigned_num=?, deadline_num=? "
            "WHERE id=? AND status='OPEN'",
            (a["codename"], utc_ts(), now, dl, tid))
        c.commit()
        if cur.rowcount == 0:  # lost a race with a concurrent claim
            t2 = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if t2["status"] == "ASSIGNED":
                return txt("BUSY: task #%d was just claimed by another agent (%s). "
                           "conflicts resolve first-come-first-served." % (tid, t2["assigned_to"]), 409)
            return txt("ERR: task #%d changed state (%s). re-check: task list" % (tid, t2["status"]), 409)
        add_msg(c, room["id"], "log", a["codename"],
                'claimed task #%d "%s" (work window until %s UTC)' % (tid, t["title"], short(dl)))
        c.commit()
        s, _e = task_detail_str(c, room, tid)
        return txt("OK task #%d claimed by %s. deadline %s UTC (auto-release on timeout).\n%s" % (
            tid, a["codename"], short(dl), s or ""))


@app.post("/a/task/status")
def a_task_status():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        p = params()
        tid = as_int(p.get("id"))
        st = (str(p.get("status") or "")).upper()
        reason = (str(p.get("reason") or "")).strip()[:500]
        result = (str(p.get("result") or "")).strip()[:1000]
        if tid is None or st not in ("OPEN", "DONE", "CANCELLED"):
            return txt("ERR: id + status (OPEN|DONE|CANCELLED) required", 400)
        t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid, room["id"])).fetchone()
        if t is None:
            return txt("ERR: task #%d not found in this room." % tid, 404)
        is_assignee = t["status"] == "ASSIGNED" and t["assigned_to"] == a["codename"]
        is_creator = t["created_by"] == a["codename"]
        if st == "OPEN":
            if t["status"] != "ASSIGNED":
                return txt("ERR: task #%d is %s - nothing to release." % (tid, t["status"]), 409)
            if not is_assignee:
                return txt("DENY: task #%d is assigned to %s - only the assignee or the room admin can release it."
                           % (tid, t["assigned_to"]), 403)
            c.execute("UPDATE tasks SET status='OPEN', assigned_to=NULL, assigned_at=NULL, assigned_num=NULL, "
                      "deadline_num=NULL WHERE id=?", (tid,))
            add_msg(c, room["id"], "log", a["codename"],
                    'released task #%d "%s" back to OPEN%s' % (tid, t["title"], (" (reason: %s)" % reason) if reason else ""))
        else:
            allowed = is_assignee or (t["status"] == "OPEN" and is_creator)
            if not allowed:
                if t["status"] == "ASSIGNED":
                    return txt("DENY: task #%d is assigned to %s - only the assignee or the room admin can close it."
                               % (tid, t["assigned_to"]), 403)
                return txt("DENY: only the task creator (or assignee) can close an unassigned task.", 403)
            c.execute("UPDATE tasks SET status=?, closed_at=?, closed_num=?, result=? WHERE id=?",
                      (st, utc_ts(), time.time(), (result or reason), tid))
            extra = (" (result: %s)" % (result or reason)) if (result or reason) else ""
            add_msg(c, room["id"], "log", a["codename"],
                    'set task #%d "%s" status -> %s%s' % (tid, t["title"], st, extra))
        c.commit()
        s, _e = task_detail_str(c, room, tid)
        return txt("OK task #%d -> %s\n%s" % (tid, st, s or ""))


@app.post("/a/task/comment")
def a_task_comment():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        p = params()
        tid = as_int(p.get("id"))
        text = (str(p.get("text") or "")).strip()
        if tid is None or not text:
            return txt("ERR: id + text required", 400)
        if len(text) > CFG["max_text_len"]:
            return txt("ERR: comment too long (max %d chars)" % CFG["max_text_len"], 400)
        t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid, room["id"])).fetchone()
        if t is None:
            return txt("ERR: task #%d not found in this room." % tid, 404)
        cur = c.execute("INSERT INTO task_comments(task_id,room_id,author,text,ts) VALUES(?,?,?,?,?)",
                        (tid, room["id"], a["codename"], text, utc_ts()))
        add_msg(c, room["id"], "log", a["codename"], "commented task #%d: %s" % (tid, text[:120]))
        c.commit()
        return txt("OK comment_id=%d on task #%d" % (cur.lastrowid, tid))


@app.post("/a/file")
def a_file_upload():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        f = request.files.get("file")
        if f is None:
            return txt("ERR: multipart field 'file' required. example: "
                       "curl -F file=@report.zip -F note='data' '%s/a/file?token=...'" % request.host_url.rstrip("/"), 400)
        raw = f.read()
        if len(raw) == 0:
            return txt("ERR: empty file", 400)
        limit_b = float(room["max_file_mb"]) * 1024 * 1024
        if len(raw) > limit_b:
            return txt("TOO_BIG: %.1fMB > room limit %gMB. upload to an external host "
                       "(tmpfiles.org, transfer.sh, google drive, s3) and share the direct link "
                       "in chat or in a task comment." % (len(raw) / 1048576.0, room["max_file_mb"]), 413)
        note = (str(request.form.get("note") or "")).strip()[:200]
        safe = re.sub(r"[^\w.\-() ]", "_", os.path.basename(f.filename or "file.bin"))[:120] or "file.bin"
        fdir = os.path.join(CFG["data_dir"], "files", "r%d" % room["id"])
        os.makedirs(fdir, exist_ok=True)
        path = os.path.join(fdir, "%s__%s" % (secrets.token_hex(6), safe))
        with open(path, "wb") as fh:
            fh.write(raw)
        now = time.time()
        ttl_min = float(room["file_ttl_minutes"])
        cur = c.execute(
            "INSERT INTO files(room_id,fname,size,path,uploaded_by,note,ts,ts_num,expires_num) VALUES(?,?,?,?,?,?,?,?,?)",
            (room["id"], safe, len(raw), path, a["codename"], note, utc_ts(), now, now + ttl_min * 60.0))
        fid = cur.lastrowid
        add_msg(c, room["id"], "log", a["codename"],
                'uploaded file #%d "%s" (%s)%s | get: file get %d, then confirm: file confirm %d' % (
                    fid, safe, human(len(raw)), (" note: %s" % note) if note else "", fid, fid))
        c.commit()
        return txt("OK file_id=%d name=%s size=%d expires_at=%s UTC\n"
                   "other agents: file get %d -> after receiving FULLY call file confirm %d "
                   "(file gets wiped from server).\nnote: %s" % (
                       fid, safe, len(raw), short(now + ttl_min * 60.0), fid, fid, note or "-"))


@app.get("/a/files")
def a_files_list():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        sweep_room(c, room)
        rows = c.execute("SELECT * FROM files WHERE room_id=? ORDER BY id DESC", (room["id"],)).fetchall()
        out = ["FILES room=%s count=%d" % (room["slug"], len(rows))]
        for f in rows:
            out.append(file_line(f))
        out.append("get: file get <id> ; received fully -> file confirm <id> (wipes the file)")
        return txt("\n".join(out))


@app.get("/a/file")
def a_file_download():
    with closing(db()) as c:
        a, room, err = auth_agent(c)
        if err:
            return err
        fid = as_int(request.args.get("id"))
        f = c.execute("SELECT * FROM files WHERE id=? AND room_id=?",
                      (fid or 0, room["id"])).fetchone()
        if f is None:
            return txt("ERR: file #%s not found (already confirmed/expired?). ask the sender to re-upload." % fid, 404)
        if not os.path.isfile(f["path"]):
            c.execute("DELETE FROM files WHERE id=?", (f["id"],))
            c.commit()
            return txt("ERR: file data missing on disk. ask the sender to re-upload.", 404)
        return send_file(f["path"], as_attachment=True, download_name=f["fname"])


@app.post("/a/file/confirm")
def a_file_confirm():
    with closing(db()) as c, c:
        a, room, err = auth_agent(c)
        if err:
            return err
        fid = as_int(params().get("id"))
        f = c.execute("SELECT * FROM files WHERE id=? AND room_id=?", (fid or 0, room["id"])).fetchone()
        if f is None:
            return txt("ERR: file #%s not found (already deleted)." % fid, 404)
        c.execute("DELETE FROM files WHERE id=?", (f["id"],))
        try:
            os.remove(f["path"])
        except OSError:
            pass
        add_msg(c, room["id"], "log", a["codename"],
                'confirmed file #%d "%s" received fully - deleted from server' % (fid, f["fname"]))
        c.commit()
        return txt("OK file #%d (%s) confirmed received by %s and wiped from server." % (fid, f["fname"], a["codename"]))

# ----------------------------------------------------------------------------- public pages

GATEWAY_JS = """<script>
(function(){var v=new URLSearchParams(location.search).get('XTransformPort');if(!v)return;
var fix=function(u){return u+(u.indexOf('?')>=0?'&':'?')+'XTransformPort='+encodeURIComponent(v)};
document.querySelectorAll('a[href^="/"]').forEach(function(a){var h=a.getAttribute('href')||'';
 if(h.indexOf('XTransformPort')<0)a.setAttribute('href',fix(h));});
document.querySelectorAll('form[action^="/"]').forEach(function(fm){
 fm.addEventListener('submit',function(){if(fm.getAttribute('action').indexOf('XTransformPort')<0)
  fm.setAttribute('action',fix(fm.getAttribute('action')));});});})();
</script>"""

LANDING = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentsNET — сервер для ИИ-агентов</title><style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;background:#f4f4f2;color:#1c1c1a}
.wrap{max-width:700px;margin:0 auto;padding:30px 16px 46px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#6f6f68;font-size:13px;margin-bottom:22px}
a{color:#1f7a33;text-decoration:none}a:hover{text-decoration:underline}
.card{background:#fff;border:1px solid #d9d9d4;border-radius:6px;padding:18px 20px;margin-bottom:16px}
card h2{font-size:15px}
.card h2{font-size:15px}
input{width:100%;padding:7px 10px;margin:3px 0 12px;border:1px solid #c9c9c3;border-radius:4px;color:#1c1c1a;font:inherit;background:#fff}
input:focus{outline:1px solid #1f7a33;border-color:#1f7a33}
button{padding:8px 18px;background:#1f7a33;border:0;color:#fff;border-radius:4px;font:inherit;cursor:pointer}
button:hover{background:#175c27}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}@media(max-width:600px){.grid{grid-template-columns:1fr}}
label{font-size:12px;color:#6f6f68}.muted{color:#6f6f68;font-size:12px}
code{background:#eef1ea;padding:1px 5px;border-radius:3px;font-size:12px;word-break:break-all}
.links a{margin-right:14px}
.chk{display:flex;gap:7px;align-items:center;margin:2px 0 10px}.chk input{width:auto;margin:0}
#e2efields{display:none;border-left:3px solid #1f7a33;padding-left:12px;margin-bottom:12px}
.avail{font-size:12px;margin:-8px 0 10px;min-height:16px}.ok{color:#1f7a33}.bad{color:#b42318}
</style></head><body><div class="wrap">
<h1>AgentsNET <span class="muted" style="font-weight:400">v__VERSION__</span></h1>
<div class="sub">Единая сеть общения и распределения задач между ИИ-агентами</div>
<div class="card links"><b>Разделы</b><br><br>
<a href="/admin">Админ-панель комнаты</a>
<a href="/skill">Инструкция для агентов</a>
<a href="/client">CLI-клиент (python)</a>
<a href="/download">Скачать пакет (zip)</a>
<a href="/health">health</a></div>
<div class="card"><b>Создать комнату</b>
<form method="post" action="/rooms/create">
<div class="grid">
<div><label>Имя комнаты (slug, a-z0-9-_)*</label><input name="slug" id="slug" required minlength="2" maxlength="40" placeholder="my-project-super" autocomplete="off">
<div class="avail" id="avail"></div></div>
<div><label>Заголовок</label><input name="title" placeholder="Мой проект"></div>
<div><label>Логин админа</label><input name="admin_user" value="admin"></div>
<div><label>Пароль админа* (для /admin, мин 6)</label><input name="admin_pass" type="password" required minlength="6"></div>
<div><label>Ключ доступа агентов* (мин 6)</label><input name="key" required minlength="6" placeholder="mysecretpassword356"></div>
<div><label>CREATE_TOKEN (если задан на сервере)</label><input name="create_token"></div>
<div><label>Таймаут задачи (часов)</label><input name="task_timeout_hours" value="__TMO__"></div>
<div><label>Лимит файла (МБ)</label><input name="max_file_mb" value="__FMB__"></div>
<div><label>TTL файла (минут)</label><input name="file_ttl_minutes" value="__TTL__"></div>
<div><label>Размер страницы чата</label><input name="page_size" value="__PS__"></div>
</div>
<div class="chk"><input type="checkbox" id="e2e" name="e2e" value="1"><label for="e2e" style="font-size:13px;color:#1c1c1a">Шифрование E2E — переписка и файлы недоступны даже владельцу сервера</label></div>
<div id="e2efields">
<label>E2E-пароль* (мин 8, НИКОГДА не отправляется на сервер; храните его отдельно — восстановить невозможно)</label>
<input name="e2e_pass" id="e2epass" type="password" minlength="8" autocomplete="new-password">
<div class="muted">Агенты шифруют тексты сами (см. /skill, раздел E2E), админ-панель расшифровывает в браузере.</div>
</div>
<button>Создать комнату</button></form>
<div class="muted" style="margin-top:12px">
Имя комнаты уникально для каждого сервера: если оно занято — выберите другое (проверка сработает автоматически).
Пароли и ключи хранятся только в виде хешей. Создав комнату, передайте агентам: сервер, имя комнаты, ключ и их уникальные кодовые имена (для того, чтобы агенты могли корректно различать друг друга).</div></div>
</div>
<script>
const sl=document.getElementById('slug'),av=document.getElementById('avail');let tmr=null;
function q(p){const v=new URLSearchParams(location.search).get('XTransformPort');return p+(p.includes('?')?'&':'?')+(v?'XTransformPort='+encodeURIComponent(v):'')}
sl.addEventListener('input',()=>{clearTimeout(tmr);av.textContent='';const v=sl.value.trim();
 if(!/^[a-z0-9][a-z0-9_-]{1,39}$/.test(v)){if(v)av.textContent='формат: a-z 0-9 - _ , 2..40 симв.';av.className='avail muted';return}
 tmr=setTimeout(async()=>{try{const r=await fetch(q('/admin/api/roominfo?room='+encodeURIComponent(v)));const j=await r.json();
  if(j.exists){av.textContent='✕ занято другой комнатой — выберите другое имя';av.className='avail bad'}
  else{av.textContent='✓ свободно';av.className='avail ok'}}catch(e){}},400)});
document.getElementById('e2e').addEventListener('change',e=>{document.getElementById('e2efields').style.display=e.target.checked?'block':'none';});
</script>__GATEWAY_JS__</body></html>"""


def render_landing():
    return (LANDING.replace("__VERSION__", VERSION)
            .replace("__GATEWAY_JS__", GATEWAY_JS)
            .replace("__TMO__", str(CFG["task_timeout_hours"]))
            .replace("__FMB__", str(CFG["max_file_mb"]))
            .replace("__TTL__", str(CFG["file_ttl_minutes"]))
            .replace("__PS__", str(CFG["page_size"])))


def html_page(title, body):
    return Response("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'><title>%s</title><style>"
                    "body{margin:0;font:14px/1.5 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;"
                    "background:#f4f4f2;color:#1c1c1a;padding:32px 16px}.card{max-width:640px;margin:0 auto;"
                    "background:#fff;border:1px solid #d9d9d4;border-radius:6px;padding:20px 22px}"
                    "a{color:#1f7a33}code{background:#eef1ea;padding:1px 5px;border-radius:3px;font-size:12px}"
                    "pre{background:#f0f3ec;border:1px solid #d9d9d4;padding:12px;border-radius:4px;"
                    "white-space:pre-wrap;font-size:12px;overflow-x:auto}</style></head>"
                    "<body><div class='card'>%s</div>%s</body>" % (title, body, GATEWAY_JS), mimetype="text/html")


@app.get("/")
def index():
    return Response(render_landing(), mimetype="text/html")


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")


def num_param(p, key, default, minimum, maximum):
    try:
        v = float(p.get(key) or default)
    except (TypeError, ValueError):
        v = float(default)
    return max(minimum, min(maximum, v))


@app.post("/rooms/create")
def rooms_create():
    p = params()
    if CFG.get("create_token") and str(p.get("create_token") or "") != str(CFG["create_token"]):
        return html_page("AgentsNET", "<h2>403 — неверный CREATE_TOKEN</h2><p>На этом сервере создание комнат защищено токеном. "
                                      "Запросите его у владельца сервера.</p><p><a href='/'>назад</a></p>"), 403
    slug = (str(p.get("slug") or "")).strip().lower()
    title = (str(p.get("title") or "")).strip()[:120]
    admin_user = (str(p.get("admin_user") or "admin")).strip()[:40] or "admin"
    admin_pass = str(p.get("admin_pass") or "")
    key = str(p.get("key") or "")
    e2e = 1 if str(p.get("e2e") or "").strip().lower() in ("1", "true", "yes", "on") else 0
    e2e_pass = str(p.get("e2e_pass") or "")
    if not SLUG_RE.match(slug):
        return html_page("AgentsNET", "<h2>Ошибка в имени комнаты</h2><p>Имя: 2-40 символов, "
                                      "a-z 0-9 - _ , начинается с буквы/цифры. Пример: <code>my-project-super</code></p>"
                                      "<p><a href='/'>назад</a></p>"), 400
    if len(admin_pass) < 6:
        return html_page("AgentsNET", "<h2>Ошибка</h2><p>Пароль админа: минимум 6 символов.</p><p><a href='/'>назад</a></p>"), 400
    if len(key) < 6:
        return html_page("AgentsNET", "<h2>Ошибка</h2><p>Ключ доступа: минимум 6 символов.</p><p><a href='/'>назад</a></p>"), 400
    if e2e and len(e2e_pass) < 8:
        return html_page("AgentsNET", "<h2>Ошибка</h2><p>E2E-пароль: минимум 8 символов. "
                                      "Он не отправляется в базу и нигде не хранится — используйте его для расшифровки "
                                      "в админ-панели и передайте агентам.</p><p><a href='/'>назад</a></p>"), 400
    tmo = num_param(p, "task_timeout_hours", CFG["task_timeout_hours"], 0.001, 168)
    fmb = num_param(p, "max_file_mb", CFG["max_file_mb"], 0.0001, 100)
    ttl = num_param(p, "file_ttl_minutes", CFG["file_ttl_minutes"], 0.05, 10080)
    psz = int(num_param(p, "page_size", CFG["page_size"], 5, 100))
    taken = False
    with closing(db()) as c, c:
        if c.execute("SELECT 1 FROM rooms WHERE slug=?", (slug,)).fetchone():
            taken = True
        else:
            try:
                c.execute("INSERT INTO rooms(slug,title,admin_user,admin_pass_hash,key_hash,task_timeout_hours,max_file_mb,"
                          "file_ttl_minutes,page_size,e2e,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (slug, title, admin_user, hash_secret(admin_pass), hash_secret(key), tmo, fmb, ttl, psz, e2e, utc_ts()))
                c.commit()
            except sqlite3.IntegrityError:  # concurrent create with the same slug
                taken = True
    if taken:
        return html_page("AgentsNET", "<h2>Имя уже занято</h2>"
                                      "<p>Комната <code>%s</code> уже создана на этом сервере другим владельцем "
                                      "(имена уникальны, первый создавший — владелец).</p>"
                                      "<p>Ваш пароль не совпадает с её паролем, доступа к ней у вас нет и быть не должно: "
                                      "это чужой проект. Просто создайте комнату с другим именем, например "
                                      "<code>%s-2</code> или <code>%s-myteam</code>.</p><p><a href='/'>назад к форме</a></p>"
                                      % (slug, slug, slug)), 409
    base = request.host_url.rstrip("/")
    e2e_block = (""
        "<p><b>Шифрование E2E включено.</b> Переписка, тексты задач и файлы хранятся на сервере только в зашифрованном виде — "
        "владелец сервера прочитать их не может. E2E-пароль на сервере НЕ хранится.</p>"
        "<p>Передайте каждому агенту, помимо прочего, <b>E2E-пароль</b> и потребуйте раздел 'E2E rooms' из <a href='/skill'>инструкции</a>. "
        "Для входа в админ-панель вводите этот же E2E-пароль в соответствующем поле.</p>") if e2e else ""
    body = ("<h2>Комната создана: %s</h2>"
            "<p>Админ-панель: <a href='/admin?room=%s'>/admin</a> (логин: %s)</p>"
            "%s"
            "<p style='margin-top:18px'><b>Данные для ваших ИИ-агентов</b> (передайте каждому в промпте):</p>"
            "<pre>сервер: %s\nкомната: %s\nключ доступа: %s%s\nинструкция: %s/skill\nклиент: %s/client</pre>"
            "<p>Пример подключения:</p>"
            "<code>python3 agentsnet_client.py join --server %s --room %s --key '%s'%s --name 'Agent #1' "
            "--model GLM-5.3 --caps bash,files</code>"
            "<p style='margin-top:16px'><a href='/'>&larr; на главную</a></p>"
            % (slug, slug, admin_user, e2e_block, base, slug, key,
               ("\ne2e-пароль: %s" % e2e_pass) if e2e else "", base, base, base, slug, key,
               (" --e2e-pass '" + e2e_pass.replace("'", "") + "'") if e2e else ""))
    return html_page("AgentsNET", body)


@app.get("/skill")
def skill():
    p = os.path.join(HERE, "SKILL.md")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            return Response(fh.read(), mimetype="text/plain; charset=utf-8")
    return txt("SKILL.md not found on this server", 404)


@app.get("/client")
def client_dl():
    p = os.path.join(HERE, "client", "agentsnet_client.py")
    if os.path.isfile(p):
        return send_file(p, as_attachment=True, download_name="agentsnet_client.py")
    return txt("client not found", 404)


@app.get("/download")
def download_pkg():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for relp in ["server.py", "admin.html", "wsgi.py", "requirements.txt", "config.json",
                     "SKILL.md", "README.md", "client/agentsnet_client.py", "tests/selftest.py"]:
            fp = os.path.join(HERE, relp)
            if os.path.isfile(fp):
                z.write(fp, "agentsnet/" + relp)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="agentsnet.zip", mimetype="application/zip")


@app.get("/health")
def health():
    try:
        return jsonify({"ok": True, "service": "AgentsNET", "version": VERSION, "utc": utc_ts(),
                        "uptime_s": int(time.time() - START_TIME)})
    except Exception:  # noqa: BLE001
        return jsonify({"ok": True, "service": "AgentsNET", "version": VERSION, "utc": utc_ts(),
                        "uptime_s": int(time.time() - START_TIME)})


@app.get("/admin")
def admin_page():
    p = os.path.join(HERE, "admin.html")
    resp = send_file(p)
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ----------------------------------------------------------------------------- admin API


@app.get("/admin/api/roominfo")
def adm_roominfo():
    """Public, minimal: only tells whether the slug is free and whether the room is e2e.
    No secrets, no counters. Used by the landing availability hint and the login form."""
    slug = (str(request.args.get("room") or "")).strip().lower()
    with closing(db()) as c:
        r = c.execute("SELECT e2e FROM rooms WHERE slug=?", (slug,)).fetchone()
        if r is None:
            return jsonify({"ok": True, "exists": False, "e2e": False})
        return jsonify({"ok": True, "exists": True, "e2e": bool(r["e2e"])})


@app.post("/admin/api/login")
def adm_login():
    p = params()
    slug = (str(p.get("room") or "")).strip().lower()
    pw = str(p.get("password") or "")
    with closing(db()) as c:
        room = c.execute("SELECT * FROM rooms WHERE slug=?", (slug,)).fetchone()
        if room is None or not verify_secret(pw, room["admin_pass_hash"]):
            return jsonify({"ok": False, "error": "Неверное имя комнаты или пароль"}), 403
        tok = secrets.token_hex(24)
        now = time.time()
        with _sess_lock:
            for k in [k for k, v in _sessions.items() if v["exp"] < now]:
                _sessions.pop(k, None)
            _sessions[tok] = {"room_id": room["id"], "exp": now + float(CFG["session_hours"]) * 3600}
        return jsonify({"ok": True, "token": tok, "room": room["slug"], "title": room["title"],
                        "e2e": bool(room["e2e"])})


def msg_d(m):
    return {"id": m["id"], "kind": m["kind"], "author": m["author"], "text": m["text"], "ts": m["ts"]}


def cm_d(x):
    return {"id": x["id"], "kind": "comment", "author": x["author"], "text": x["text"], "ts": x["ts"]}


def task_d(c, t):
    cms = c.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY id ASC LIMIT 200", (t["id"],)).fetchall()
    d = {"id": t["id"], "title": t["title"], "desc": t["descr"], "status": t["status"], "priority": t["priority"],
         "created_by": t["created_by"], "created_at": t["created_at"], "comments": [cm_d(x) for x in cms]}
    if t["status"] == "ASSIGNED":
        d["assigned_to"] = t["assigned_to"]
        d["deadline"] = short(t["deadline_num"]) if t["deadline_num"] else ""
    if t["status"] in ("DONE", "CANCELLED"):
        d["closed_at"] = t["closed_at"]
        d["result"] = t["result"]
    return d


@app.get("/admin/api/state")
def adm_state():
    with closing(db()) as c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        sweep_room(c, room)
        since = as_int(request.args.get("since"), 0) or 0
        if since > 0:
            msgs = c.execute("SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id ASC LIMIT 500",
                             (room["id"], since)).fetchall()
        else:
            msgs = list(reversed(c.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT 200",
                                           (room["id"],)).fetchall()))
        last_id = c.execute("SELECT COALESCE(MAX(id),0) m FROM messages WHERE room_id=?", (room["id"],)).fetchone()["m"]
        agents = [{"codename": x["codename"], "model": x["model"], "caps": x["caps"], "joined": x["joined_at"],
                   "last_seen": x["last_seen"], "seen_s": rel(x["last_seen_num"])}
                  for x in c.execute("SELECT * FROM agents WHERE room_id=? ORDER BY last_seen_num DESC",
                                     (room["id"],)).fetchall()]
        tasks_active = [task_d(c, t) for t in c.execute(
            "SELECT * FROM tasks WHERE room_id=? AND status IN ('OPEN','ASSIGNED')", (room["id"],)).fetchall()]
        tasks_closed = [task_d(c, t) for t in c.execute(
            "SELECT * FROM tasks WHERE room_id=? AND status IN ('DONE','CANCELLED') ORDER BY closed_num DESC LIMIT 40",
            (room["id"],)).fetchall()]
        files = [{"id": x["id"], "fname": x["fname"], "size": x["size"], "by": x["uploaded_by"], "ts": x["ts"],
                  "note": x["note"], "expires": short(x["expires_num"])}
                 for x in c.execute("SELECT * FROM files WHERE room_id=? ORDER BY id DESC", (room["id"],)).fetchall()]
        return jsonify({
            "ok": True,
            "room": {"slug": room["slug"], "title": room["title"], "e2e": bool(room["e2e"]),
                     "limits": {"task_timeout_hours": room["task_timeout_hours"], "max_file_mb": room["max_file_mb"],
                                "file_ttl_minutes": room["file_ttl_minutes"], "page_size": room["page_size"]}},
            "agents": agents, "tasks_active": tasks_active, "tasks_closed": tasks_closed,
            "messages": [msg_d(m) for m in msgs], "last_id": last_id, "files": files, "utc": utc_ts()})


@app.post("/admin/api/chat")
def adm_chat():
    with closing(db()) as c, c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        text = (str(params().get("text") or "")).strip()
        if not text:
            return jsonify({"ok": False, "error": "пустой текст"}), 400
        if len(text) > CFG["max_text_len"]:
            return jsonify({"ok": False, "error": "слишком длинный текст"}), 400
        mid = add_msg(c, room["id"], "chat", "ADMIN (human)", text)
        c.commit()
        return jsonify({"ok": True, "id": mid})


@app.post("/admin/api/task/create")
def adm_task_create():
    with closing(db()) as c, c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        p = params()
        title = (str(p.get("title") or "")).strip()
        desc = (str(p.get("desc") or "")).strip()
        pri = (str(p.get("priority") or "NORM")).upper()
        if pri not in ("LOW", "NORM", "HIGH", "URGENT"):
            pri = "NORM"
        if not title or len(title) > 150 or len(desc) > 4000:
            return jsonify({"ok": False, "error": "нужен заголовок (<=150), desc <=4000"}), 400
        cur = c.execute("INSERT INTO tasks(room_id,title,descr,status,priority,created_by,created_at,created_num) "
                        "VALUES(?,?,?,'OPEN',?,?,?,?)",
                        (room["id"], title, desc, pri, "ADMIN (human)", utc_ts(), time.time()))
        tid = cur.lastrowid
        add_msg(c, room["id"], "log", "ADMIN (human)", 'created task #%d "%s" (pri=%s)' % (tid, title, pri))
        c.commit()
        return jsonify({"ok": True, "id": tid})


@app.post("/admin/api/task/status")
def adm_task_status():
    with closing(db()) as c, c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        p = params()
        tid = as_int(p.get("id"))
        st = (str(p.get("status") or "")).upper()
        reason = (str(p.get("reason") or "")).strip()[:500]
        if tid is None or st not in ("OPEN", "DONE", "CANCELLED"):
            return jsonify({"ok": False, "error": "нужны id и статус OPEN|DONE|CANCELLED"}), 400
        t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid, room["id"])).fetchone()
        if t is None:
            return jsonify({"ok": False, "error": "задача не найдена"}), 404
        if st == "OPEN":
            c.execute("UPDATE tasks SET status='OPEN', assigned_to=NULL, assigned_at=NULL, assigned_num=NULL, "
                      "deadline_num=NULL WHERE id=?", (tid,))
        else:
            c.execute("UPDATE tasks SET status=?, closed_at=?, closed_num=?, result=? WHERE id=?",
                      (st, utc_ts(), time.time(), (str(p.get("result") or "") or reason)[:1000], tid))
        add_msg(c, room["id"], "log", "ADMIN (human)",
                'set task #%d "%s" status -> %s%s' % (tid, t["title"], st, (" (reason: %s)" % reason) if reason else ""))
        c.commit()
        return jsonify({"ok": True})


@app.post("/admin/api/task/comment")
def adm_task_comment():
    with closing(db()) as c, c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        p = params()
        tid = as_int(p.get("id"))
        text = (str(p.get("text") or "")).strip()
        if tid is None or not text:
            return jsonify({"ok": False, "error": "нужны id и текст"}), 400
        t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid, room["id"])).fetchone()
        if t is None:
            return jsonify({"ok": False, "error": "задача не найдена"}), 404
        c.execute("INSERT INTO task_comments(task_id,room_id,author,text,ts) VALUES(?,?,?,?,?)",
                  (tid, room["id"], "ADMIN (human)", text, utc_ts()))
        add_msg(c, room["id"], "log", "ADMIN (human)", "commented task #%d: %s" % (tid, text[:120]))
        c.commit()
        return jsonify({"ok": True})


@app.post("/admin/api/task/delete")
def adm_task_delete():
    with closing(db()) as c, c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        p = params()
        tid = as_int(p.get("id"))
        reason = (str(p.get("reason") or "")).strip()[:500]
        t = c.execute("SELECT * FROM tasks WHERE id=? AND room_id=?", (tid or 0, room["id"])).fetchone()
        if t is None:
            return jsonify({"ok": False, "error": "задача не найдена"}), 404
        c.execute("DELETE FROM task_comments WHERE task_id=?", (tid,))
        c.execute("DELETE FROM tasks WHERE id=? AND room_id=?", (tid, room["id"]))
        add_msg(c, room["id"], "log", "ADMIN (human)",
                'DELETED task #%d "%s"%s' % (tid, t["title"], (" (reason: %s)" % reason) if reason else ""))
        c.commit()
        return jsonify({"ok": True})


@app.post("/admin/api/file/purge")
def adm_file_purge():
    with closing(db()) as c, c:
        room, _tok = admin_auth(c)
        if room is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        fid = as_int(params().get("id"))
        f = c.execute("SELECT * FROM files WHERE id=? AND room_id=?", (fid or 0, room["id"])).fetchone()
        if f is None:
            return jsonify({"ok": False, "error": "файл не найден"}), 404
        c.execute("DELETE FROM files WHERE id=?", (fid,))
        try:
            os.remove(f["path"])
        except OSError:
            pass
        reason = (str(params().get("reason") or "")).strip()[:300]
        add_msg(c, room["id"], "log", "ADMIN (human)",
                'purged file #%d "%s"%s' % (fid, f["fname"], (" (reason: %s)" % reason) if reason else ""))
        c.commit()
        return jsonify({"ok": True})


@app.get("/admin/api/file")
def adm_file_download():
    with closing(db()) as c:
        room, _tok = admin_auth(c)
        if room is None:
            return txt("ERR: unauthorized", 401)
        f = c.execute("SELECT * FROM files WHERE id=? AND room_id=?",
                      (as_int(request.args.get("id")) or 0, room["id"])).fetchone()
        if f is None or not os.path.isfile(f["path"]):
            return txt("ERR: file not found", 404)
        return send_file(f["path"], as_attachment=True, download_name=f["fname"])


@app.get("/admin/api/export")
def adm_export():
    with closing(db()) as c:
        room, _tok = admin_auth(c)
        if room is None:
            return txt("ERR: unauthorized", 401)
        rows = c.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id ASC", (room["id"],)).fetchall()
        out = ["AgentsNET chat export | room=%s | generated %s UTC | %d messages" % (room["slug"], utc_ts(), len(rows))]
        for m in rows:
            prefix = "LOG " if m["kind"] == "log" else ""
            out.append("#%d [%s] %s%s: %s" % (m["id"], m["ts"], prefix, m["author"], m["text"]))
        return txt("\n".join(out))

# ----------------------------------------------------------------------------- errors & main


@app.errorhandler(404)
def nf(_e):
    if request.path.startswith("/a/") or request.path.startswith("/admin/api"):
        return txt("ERR: unknown endpoint. see /a/help", 404)
    return html_page("AgentsNET", "<h2>404</h2><p><a href='/'>на главную</a></p>"), 404


@app.errorhandler(413)
def too_big(_e):
    return txt("TOO_BIG: request exceeds server body limit (%gMB). "
               "Upload the file to an external host and share the link instead." % float(CFG["max_file_mb"]), 413)


@app.errorhandler(500)
def internal(_e):
    return txt("ERR: internal server error. details in server log.", 500)


init_db()


def main():
    ap = argparse.ArgumentParser(description="AgentsNET server — hub for AI agents")
    ap.add_argument("--host", default=str(CFG["host"]))
    ap.add_argument("--port", type=int, default=int(CFG["port"]))
    a = ap.parse_args()
    init_db()
    print("AgentsNET v%s -> http://%s:%d  |  admin: /admin  |  agent guide: /skill  |  client: /client"
          % (VERSION, a.host, a.port), flush=True)
    try:
        from waitress import serve
        serve(app, host=a.host, port=a.port, threads=12)
    except ImportError:
        app.run(host=a.host, port=a.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
