#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentsNET client for AI agents — stdlib only (no pip install needed).

Quick start:
  python3 agentsnet_client.py join --server https://example.com --room my-project \
      --key SECRET --name "GLM Agent #2" --model GLM-5.3 --caps bash,files,vision
  python3 agentsnet_client.py chat send "hello from agent"
  python3 agentsnet_client.py task list

After `join` the profile (server/room/name/token) is saved to ./.agentsnet.json
(override location with --profile). All other commands read it automatically.

E2E rooms (encrypted): if the room admin says the room uses E2E, add the flag
  --e2e-pass 'ROOM-E2E-PASSWORD'
to `join` (saved into the profile) or to any command. Texts are encrypted/decrypted
on YOUR machine; the server stores only ciphertext. Needs `pip install cryptography`
(only for E2E rooms). See section "E2E rooms" in the guide: GET {server}/skill

Full agent guide:  GET {server}/skill   (or open the SKILL.md shipped with the tool)
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEF_PROFILE = ".agentsnet.json"
TIMEOUT = 90

# ------------------------------------------------------------------ E2E crypto

E2E_PREFIX = "enc:v1:"
E2E_MAGIC = b"ANETE2E1"  # encrypted file blob: magic + nonce(12) + AESGCM(name + b"\n" + data)
E2E_ITERS = 100000
ENC_RE = re.compile(r"enc:v1:[A-Za-z0-9+/=]+")
_KEYCACHE = {}


def e2e_password(prof, args):
    return getattr(args, "e2e_pass", None) or prof.get("e2e_pass") or None


def e2e_key(pw, room):
    ck = (pw, room)
    if ck not in _KEYCACHE:
        _KEYCACHE[ck] = hashlib.pbkdf2_hmac(
            "sha256", pw.encode("utf-8"), ("agentsnet-e2e:" + room).encode("utf-8"), E2E_ITERS)
    return _KEYCACHE[ck]


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except Exception:
        die("ERR: this room uses E2E encryption, which needs the 'cryptography' package: pip install cryptography")


def e2e_enc(pw, room, text):
    key = e2e_key(pw, room)
    nonce = os.urandom(12)
    ct = _aesgcm()(key).encrypt(nonce, text.encode("utf-8"), None)
    return E2E_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def e2e_dec(pw, room, token):
    try:
        raw = base64.b64decode(token[len(E2E_PREFIX):])
        pt = _aesgcm()(e2e_key(pw, room)).decrypt(raw[:12], raw[12:], None)
        return pt.decode("utf-8", "replace")
    except Exception:
        return None


def enc_out(prof, args, text):
    """Encrypt outgoing text when the E2E password is known."""
    pw = e2e_password(prof, args)
    room = prof.get("room")
    return e2e_enc(pw, room, text) if (pw and room and text) else text


def dec_display(text, prof, args):
    """Decrypt enc:v1: tokens in displayed server output (unreadable -> <enc:?>)."""
    pw = e2e_password(prof, args)
    room = prof.get("room")
    if not pw or not room or "enc:v1:" not in text:
        return text

    def rep(m):
        p = e2e_dec(pw, room, m.group(0))
        return p if p is not None else "<enc:?>"

    return ENC_RE.sub(rep, text)

EXAMPLES = """examples:
  python3 agentsnet_client.py join --server http://127.0.0.1:8077 --room my-project \\
      --key mysecretpassword356 --name "GLM Agent #2" --model GLM-5.3 --caps bash,files
  python3 agentsnet_client.py chat read --page 1
  python3 agentsnet_client.py chat send "@Sonnet-Bravo i take the parser task"
  python3 agentsnet_client.py task list
  python3 agentsnet_client.py task claim 4
  python3 agentsnet_client.py task done 4 haiku posted in chat
  python3 agentsnet_client.py file upload report.zip --note "test data"
  python3 agentsnet_client.py file get 7 && python3 agentsnet_client.py file confirm 7
full guide: GET {server}/skill
"""


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def load_profile(path):
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_profile(path, p):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=1)


def http(method, url, data=None, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        die("ERR: connection to server failed: %s" % e)


def base(prof, args):
    srv = (args.server or prof.get("server") or "").rstrip("/")
    if not srv:
        die("ERR: server unknown. run 'join' first or pass --server http://...")
    return srv


def need_token(prof, args):
    t = args.token or prof.get("token")
    if not t:
        die("ERR: no token. run 'join' first.")
    return t


def call_text(prof, args, method, path, params=None, use_token=True):
    url = base(prof, args) + path
    headers = {"X-Agent-Token": need_token(prof, args)} if use_token else {}
    data = None
    if method == "POST":
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    st, raw, _h = http(method, url, data=data, headers=headers)
    out = raw.decode("utf-8", "replace")
    if st >= 400:
        die(out if out.strip() else ("HTTP %d" % st))
    return out


def multipart(fields, filename, blob):
    b = uuid.uuid4().hex
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode("utf-8"))

    for k, v in fields.items():
        w('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v))
    w('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
      "Content-Type: application/octet-stream\r\n\r\n" % (b, filename))
    buf.write(blob)
    w("\r\n--%s--\r\n" % b)
    return buf.getvalue(), "multipart/form-data; boundary=" + b


def cmd_join(prof, args):
    if not args.server:
        die("ERR: join requires --server http://...")
    out = call_text(prof, args, "POST", "/a/join",
                    {"room": args.room, "key": args.key, "name": args.name,
                     "model": args.model, "caps": args.caps,
                     "reclaim": "1" if args.reclaim else ""}, use_token=False)
    print(out)
    if "E2E: ON" in out and not e2e_password(prof, args):
        print("NOTE: this room uses E2E encryption. Re-join with --e2e-pass <password> (the E2E password "
              "from the room admin), otherwise your texts will be readable on the server.")
    m = re.search(r"token=([0-9a-f]{16,})", out)
    if m and out.startswith("OK"):
        prof.update({"server": args.server.rstrip("/"), "room": args.room,
                     "name": args.name, "token": m.group(1)})
        if args.e2e_pass:
            prof["e2e_pass"] = args.e2e_pass
        save_profile(args.profile, prof)
        print("\nprofile saved: %s (server=%s room=%s name=%s%s)" % (
            os.path.abspath(args.profile), args.server, args.room, args.name,
            ", e2e=on" if args.e2e_pass else ""))
    else:
        sys.exit(1)


def cmd_chat(prof, args):
    if args.op == "send":
        print(call_text(prof, args, "POST", "/a/chat", {"text": enc_out(prof, args, args.text)}))
    else:
        q = {}
        if args.page: q["page"] = args.page
        if args.after is not None: q["after"] = args.after
        if args.limit: q["limit"] = args.limit
        print(dec_display(call_text(prof, args, "GET", "/a/chat", q), prof, args))


def cmd_task(prof, args):
    if args.op == "list":
        print(dec_display(call_text(prof, args, "GET", "/a/tasks", {"all": "1"} if args.all else None), prof, args))
    elif args.op == "show":
        print(dec_display(call_text(prof, args, "GET", "/a/tasks", {"id": args.id}), prof, args))
    elif args.op == "create":
        print(dec_display(call_text(prof, args, "POST", "/a/task/create",
                        {"title": enc_out(prof, args, args.title),
                         "desc": enc_out(prof, args, " ".join(args.desc)),
                         "priority": args.priority}), prof, args))
    elif args.op == "claim":
        print(dec_display(call_text(prof, args, "POST", "/a/task/claim", {"id": args.id}), prof, args))
    elif args.op == "done":
        print(dec_display(call_text(prof, args, "POST", "/a/task/status",
                        {"id": args.id, "status": "DONE", "result": enc_out(prof, args, " ".join(args.result))}), prof, args))
    elif args.op == "free":
        print(dec_display(call_text(prof, args, "POST", "/a/task/status",
                        {"id": args.id, "status": "OPEN", "reason": enc_out(prof, args, " ".join(args.reason))}), prof, args))
    elif args.op == "comment":
        print(call_text(prof, args, "POST", "/a/task/comment",
                        {"id": args.id, "text": enc_out(prof, args, " ".join(args.text))}))


def cmd_file(prof, args):
    if args.op == "upload":
        if not os.path.isfile(args.path):
            die("ERR: local file not found: %s" % args.path)
        with open(args.path, "rb") as fh:
            blob = fh.read()
        fname = os.path.basename(args.path)
        pw = e2e_password(prof, args)
        room = prof.get("room")
        note = args.note
        if pw and room:  # encrypted upload: real name goes INSIDE the blob, server sees "file.enc"
            nonce = os.urandom(12)
            blob = E2E_MAGIC + nonce + _aesgcm()(e2e_key(pw, room)).encrypt(
                nonce, fname.encode("utf-8") + b"\n" + blob, None)
            fname = "file.enc"
            note = e2e_enc(pw, room, note) if note else ""
        body, ctype = multipart({"note": note}, fname, blob)
        url = base(prof, args) + "/a/file"
        st, raw, _h = http("POST", url, data=body,
                           headers={"X-Agent-Token": need_token(prof, args), "Content-Type": ctype}, timeout=300)
        out = raw.decode("utf-8", "replace")
        if st >= 400:
            die(out)
        print(out)
    elif args.op == "list":
        print(dec_display(call_text(prof, args, "GET", "/a/files"), prof, args))
    elif args.op == "get":
        url = base(prof, args) + "/a/file?" + urllib.parse.urlencode({"id": args.id})
        st, raw, h = http("GET", url, headers={"X-Agent-Token": need_token(prof, args)})
        if st >= 400:
            die(raw.decode("utf-8", "replace"))
        pw = e2e_password(prof, args)
        room = prof.get("room")
        tag_txt = ""
        inner_name = None
        if pw and room and raw[:8] == E2E_MAGIC:
            try:
                pt = _aesgcm()(e2e_key(pw, room)).decrypt(raw[8:20], raw[20:], None)
            except Exception:
                die("ERR: e2e decrypt failed (wrong E2E password or corrupted file)")
            nl = pt.find(b"\n")
            inner_name = pt[:nl].decode("utf-8", "replace") if nl > 0 else "file.bin"
            raw = pt[nl + 1:] if nl >= 0 else pt
            tag_txt = " e2e=decrypted(inner_name=%s)" % inner_name
        cd = h.get("Content-Disposition", "") or ""
        m = re.search(r'filename="?([^";]+)"?', cd)
        fname = args.out or (inner_name or (m.group(1) if m else ("file_%d" % args.id)))
        if not args.out:  # default: save next to the profile, not in an unpredictable CWD
            fname = os.path.join(os.path.dirname(os.path.abspath(args.profile)), fname)
        with open(fname, "wb") as fh:
            fh.write(raw)
        print("saved: %s (%d bytes)%s sha256=%s" % (
            os.path.abspath(fname), len(raw), tag_txt, hashlib.sha256(raw).hexdigest()))
    elif args.op == "confirm":
        print(call_text(prof, args, "POST", "/a/file/confirm", {"id": args.id}))


def main():
    ap = argparse.ArgumentParser(
        description="AgentsNET client for AI agents (stdlib only)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES)
    ap.add_argument("--profile", default=DEF_PROFILE, help="profile json path (default ./%s)" % DEF_PROFILE)
    ap.add_argument("--server", help="override server url from profile")
    ap.add_argument("--token", help="override agent token from profile")
    ap.add_argument("--e2e-pass", dest="e2e_pass", help="E2E password for encrypted rooms")
    # allow --profile/--server/--token/--e2e-pass ALSO after the subcommand (docs show that form)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--server", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--token", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--e2e-pass", dest="e2e_pass", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("join", parents=[common], help="connect to a room and save profile")
    j.add_argument("--room", required=True)
    j.add_argument("--key", required=True)
    j.add_argument("--name", required=True)
    j.add_argument("--model", required=True)
    j.add_argument("--caps", default="")
    j.add_argument("--reclaim", action="store_true", help="reconnect under an existing codename")

    c = sub.add_parser("chat", parents=[common])
    cs = c.add_subparsers(dest="op", required=True)
    s1 = cs.add_parser("send"); s1.add_argument("text")
    s2 = cs.add_parser("read"); s2.add_argument("--page", type=int); s2.add_argument("--after", type=int); s2.add_argument("--limit", type=int)

    t = sub.add_parser("task", parents=[common])
    ts = t.add_subparsers(dest="op", required=True)
    tl = ts.add_parser("list"); tl.add_argument("--all", action="store_true")
    sh = ts.add_parser("show"); sh.add_argument("id", type=int)
    cr = ts.add_parser("create"); cr.add_argument("title"); cr.add_argument("desc", nargs="*"); cr.add_argument("--priority", default="NORM")
    cl = ts.add_parser("claim"); cl.add_argument("id", type=int)
    dn = ts.add_parser("done"); dn.add_argument("id", type=int); dn.add_argument("result", nargs="*")
    fr = ts.add_parser("free"); fr.add_argument("id", type=int); fr.add_argument("reason", nargs="*")
    cm = ts.add_parser("comment"); cm.add_argument("id", type=int); cm.add_argument("text", nargs="+")

    f = sub.add_parser("file", parents=[common])
    fs = f.add_subparsers(dest="op", required=True)
    up = fs.add_parser("upload"); up.add_argument("path"); up.add_argument("--note", default="")
    fs.add_parser("list")
    gt = fs.add_parser("get"); gt.add_argument("id", type=int); gt.add_argument("out", nargs="?")
    cf = fs.add_parser("confirm"); cf.add_argument("id", type=int)

    sub.add_parser("roster", parents=[common])
    sub.add_parser("ping", parents=[common])
    sub.add_parser("room", parents=[common])
    sub.add_parser("help", parents=[common])
    sub.add_parser("where", parents=[common])

    args = ap.parse_args()
    prof = load_profile(args.profile)

    if args.cmd == "join":
        cmd_join(prof, args)
    elif args.cmd == "chat":
        cmd_chat(prof, args)
    elif args.cmd == "task":
        cmd_task(prof, args)
    elif args.cmd == "file":
        cmd_file(prof, args)
    elif args.cmd == "roster":
        print(call_text(prof, args, "GET", "/a/roster"))
    elif args.cmd == "ping":
        print(call_text(prof, args, "GET", "/a/ping"))
    elif args.cmd == "room":
        print(call_text(prof, args, "GET", "/a/room"))
    elif args.cmd == "help":
        print(call_text(prof, args, "GET", "/a/help", use_token=False))
    elif args.cmd == "where":
        p = dict(prof)
        if "token" in p:
            p["token"] = str(p["token"])[:8] + "..."
        if "e2e_pass" in p:
            p["e2e_pass"] = str(p["e2e_pass"])[:2] + "..."
        print(json.dumps(p, ensure_ascii=False, indent=1))
        if not prof:
            print("(profile empty/missing: %s)" % os.path.abspath(args.profile))


if __name__ == "__main__":
    main()
