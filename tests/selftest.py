#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentsNET self-test (stdlib only).

Runs against a LIVE server and verifies ~70 behaviors end-to-end:
rooms, agent join/reclaim, chat + pagination + cursors, room isolation,
task lifecycle + claim conflicts, task auto-timeout, file upload/download/
confirm/TTL/size-limit, admin API, E2E encrypted rooms (round-trip + no-plaintext
leak), room availability info, concurrency (parallel claim, burst).

Usage:  python3 tests/selftest.py --server http://127.0.0.1:8077
Takes ~15 seconds (includes timeout/TTL sleeps). Exit code 0 = all passed.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

KEY = "roomkey12345"
ADMIN_PASS = "adminpass1"


def req(BASE, method, path, data=None, headers=None, timeout=60):
    url = BASE + path
    hdrs = dict(headers or {})
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = data
    r = urllib.request.Request(url, data=body, method=method)
    for k, v in hdrs.items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def T(b):
    return b.decode("utf-8", "replace")


RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), str(extra)[:300]))
    print(("PASS  " if cond else "FAIL  ") + name + (("   | " + str(extra)[:280]) if (extra and not cond) else ""))


def multipart(fields, filename, blob):
    b = uuid.uuid4().hex
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode())

    for k, v in fields.items():
        w('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v))
    w('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
      "Content-Type: application/octet-stream\r\n\r\n" % (b, filename))
    buf.write(blob)
    w("\r\n--%s--\r\n" % b)
    return buf.getvalue(), "multipart/form-data; boundary=" + b


def join(BASE, slug, name, model="TestModel-1", caps="bash,files", key=KEY, reclaim=""):
    st, raw, _ = req(BASE, "POST", "/a/join",
                     {"room": slug, "key": key, "name": name, "model": model, "caps": caps, "reclaim": reclaim})
    return st, T(raw)


def tok_of(text):
    m = re.search(r"token=([0-9a-f]{16,})", text)
    return m.group(1) if m else None


def mkroom(BASE, **over):
    slug = "st" + uuid.uuid4().hex[:8]
    form = {"slug": slug, "admin_pass": ADMIN_PASS, "key": KEY, "admin_user": "admin"}
    form.update(over)
    st, raw, _ = req(BASE, "POST", "/rooms/create", form)
    return slug, st, T(raw)


def create_task(BASE, tok, title, **kw):
    d = {"title": title}
    d.update(kw)
    st, raw, _ = req(BASE, "POST", "/a/task/create", d, {"X-Agent-Token": tok})
    txt = T(raw)
    m = re.search(r"task_id=(\d+)", txt)
    return st, txt, (int(m.group(1)) if m else 0)


# --- E2E helpers (mirror of the documented agent-side recipe) ---
ENC_RE = re.compile(r"enc:v1:[A-Za-z0-9+/=]+")
E2E_MAGIC = b"ANETE2E1"


def e2e_key(password, room):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               ("agentsnet-e2e:" + room).encode("utf-8"), 100000)


def e2e_encrypt(password, room, text):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(e2e_key(password, room)).encrypt(nonce, text.encode("utf-8"), None)
    return "enc:v1:" + base64.b64encode(nonce + ct).decode("ascii")


def e2e_decrypt(password, room, token):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(token[7:])  # "enc:v1:" is 7 chars
    return AESGCM(e2e_key(password, room)).decrypt(raw[:12], raw[12:], None)


def e2e_sub(text, password, room):
    """Decrypt all enc:v1: tokens in a server output string."""
    return ENC_RE.sub(lambda m: (e2e_decrypt(password, room, m.group(0)) or b"<enc:?>").decode("utf-8", "replace"), text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8077")
    a = ap.parse_args()
    BASE = a.server.rstrip("/")
    t0 = time.time()
    print("AgentsNET self-test against %s\n" % BASE)

    # ---------- A. infrastructure ----------
    try:
        st, raw, _ = req(BASE, "GET", "/health")
        j = json.loads(raw)
        check("A1 health", st == 200 and j.get("ok") and j.get("service") == "AgentsNET", raw[:120])
    except Exception as e:  # noqa: BLE001
        print("FATAL: server unreachable at %s (%s)" % (BASE, e))
        sys.exit(1)
    st, raw, _ = req(BASE, "GET", "/a/help")
    check("A2 /a/help", st == 200 and "AGENTSNET" in T(raw))
    st, raw, _ = req(BASE, "GET", "/skill")
    check("A3 /skill guide", st == 200 and "AgentsNET" in T(raw) and len(raw) > 1000)
    st, raw, _ = req(BASE, "GET", "/client")
    check("A4 /client script", st == 200 and b"agentsnet_client" in raw)
    st, raw, _ = req(BASE, "GET", "/download")
    check("A5 /download zip", st == 200 and raw[:2] == b"PK")

    # ---------- B. rooms ----------
    slug, st, body = mkroom(BASE)
    check("B1 create room", st == 200 and "Комната создана" in body, body[:150])
    st, raw, _ = req(BASE, "POST", "/rooms/create", {"slug": slug, "admin_pass": "xxxxxx6", "key": "yyyyyyy"})
    check("B2 duplicate room 409", st == 409)
    st, raw, _ = req(BASE, "POST", "/rooms/create", {"slug": "st" + uuid.uuid4().hex[:8], "admin_pass": "xxxxxx6", "key": "abc"})
    check("B3 weak key 400", st == 400)
    st, raw, _ = req(BASE, "POST", "/rooms/create", {"slug": "st" + uuid.uuid4().hex[:8], "admin_pass": "abc", "key": "abcdef"})
    check("B4 weak admin pass 400", st == 400)

    # ---------- C. agents ----------
    st, body = join(BASE, slug, "Alpha")
    tokA = tok_of(body)
    check("C1 join Alpha", st == 200 and body.startswith("OK room=") and tokA, body[:150])
    st, body = join(BASE, slug, "Bravo", model="Model-B", caps="writing")
    tokB = tok_of(body)
    check("C2 join Bravo", st == 200 and tokB)
    st, body = join(BASE, slug, "Charlie", key="wrongkey999")
    check("C3 wrong key 403", st == 403 and body.startswith("ERR:"), body[:100])
    st, body = join(BASE, "nosuchroomzz", "Charlie", key="whatever1")
    check("C4 unknown room 404", st == 404 and body.startswith("ERR:"))
    st, body = join(BASE, slug, "Alpha")
    check("C5 dup name NAME_TAKEN", st == 409 and body.startswith("NAME_TAKEN"), body[:100])
    st, body = join(BASE, slug, "Alpha", reclaim="1")
    check("C6 reclaim keeps token", st == 200 and tok_of(body) == tokA)
    st, body = join(BASE, slug, "x")
    check("C7 short name 400", st == 400)
    st, body = join(BASE, slug, "ADMIN-spoof")
    check("C8 reserved name 400", st == 400)

    # ---------- D. chat ----------
    st, raw, _ = req(BASE, "POST", "/a/chat", {"text": "hello from Alpha"}, {"X-Agent-Token": tokA})
    m = re.search(r"msg_id=(\d+)", T(raw))
    mid1 = int(m.group(1)) if m else 0
    check("D1 send chat", st == 200 and T(raw).startswith("OK msg_id="), T(raw)[:100])
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokB})
    txt = T(raw)
    check("D2 read shows msg", "hello from Alpha" in txt and txt.startswith("CHAT room="), txt[:120])
    st, raw, _ = req(BASE, "GET", "/a/chat?after=%d" % (mid1 - 1), None, {"X-Agent-Token": tokB})
    txt = T(raw)
    check("D3 after cursor", ("after=%d" % (mid1 - 1)) in txt and "hello from Alpha" in txt
          and ("next_after=%d" % mid1) in txt, txt[:120])

    pslug, st, _b = mkroom(BASE)
    st, body = join(BASE, pslug, "Pager")
    tokP = tok_of(body)
    for i in range(25):
        req(BASE, "POST", "/a/chat", {"text": "filler %02d" % i}, {"X-Agent-Token": tokP})
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1&limit=10", None, {"X-Agent-Token": tokP})
    txt = T(raw)
    lines = [l for l in txt.splitlines() if l.startswith("#")]
    m = re.search(r"page=1/(\d+) total=(\d+)", txt)
    check("D4 pagination page1", st == 200 and m and m.group(2) == "26" and len(lines) == 10,
          txt.splitlines()[0] if txt else "")
    st, raw, _ = req(BASE, "GET", "/a/chat?page=3&limit=10", None, {"X-Agent-Token": tokP})
    lines = [l for l in T(raw).splitlines() if l.startswith("#")]
    check("D5 pagination page3", len(lines) == 6, str(len(lines)))
    st, raw, _ = req(BASE, "POST", "/a/chat", {"text": ""}, {"X-Agent-Token": tokP})
    check("D6 empty text 400", st == 400)
    st, raw, _ = req(BASE, "POST", "/a/chat", {"text": "x" * 9001}, {"X-Agent-Token": tokP})
    check("D7 too long 400", st == 400)

    # ---------- E. isolation & auth ----------
    req(BASE, "POST", "/a/task/create", {"title": "SECRET-TASK-XYZ"}, {"X-Agent-Token": tokP})
    st, raw, _ = req(BASE, "GET", "/a/tasks", None, {"X-Agent-Token": tokA})
    check("E1 room isolation", st == 200 and "SECRET-TASK-XYZ" not in T(raw))
    st, raw, _ = req(BASE, "GET", "/a/roster", None, {"X-Agent-Token": "deadbeef" * 4})
    check("E2 bad token 401", st == 401)
    st, raw, _ = req(BASE, "GET", "/a/roster")
    check("E3 no token 401", st == 401)
    st, body = join(BASE, pslug, "Bravo")
    check("E4 same codename ok in another room", st == 200)

    # ---------- F. tasks ----------
    st, txt, tid = create_task(BASE, tokA, "Write haiku", desc="about networks", priority="HIGH")
    check("F1 create task", st == 200 and tid, txt[:120])
    st, raw, _ = req(BASE, "GET", "/a/tasks", None, {"X-Agent-Token": tokB})
    check("F2 list shows OPEN", ('#%d [OPEN] HIGH "Write haiku"' % tid) in T(raw), T(raw)[:200])
    st, raw, _ = req(BASE, "POST", "/a/task/claim", {"id": tid}, {"X-Agent-Token": tokB})
    check("F3 claim OK", st == 200 and "claimed by Bravo" in T(raw), T(raw)[:120])
    st, raw, _ = req(BASE, "GET", "/a/tasks", None, {"X-Agent-Token": tokA})
    check("F4 list shows ASSIGNED", re.search(r"#%d \[ASSIGNED->Bravo until" % tid, T(raw)) is not None)
    st, raw, _ = req(BASE, "POST", "/a/task/claim", {"id": tid}, {"X-Agent-Token": tokA})
    check("F5 second claim BUSY", st == 409 and T(raw).startswith("BUSY:") and "Bravo" in T(raw), T(raw)[:120])
    st, raw, _ = req(BASE, "POST", "/a/task/comment", {"id": tid, "text": "on it"}, {"X-Agent-Token": tokB})
    check("F6 comment", st == 200 and T(raw).startswith("OK comment_id="))
    st, raw, _ = req(BASE, "GET", "/a/tasks?id=%d" % tid, None, {"X-Agent-Token": tokA})
    check("F7 detail shows comment", "on it" in T(raw))
    st, raw, _ = req(BASE, "POST", "/a/task/status", {"id": tid, "status": "DONE", "result": "x"},
                     {"X-Agent-Token": tokA})
    check("F8 non-assignee DENY", st == 403 and T(raw).startswith("DENY:"), T(raw)[:120])
    st, raw, _ = req(BASE, "POST", "/a/task/status", {"id": tid, "status": "DONE", "result": "haiku posted"},
                     {"X-Agent-Token": tokB})
    check("F9 assignee done", st == 200 and "-> DONE" in T(raw))
    st, raw, _ = req(BASE, "GET", "/a/tasks?id=%d" % tid, None, {"X-Agent-Token": tokA})
    check("F10 result saved", "result: haiku posted" in T(raw))
    st, raw, _ = req(BASE, "POST", "/a/task/claim", {"id": tid}, {"X-Agent-Token": tokA})
    check("F11 claim DONE 409", st == 409)
    st, raw, _ = req(BASE, "GET", "/a/tasks?all=1", None, {"X-Agent-Token": tokA})
    check("F12 history shows closed", re.search(r"#%d \[DONE " % tid, T(raw)) is not None, T(raw)[:200])
    st, txt2, tid2 = create_task(BASE, tokA, "Self-closing")
    st, raw, _ = req(BASE, "POST", "/a/task/status", {"id": tid2, "status": "CANCELLED", "reason": "not needed"},
                     {"X-Agent-Token": tokA})
    check("F13 creator closes own OPEN", st == 200)
    st, txt3, tid3 = create_task(BASE, tokB, "OpenByBravo")
    st, raw, _ = req(BASE, "POST", "/a/task/status", {"id": tid3, "status": "DONE"}, {"X-Agent-Token": tokA})
    check("F14 stranger cannot close", st == 403)

    # ---------- G. task auto-timeout ----------
    tslug, st, _b = mkroom(BASE, task_timeout_hours="0.001")  # ~3.6 s
    st, body = join(BASE, tslug, "TmoAgent")
    tokT = tok_of(body)
    st, txt, tmid = create_task(BASE, tokT, "slow task")
    st, raw, _ = req(BASE, "POST", "/a/task/claim", {"id": tmid}, {"X-Agent-Token": tokT})
    check("G1 claim ok", st == 200, T(raw)[:120])
    time.sleep(5)
    st, raw, _ = req(BASE, "GET", "/a/tasks", None, {"X-Agent-Token": tokT})
    check("G2 auto-release to OPEN", re.search(r"#%d \[OPEN\]" % tmid, T(raw)) is not None, T(raw)[:200])
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokT})
    check("G3 timeout log in chat", "auto-released (timeout)" in T(raw))

    # ---------- H. files ----------
    blob = bytes(__import__("random").getrandbits(8) for _ in range(100000))
    body_, ct = multipart({"note": "selftest data"}, "blob.bin", blob)
    st, raw, _ = req(BASE, "POST", "/a/file", body_, {"X-Agent-Token": tokA, "Content-Type": ct})
    txt = T(raw)
    m = re.search(r"file_id=(\d+)", txt)
    fid = int(m.group(1)) if m else 0
    check("H1 upload", st == 200 and fid and "expires_at=" in txt, txt[:150])
    st, raw, _ = req(BASE, "GET", "/a/files", None, {"X-Agent-Token": tokB})
    check("H2 files list", ("#%d " % fid) in T(raw) and "blob.bin" in T(raw))
    st, raw, _ = req(BASE, "GET", "/a/file?id=%d" % fid, None, {"X-Agent-Token": tokB})
    check("H3 download integrity", st == 200 and hashlib.sha256(raw).digest() == hashlib.sha256(blob).digest())
    st, raw, _ = req(BASE, "POST", "/a/file/confirm", {"id": fid}, {"X-Agent-Token": tokB})
    check("H4 confirm wipes", st == 200 and "wiped" in T(raw))
    st, raw, _ = req(BASE, "GET", "/a/file?id=%d" % fid, None, {"X-Agent-Token": tokB})
    check("H5 gone after confirm 404", st == 404)

    bslug, st, _b = mkroom(BASE, max_file_mb="0.0005")  # ~524 B
    st, body = join(BASE, bslug, "BigAgent")
    tokBig = tok_of(body)
    body_, ct = multipart({}, "toobig.txt", b"x" * 2048)
    st, raw, _ = req(BASE, "POST", "/a/file", body_, {"X-Agent-Token": tokBig, "Content-Type": ct})
    check("H6 TOO_BIG + hint", st == 413 and T(raw).startswith("TOO_BIG") and "external host" in T(raw), T(raw)[:120])

    lslug, st, _b = mkroom(BASE, file_ttl_minutes="0.08")  # ~4.8 s
    st, body = join(BASE, lslug, "TtlAgent")
    tokTtl = tok_of(body)
    body_, ct = multipart({}, "shortlived.txt", b"ttl data")
    st, raw, _ = req(BASE, "POST", "/a/file", body_, {"X-Agent-Token": tokTtl, "Content-Type": ct})
    m = re.search(r"file_id=(\d+)", T(raw))
    fid2 = int(m.group(1)) if m else 0
    check("H7 ttl upload", st == 200 and fid2)
    time.sleep(6)
    st, raw, _ = req(BASE, "GET", "/a/files", None, {"X-Agent-Token": tokTtl})
    check("H8 ttl purged", "count=0" in T(raw) and ("#%d" % fid2) not in T(raw), T(raw)[:120])
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokTtl})
    check("H9 ttl log in chat", "expired and was purged" in T(raw))

    # ---------- I. admin ----------
    st, raw, _ = req(BASE, "POST", "/admin/api/login", {"room": slug, "password": "nope"})
    check("I1 bad login 403", st == 403)
    st, raw, _ = req(BASE, "POST", "/admin/api/login", {"room": slug, "password": ADMIN_PASS})
    atok = json.loads(raw).get("token") if st == 200 else None
    check("I2 login ok", st == 200 and atok)
    AH = {"X-Admin-Token": atok or ""}
    st, raw, _ = req(BASE, "GET", "/admin/api/state?since=0", None, AH)
    js = json.loads(raw) if st == 200 else {}
    check("I3 state sees chat", st == 200 and js.get("ok")
          and any("hello from Alpha" in m["text"] for m in js.get("messages", [])))
    check("I4 state structure", all(k in js for k in ("agents", "tasks_active", "tasks_closed", "files", "last_id")))
    st, raw, _ = req(BASE, "GET", "/admin/api/state?since=0")
    check("I5 state unauthorized 401", st == 401)
    st, raw, _ = req(BASE, "POST", "/admin/api/chat", {"text": "Привет, агенты!"}, AH)
    check("I6 admin chat send", st == 200)
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokA})
    check("I7 admin msg visible to agents", "ADMIN (human): Привет, агенты!" in T(raw))
    st, raw, _ = req(BASE, "POST", "/admin/api/task/create", {"title": "Admin task", "priority": "URGENT"}, AH)
    atid = json.loads(raw).get("id") if st == 200 else 0
    check("I8 admin create task", st == 200 and atid)
    st, raw, _ = req(BASE, "GET", "/a/tasks", None, {"X-Agent-Token": tokA})
    check("I9 agent sees admin task", ('#%d [OPEN] URGENT "Admin task" by=ADMIN (human)' % atid) in T(raw))
    st, raw, _ = req(BASE, "POST", "/admin/api/task/status", {"id": atid, "status": "DONE", "reason": "verified by admin"}, AH)
    check("I10 admin status change", st == 200)
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokA})
    check("I11 admin status log in chat",
          ('ADMIN (human): set task #%d "Admin task" status -> DONE (reason: verified by admin)' % atid) in T(raw))
    st, raw, _ = req(BASE, "POST", "/admin/api/task/comment", {"id": tid, "text": "admin note"}, AH)
    check("I12 admin comment", st == 200)
    st, raw, _ = req(BASE, "GET", "/a/tasks?id=%d" % tid, None, {"X-Agent-Token": tokA})
    check("I13 admin comment visible", "admin note" in T(raw))
    st, raw, _ = req(BASE, "POST", "/admin/api/task/create", {"title": "Doomed"}, AH)
    dtid = json.loads(raw).get("id") if st == 200 else 0
    st, raw, _ = req(BASE, "POST", "/admin/api/task/delete", {"id": dtid, "reason": "test"}, AH)
    check("I14 admin delete", st == 200)
    st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokA})
    check("I15 delete tombstone in chat", ('DELETED task #%d "Doomed" (reason: test)' % dtid) in T(raw))
    body_, ct = multipart({}, "purge_me.txt", b"purge")
    st, raw, _ = req(BASE, "POST", "/a/file", body_, {"X-Agent-Token": tokA, "Content-Type": ct})
    pfid = int(re.search(r"file_id=(\d+)", T(raw)).group(1))
    st, raw, _ = req(BASE, "POST", "/admin/api/file/purge", {"id": pfid}, AH)
    st, raw, _ = req(BASE, "GET", "/a/files", None, {"X-Agent-Token": tokA})
    check("I16 admin file purge", st == 200 and ("#%d " % pfid) not in T(raw))

    # ---------- J. concurrency ----------
    st, txt, ctid = create_task(BASE, tokA, "conflict drill")
    res = {}
    barrier = threading.Barrier(2)

    def racer(nm, tk):
        barrier.wait()
        s, r, _ = req(BASE, "POST", "/a/task/claim", {"id": ctid}, {"X-Agent-Token": tk})
        res[nm] = (s, T(r))

    ths = [threading.Thread(target=racer, args=("A", tokA)), threading.Thread(target=racer, args=("B", tokB))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    oks = [n for n, (s, t_) in res.items() if s == 200 and t_.startswith("OK")]
    busy = [n for n, (s, t_) in res.items() if s == 409 and t_.startswith("BUSY")]
    check("J1 parallel claim: exactly one winner", len(oks) == 1 and len(busy) == 1, str({k: v[0] for k, v in res.items()}))

    okc = []
    lk = threading.Lock()

    def burst(i):
        s, r, _ = req(BASE, "POST", "/a/chat", {"text": "burst %d" % i}, {"X-Agent-Token": tokA})
        with lk:
            okc.append(s == 200 and T(r).startswith("OK msg_id="))

    ths = [threading.Thread(target=burst, args=(i,)) for i in range(12)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("J2 burst 12 parallel msgs", all(okc) and len(okc) == 12, str(len(okc)))

    # ---------- K. E2E rooms, roominfo, server-stat privacy ----------
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        HAVE_CRYPTO = True
    except Exception:
        HAVE_CRYPTO = False
        print("NOTE: 'cryptography' is not installed -> E2E round-trip checks are skipped")

    st, raw, _ = req(BASE, "GET", "/admin/api/roominfo?room=" + slug)
    j = json.loads(raw) if st == 200 else {}
    check("K1 roominfo: exists + e2e off", st == 200 and j.get("exists") is True and j.get("e2e") is False, T(raw)[:120])
    st, raw, _ = req(BASE, "GET", "/admin/api/roominfo?room=zz-no-such-slug-zz")
    j = json.loads(raw) if st == 200 else {}
    check("K2 roominfo: free name", st == 200 and j.get("exists") is False, T(raw)[:120])

    E2EP = "e2e-super-pass-9"
    eslug, st, ebody = mkroom(BASE, e2e="1", e2e_pass=E2EP)
    check("K3 create e2e room", st == 200 and "Комната создана" in ebody and "Шифрование E2E включено" in ebody, ebody[:200])
    st, raw, _ = req(BASE, "GET", "/admin/api/roominfo?room=" + eslug)
    j = json.loads(raw) if st == 200 else {}
    check("K4 roominfo: e2e on", st == 200 and j.get("exists") is True and j.get("e2e") is True, T(raw)[:120])
    st, raw, _ = req(BASE, "POST", "/admin/api/login", {"room": eslug, "password": ADMIN_PASS})
    je = json.loads(raw) if st == 200 else {}
    atoke = je.get("token")
    check("K5 e2e admin login flag", st == 200 and je.get("e2e") is True, T(raw)[:120])
    st, ebody2 = join(BASE, eslug, "E2EAgent")
    check("K6 join announces E2E ON", st == 200 and "E2E: ON" in ebody2, ebody2[:160])
    st, ebody3 = join(BASE, eslug, "E2EAgent2")
    tokE = tok_of(ebody3)

    if HAVE_CRYPTO and tokE and atoke:
        secret = "секретная фраза AgentsNET 123"
        st, raw, _ = req(BASE, "POST", "/a/chat", {"text": e2e_encrypt(E2EP, eslug, secret)},
                         {"X-Agent-Token": tokE})
        check("K7 e2e chat send", st == 200 and T(raw).startswith("OK msg_id="), T(raw)[:100])
        st, raw, _ = req(BASE, "GET", "/a/chat?page=1", None, {"X-Agent-Token": tokE})
        check("K8 e2e chat decrypts on read", secret in e2e_sub(T(raw), E2EP, eslug), T(raw)[:200])
        st, raw, _ = req(BASE, "GET", "/admin/api/state?since=0", None, {"X-Admin-Token": atoke})
        raws = T(raw)
        check("K9 admin state stores ciphertext only", st == 200 and secret not in raws and "enc:v1:" in raws,
              "leak" if (st == 200 and secret in raws) else T(raw)[:120])
        st, raw, _ = req(BASE, "POST", "/a/task/create",
                         {"title": e2e_encrypt(E2EP, eslug, "secret task title"),
                          "desc": e2e_encrypt(E2EP, eslug, "top secret desc")}, {"X-Agent-Token": tokE})
        check("K10 e2e task create", st == 200, T(raw)[:120])
        st, raw, _ = req(BASE, "GET", "/a/tasks", None, {"X-Agent-Token": tokE})
        out = e2e_sub(T(raw), E2EP, eslug)
        tid_m = re.search(r"#(\d+) \[OPEN\]", out)
        st2, raw2, _ = req(BASE, "GET", "/a/tasks?id=" + (tid_m.group(1) if tid_m else "0"),
                           None, {"X-Agent-Token": tokE})
        out2 = e2e_sub(T(raw2), E2EP, eslug)
        check("K11 e2e task texts decrypt", "secret task title" in out and "top secret desc" in out2,
              out[:120] + " / " + out2[:120])
        blob = b"print('hello AgentsNET E2E')\n" * 10
        nonce = os.urandom(12)
        enc_blob = E2E_MAGIC + nonce + AESGCM(e2e_key(E2EP, eslug)).encrypt(nonce, b"code.py\n" + blob, None)
        body_, ct = multipart({}, "file.enc", enc_blob)
        st, raw, _ = req(BASE, "POST", "/a/file", body_, {"X-Agent-Token": tokE, "Content-Type": ct})
        m = re.search(r"file_id=(\d+)", T(raw))
        efid = int(m.group(1)) if m else 0
        check("K12 e2e file upload as file.enc", st == 200 and efid and "file.enc" in T(raw), T(raw)[:150])
        st, raw, _ = req(BASE, "GET", "/a/file?id=%d" % efid, None, {"X-Agent-Token": tokE})
        dec_ok = st == 200 and raw[:8] == E2E_MAGIC
        if dec_ok:
            try:
                pt = AESGCM(e2e_key(E2EP, eslug)).decrypt(raw[8:20], raw[20:], None)
                dec_ok = pt.startswith(b"code.py\n") and pt[8:] == blob
            except Exception as ex:
                dec_ok = False
        check("K13 e2e file round-trip", dec_ok)
        st, raw, _ = req(BASE, "GET", "/admin/api/export", None, {"X-Admin-Token": atoke})
        check("K14 export stores ciphertext only", st == 200 and secret not in T(raw) and "enc:v1:" in T(raw), T(raw)[:150])
    elif not HAVE_CRYPTO:
        print("  (K7-K14 skipped: cryptography missing)")
    if tokE:
        st, raw, _ = req(BASE, "POST", "/a/chat", {"text": "plain in e2e room"}, {"X-Agent-Token": tokE})
        check("K15 plaintext still accepted in e2e room (compat)", st == 200, T(raw)[:100])

    st, raw, _ = req(BASE, "GET", "/health")
    j = json.loads(raw) if st == 200 else {}
    check("K16 health hides server-wide stats", st == 200 and all(k not in j for k in ("rooms", "agents", "tasks_active")), T(raw)[:150])

    # ---------- summary ----------
    dur = time.time() - t0
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n" + "=" * 62)
    print("SELFTEST: %d checks, %d passed, %d failed  (%.1fs)" % (len(RESULTS), passed, failed, dur))
    if failed:
        print("Failures:")
        for name, ok, extra in RESULTS:
            if not ok:
                print("  - %s | %s" % (name, extra))
    print("=" * 62)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
