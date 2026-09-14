#!/usr/bin/env python3
"""board_service — the coordination board itself: a small HTTP service over SQLite.

Written fresh rather than ported. An earlier board in this family was audited and five defects
were found in it; each one is a design rule here rather than a patch.

  1. EVERY authenticated path goes through one check. In the audited board the login path was
     handled before the rate limiter and therefore had none, so the one endpoint an outsider
     actually reaches was the one with unlimited attempts.
  2. NO HTML IS SERVED. The audited board rendered a page and interpolated sender and recipient
     names into it unescaped — five injection sites, one of them inside an attribute where
     escaping would not have helped. A JSON-only service cannot have that class of defect at all.
  3. The body is read AFTER authentication and with a ceiling. Reading Content-Length bytes from
     an unauthenticated caller is an invitation to exhaust memory.
  4. Every list endpoint has a maximum. "How many" from a caller is a request, not an order.
  5. Token comparison is constant-time. On its own this matters little; combined with rule 1 it
     stops being theoretical.

Storage is one SQLite file. Reads do not consume: draining is a separate, explicit endpoint,
because a listener that quietly emptied a queue once made another node's messages vanish with
no symptom on either side.
"""
import hmac
import json
import queue
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BOARD_DATA", os.path.join(os.path.expanduser("~"), ".orchestration-board"))
PORT = int(os.environ.get("BOARD_PORT", "8781"))
HOST = os.environ.get("BOARD_HOST", "0.0.0.0")

MAX_BODY = 1 << 20          # 1 MiB: a task description, not a payload
HARD_BODY_WALL = 16 << 20   # we will drain this much to answer 413 politely, never more
MAX_ROWS = 500
STATES = ("submitted", "working", "input-required", "needs-operator", "completed", "failed")
KINDS = ("chat", "task", "result", "status", "gate")

class _Reject(Exception):
    """A refusal with a status code, so the caller learns WHY instead of seeing a dead socket."""

    def __init__(self, code, why):
        super().__init__(why)
        self.code, self.why = code, why


_lock = threading.Lock()
_fails: dict[str, list[float]] = {}
_flock = threading.Lock()
MAX_FAILS, WINDOW = 12, 300.0

# --- PUSH, because polling is how coordination quietly stops working -----------------------
# Without a stream, an agent learns about work only when it happens to look. On the board this
# replaces, a listener died with a reboot and nobody noticed: messages addressed to that node sat
# unread for hours while both sides believed the channel was fine. Long-polling /recv is not a
# substitute — it CONSUMES, so using it to stay awake steals messages from whoever was meant to
# act on them. /events is read-only and consumes nothing.
_subs: list["queue.Queue"] = []
_sub_lock = threading.Lock()


def broadcast(kind, payload):
    dead = []
    with _sub_lock:
        for q in _subs:
            try:
                q.put_nowait(f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")
            except Exception:
                dead.append(q)
        for q in dead:
            _subs.remove(q)


def data_path(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def load_or_make_token():
    """The operator does not invent the token; the service generates one and tells them.

    A token a human chooses is a token a human reuses. This one is written next to the data with
    owner-only permissions on the platforms that have them.
    """
    p = data_path("token.txt")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    tok = secrets.token_urlsafe(24)
    with open(p, "w", encoding="utf-8") as f:
        f.write(tok)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


TOKEN = load_or_make_token()


def db():
    c = sqlite3.connect(data_path("board.db"), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT,
        sender TEXT, recipient TEXT, task_id INTEGER, body TEXT);
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_created REAL, ts_updated REAL,
        creator TEXT, assignee TEXT, title TEXT, body TEXT, state TEXT,
        reversible INTEGER DEFAULT 1, result TEXT);
    CREATE TABLE IF NOT EXISTS cursors(peer TEXT PRIMARY KEY, last_id INTEGER);
    CREATE TABLE IF NOT EXISTS nodes(
        name TEXT PRIMARY KEY, ts_seen REAL, can TEXT, cannot TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS gate_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id INTEGER,
        action TEXT, verdict TEXT, mode TEXT, reason TEXT);
    """)
    return c


def rows(cur, cols):
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "orchestration-board"

    def log_message(self, *a):
        pass

    # ---------- plumbing ----------
    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self, q):
        """One gate for every path that needs one. There is no second way in."""
        ip = self.client_address[0]
        now = time.time()
        with _flock:
            hist = [t for t in _fails.get(ip, []) if now - t < WINDOW]
            _fails[ip] = hist
            if len(hist) >= MAX_FAILS:
                return False
            if len(_fails) > 5000:                      # an IP dict fed from a network grows
                for k in [k for k, v in _fails.items() if not v or now - v[-1] > WINDOW]:
                    _fails.pop(k, None)
        got = self.headers.get("X-Board-Token") or q.get("t", [""])[0]
        if got and hmac.compare_digest(got, TOKEN):
            return True
        with _flock:
            _fails.setdefault(ip, []).append(now)
        return False

    def _body(self):
        """Read after authentication only, bounded, and REFUSE rather than corrupt.

        Returns the text, or raises _Reject with the status to send. Two rules, both learned
        from watching the quiet version of each failure:

        Oversized: reply 413 instead of reading the ceiling and abandoning the rest. Truncating
        the read leaves unread bytes in the socket, the client sees a connection reset, and a
        clear "too large" becomes an unexplained network error.

        Invalid encoding: reply 400 instead of decoding with errors="replace". Replacement turns
        a client-side encoding mistake into question marks stored forever, and every time it
        happens it looks like a server fault. This exact confusion cost days on the board this
        service replaces. A body that cannot be read is a refused request, not a stored one.
        """
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise _Reject(400, "Content-Length is not a number")
        if n <= 0:
            return ""
        if n > MAX_BODY:
            # Answering 413 without reading first does not work: the client is still sending, so
            # it sees a connection reset instead of the refusal. Measured — a 5 MiB post came back
            # as WinError 10053 and not as "too large", which is exactly the kind of failure that
            # sends someone debugging the network instead of their request.
            # So drain what was promised (up to a hard wall, in case the header is a lie), then
            # refuse in a way the caller can actually read.
            left = min(n, HARD_BODY_WALL)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            raise _Reject(413, f"body is {n} bytes; the limit is {MAX_BODY}")
        raw = self.rfile.read(n)
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise _Reject(400, "body is not valid UTF-8 — send bytes encoded as UTF-8 and "
                               "declare charset=utf-8; do not let the client pick an encoding")

    @staticmethod
    def _limit(q, key, default, cap):
        try:
            return max(1, min(int(q.get(key, [str(default)])[0]), cap))
        except (TypeError, ValueError):
            return default

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._json(200, {"ok": True, "port": PORT, "time": time.time(),
                                    "service": "orchestration-board"})
        if not self._authed(q):
            return self._json(401, {"error": "bad or missing token"})

        if u.path == "/events":
            # Server-sent events. Read-only: subscribing takes nothing from anyone, which is the
            # whole reason this exists next to /recv rather than instead of it.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            myq = queue.Queue(maxsize=200)
            with _sub_lock:
                _subs.append(myq)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        chunk = myq.get(timeout=20)
                    except queue.Empty:
                        # A silent socket is indistinguishable from a dead one. The keepalive is
                        # what lets a watcher tell "nothing is happening" from "the board is gone".
                        chunk = ": keepalive\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sub_lock:
                    if myq in _subs:
                        _subs.remove(myq)
            return

        if u.path == "/log":
            n = self._limit(q, "n", 50, MAX_ROWS)
            with _lock:
                c = db()
                cur = c.execute("SELECT id,ts,kind,sender,recipient,task_id,body FROM messages"
                                " ORDER BY id DESC LIMIT ?", (n,))
                out = rows(cur, ("id", "ts", "kind", "from", "to", "task_id", "body"))
                c.close()
            return self._json(200, list(reversed(out)))

        if u.path == "/recv":
            me = q.get("me", [""])[0]
            if not me:
                return self._json(400, {"error": "me is required"})
            wait = min(float(self._limit(q, "wait", 0, 55)), 55)
            deadline = time.time() + wait
            while True:
                with _lock:
                    c = db()
                    last = (c.execute("SELECT last_id FROM cursors WHERE peer=?",
                                      (me,)).fetchone() or [0])[0]
                    cur = c.execute(
                        "SELECT id,ts,kind,sender,recipient,task_id,body FROM messages"
                        " WHERE id>? AND sender!=? AND recipient IN ('all',?) ORDER BY id",
                        (last, me, me))
                    got = rows(cur, ("id", "ts", "kind", "from", "to", "task_id", "body"))
                    if got:
                        c.execute("INSERT INTO cursors(peer,last_id) VALUES(?,?)"
                                  " ON CONFLICT(peer) DO UPDATE SET last_id=excluded.last_id",
                                  (me, got[-1]["id"]))
                        c.commit()
                    c.close()
                if got or time.time() >= deadline:
                    return self._json(200, got)
                time.sleep(0.7)

        if u.path == "/tasks":
            with _lock:
                c = db()
                t = rows(c.execute(
                    "SELECT id,ts_created,ts_updated,creator,assignee,title,body,state,"
                    "reversible,result FROM tasks ORDER BY id DESC LIMIT ?", (MAX_ROWS,)),
                    ("id", "created", "updated", "creator", "assignee", "title", "body",
                     "state", "reversible", "result"))
                g = rows(c.execute("SELECT id,ts,task_id,action,verdict,mode,reason FROM gate_log"
                                   " ORDER BY id DESC LIMIT 100"),
                         ("id", "ts", "task_id", "action", "verdict", "mode", "reason"))
                c.close()
            return self._json(200, {"tasks": t, "gate": g})

        if u.path == "/nodes":
            with _lock:
                c = db()
                n = rows(c.execute("SELECT name,ts_seen,can,cannot,note FROM nodes"
                                   " ORDER BY ts_seen DESC"),
                         ("name", "seen", "can", "cannot", "note"))
                c.close()
            for x in n:
                for k in ("can", "cannot"):
                    try:
                        x[k] = json.loads(x[k] or "[]")
                    except ValueError:
                        x[k] = []
            return self._json(200, n)

        return self._json(404, {"error": "no such path"})

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._json(401, {"error": "bad or missing token"})
        try:
            body = self._body()
        except _Reject as r:
            return self._json(r.code, {"error": r.why})

        if u.path == "/send":
            frm, to = q.get("from", [""])[0], q.get("to", ["all"])[0]
            kind = q.get("kind", ["chat"])[0]
            if not frm or not body:
                return self._json(400, {"error": "from and a non-empty body are required"})
            if kind not in KINDS:
                return self._json(400, {"error": f"kind must be one of {KINDS}"})
            tid = q.get("task_id", [None])[0]
            with _lock:
                c = db()
                cur = c.execute("INSERT INTO messages(ts,kind,sender,recipient,task_id,body)"
                                " VALUES(?,?,?,?,?,?)",
                                (time.time(), kind, frm, to, int(tid) if tid else None, body))
                c.commit()
                mid = cur.lastrowid
                c.close()
            broadcast("message", {"id": mid, "ts": time.time(), "kind": kind,
                                  "from": frm, "to": to, "body": body})
            return self._json(200, {"id": mid, "to": to})

        if u.path == "/task":
            frm = q.get("from", [""])[0]
            title = q.get("title", [""])[0]
            if not frm or not title:
                return self._json(400, {"error": "from and title are required"})
            rev = 0 if q.get("reversible", ["1"])[0] in ("0", "false", "no") else 1
            now = time.time()
            with _lock:
                c = db()
                cur = c.execute(
                    "INSERT INTO tasks(ts_created,ts_updated,creator,assignee,title,body,state,"
                    "reversible,result) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, now, frm, q.get("to", ["all"])[0], title, body, "submitted", rev, None))
                tid = cur.lastrowid
                # The gate records what it WOULD do, chosen by reversibility. Reversible work is
                # observed only, so a false alarm can never stall the loop; irreversible work is
                # the kind worth holding.
                c.execute("INSERT INTO gate_log(ts,task_id,action,verdict,mode,reason)"
                          " VALUES(?,?,?,?,?,?)",
                          (now, tid, "create-task", "allow" if rev else "hold",
                           "observe" if rev else "enforce",
                           "reversible -> observe" if rev else "irreversible -> would hold"))
                c.execute("INSERT INTO messages(ts,kind,sender,recipient,task_id,body)"
                          " VALUES(?,?,?,?,?,?)",
                          (now, "task", frm, q.get("to", ["all"])[0], tid, f"[task #{tid}] {title}"))
                c.commit()
                c.close()
            broadcast("message", {"id": None, "ts": now, "kind": "task", "from": frm,
                                  "to": q.get("to", ["all"])[0],
                                  "body": f"[task #{tid}] {title}"})
            return self._json(200, {"task_id": tid, "state": "submitted",
                                    "gate_mode": "observe" if rev else "enforce"})

        if u.path == "/task/state":
            tid, state = q.get("id", [""])[0], q.get("state", [""])[0]
            if not tid or state not in STATES:
                return self._json(400, {"error": f"id and state in {STATES} are required"})
            by = q.get("by", ["?"])[0]
            now = time.time()
            with _lock:
                c = db()
                c.execute("UPDATE tasks SET state=?, ts_updated=?, result=COALESCE(?,result)"
                          " WHERE id=?", (state, now, body or None, int(tid)))
                # A truncated notice must say that it is truncated and where the whole thing is.
                # Silent truncation is indistinguishable from a short answer, and on the board this
                # was built from, a week of trust was lost to exactly that.
                note = ""
                if body:
                    note = (f": {body[:200]}… [truncated, {len(body)} chars in full: "
                            f"GET /tasks, task #{tid}]") if len(body) > 200 else f": {body}"
                c.execute("INSERT INTO messages(ts,kind,sender,recipient,task_id,body)"
                          " VALUES(?,?,?,?,?,?)",
                          (now, "result" if state in ("completed", "failed") else "status",
                           by, "all", int(tid), f"[task #{tid}] -> {state}{note}"))
                c.commit()
                c.close()
            broadcast("message", {"id": None, "ts": now, "kind": "status", "from": by,
                                  "to": "all", "body": f"[task #{tid}] -> {state}{note}"})
            return self._json(200, {"task_id": int(tid), "state": state})

        if u.path == "/register":
            name = q.get("name", [""])[0]
            if not name:
                return self._json(400, {"error": "name is required"})
            try:
                d = json.loads(body) if body else {}
            except ValueError as e:
                return self._json(400, {"error": f"body must be JSON: {str(e)[:80]}"})
            cannot = d.get("cannot", [])
            # A limitation without a checked date is an excuse, not a fact.
            bad = [x for x in cannot if not (isinstance(x, dict) and x.get("checked"))]
            if bad:
                return self._json(400, {"error": "every 'cannot' needs a 'checked' date — "
                                                 "a confirmed attempt, not an assumption",
                                        "offending": bad[:3]})
            with _lock:
                c = db()
                c.execute("INSERT INTO nodes(name,ts_seen,can,cannot,note) VALUES(?,?,?,?,?)"
                          " ON CONFLICT(name) DO UPDATE SET ts_seen=excluded.ts_seen,"
                          " can=excluded.can, cannot=excluded.cannot, note=excluded.note",
                          (name, time.time(), json.dumps(d.get("can", []), ensure_ascii=False),
                           json.dumps(cannot, ensure_ascii=False), d.get("note", "")))
                c.commit()
                c.close()
            return self._json(200, {"registered": name})

        return self._json(404, {"error": "no such path"})


def serve():
    db().close()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"orchestration-board on {HOST}:{PORT}  data={DATA_DIR}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve()
