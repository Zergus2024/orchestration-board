#!/usr/bin/env python3
"""OrchestrationBoardHub — runs the board on this machine and exposes it as tools.

Install this on ONE machine. It starts the board service itself, so nobody has to stand up a
server by hand; the other machines install the agent plugin and point at this one.

WHY THE SERVICE IS A DETACHED CHILD AND NOT PART OF THIS PROCESS.
A session is episodic — it exists while somebody is working in it. A board has to be ambient.
If the board lived inside the agent that hosts it, the record would vanish the moment the
operator closed the window, which is the failure this whole thing exists to prevent. So the
service is launched detached, keeps its own SQLite file, and outlives the session that started
it.

WHY EVERY STATUS ANSWER IS A MEASUREMENT.
`hub_start` does not report success because a process was spawned. It reports success because
something answered on the port. The distinction is not pedantry: on the board this replaces, a
completion notice once announced a deletion that a permission check had actually refused, and it
was caught only because a person looked at the disk instead of reading the report. A spawned
process that died at once and a healthy service look identical from the caller's side unless
somebody checks.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import MCPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BOARD_DATA", os.path.join(os.path.expanduser("~"), ".orchestration-board"))
PORT = int(os.environ.get("BOARD_PORT", "8781"))
HOST = os.environ.get("BOARD_HOST", "0.0.0.0")
ME = os.environ.get("BOARD_NODE", "hub")
LOCAL = f"http://127.0.0.1:{PORT}"

mcp = MCPServer(
    name="orchestration-board-hub",
    instructions="This machine hosts the board. Use hub_status to see whether the service is "
                 "actually answering, and hub_token to get the value other machines need. "
                 "Read with board_inbox; board_drain consumes and belongs to whatever component "
                 "must handle each message exactly once.",
)


def _paths():
    os.makedirs(DATA_DIR, exist_ok=True)
    return (os.path.join(DATA_DIR, "token.txt"),
            os.path.join(DATA_DIR, "board.pid"),
            os.path.join(DATA_DIR, "board.log"))


def _token():
    p = _paths()[0]
    return open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""


def _alive(timeout=2.0):
    """Answering, not merely spawned."""
    try:
        with urllib.request.urlopen(f"{LOCAL}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _call(path, params=None, body=None, method="GET"):
    q = dict(params or {})
    tok = _token()
    if tok:
        q["t"] = tok
    url = f"{LOCAL}{path}?{urllib.parse.urlencode(q)}" if q else f"{LOCAL}{path}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"board returned HTTP {e.code}",
                "detail": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"error": f"board not answering on {LOCAL}: {type(e).__name__}. "
                         f"Call hub_start."}


# ---------------------------------------------------------------- hub management

@mcp.tool()
def hub_status() -> dict:
    """Is the board actually running on this machine? Checked by asking it, not by assuming."""
    h = _alive()
    tok = _token()
    return {"running": h is not None,
            "health": h,
            "url_for_this_machine": LOCAL,
            "url_for_other_machines": f"http://<this machine's LAN address>:{PORT}",
            "data_dir": DATA_DIR,
            "token_configured": bool(tok)}


@mcp.tool()
def hub_start() -> dict:
    """Start the board service, detached, so it outlives this session.

    Returns only after something answers on the port — or after saying plainly that nothing did,
    with the last lines of the service log. A process that exits immediately must not be
    reported as a running board.
    """
    if _alive():
        return {"already_running": True, **hub_status()}
    tok_path, pid_path, log_path = _paths()
    env = dict(os.environ, BOARD_DATA=DATA_DIR, BOARD_PORT=str(PORT), BOARD_HOST=HOST,
               PYTHONUTF8="1")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(log_path, "a", encoding="utf-8")
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "board_service.py")],
                         cwd=HERE, env=env, stdout=log, stderr=log,
                         creationflags=flags, close_fds=True,
                         start_new_session=(os.name != "nt"))
    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(p.pid))

    for _ in range(30):
        time.sleep(0.4)
        h = _alive(1.0)
        if h:
            return {"started": True, "pid": p.pid, "health": h,
                    "token": _token(),
                    "next": "Give the token and this machine's LAN URL to the other machines, "
                            "where the agent plugin asks for them."}
    tail = ""
    try:
        tail = open(log_path, encoding="utf-8", errors="replace").read()[-800:]
    except OSError:
        pass
    return {"started": False,
            "error": "the process was launched but nothing answered on the port within 12s",
            "pid": p.pid, "log_tail": tail}


@mcp.tool()
def hub_stop() -> dict:
    """Stop the board service. Confirms by checking that the port stopped answering."""
    _, pid_path, _ = _paths()
    if not os.path.exists(pid_path):
        return {"stopped": False, "error": "no pid file; if the board is running it was not "
                                           "started by this plugin"}
    pid = int(open(pid_path, encoding="utf-8").read().strip())
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except Exception as e:
        return {"stopped": False, "error": f"{type(e).__name__}: {e}"}
    for _ in range(10):
        time.sleep(0.3)
        if not _alive(1.0):
            os.remove(pid_path)
            return {"stopped": True, "verified": "port no longer answers"}
    return {"stopped": False, "error": "signalled, but the port is still answering"}


@mcp.tool()
def hub_token() -> dict:
    """The token other machines need. Treat it as a password: it is one."""
    tok = _token()
    if not tok:
        return {"error": "no token yet — the service generates one on first start. Call hub_start."}
    return {"token": tok, "port": PORT,
            "note": "Anyone who can reach this port and holds this token can read and write the "
                    "board. Keep it off shared channels, and regenerate by deleting token.txt "
                    "in the data directory and restarting."}


# ---------------------------------------------------------------- board tools

@mcp.tool()
def board_health() -> dict:
    """Is the board up? Distinguishes an empty board from an unreachable one."""
    h = _alive()
    return h or {"error": "board not answering", "hint": "call hub_start"}


@mcp.tool()
def board_inbox(limit: int = 30) -> dict:
    """Recent messages, WITHOUT consuming them. Use this to read."""
    rows = _call("/log", {"n": max(1, min(int(limit), 200))})
    if isinstance(rows, dict):
        return rows
    mine = [r for r in rows if r.get("to") in ("all", ME) or r.get("from") == ME]
    return {"count": len(mine), "messages": mine}


@mcp.tool()
def board_drain(wait_seconds: int = 0) -> dict:
    """Take messages for this node AND MARK THEM TAKEN. Destructive; prefer board_inbox."""
    return {"messages": _call("/recv", {"me": ME, "wait": max(0, min(int(wait_seconds), 55))})}


@mcp.tool()
def board_send(to: str, body: str, kind: str = "chat", task_id: int | None = None) -> dict:
    """Post a message to a node or to "all". The sender is fixed by configuration."""
    if not body.strip():
        return {"error": "refusing to post an empty message"}
    p = {"from": ME, "to": to, "kind": kind}
    if task_id:
        p["task_id"] = int(task_id)
    return _call("/send", p, body=body, method="POST")


@mcp.tool()
def board_tasks() -> dict:
    """Open and recently closed work, plus the gate log."""
    d = _call("/tasks")
    if isinstance(d, dict) and "tasks" in d:
        t = d["tasks"]
        return {"open": [x for x in t if x.get("state") not in ("completed", "failed")],
                "recent_closed": [x for x in t if x.get("state") in ("completed", "failed")][:10],
                "gate": d.get("gate", [])[:10]}
    return d


@mcp.tool()
def board_task_create(to: str, title: str, body: str, reversible: bool = True) -> dict:
    """Create a task. `reversible` selects how the gate treats it — mark it honestly.

    Deleting data, publishing, sending mail and spending money are not reversible.
    """
    if not title.strip():
        return {"error": "a task needs a title"}
    return _call("/task", {"from": ME, "to": to, "title": title,
                           "reversible": "1" if reversible else "0"},
                 body=body, method="POST")


@mcp.tool()
def board_task_state(task_id: int, state: str, note: str = "") -> dict:
    """Move a task and attach its result. Report what HAPPENED, not what was attempted."""
    ok = ("submitted", "working", "input-required", "needs-operator", "completed", "failed")
    if state not in ok:
        return {"error": f"state must be one of {ok}"}
    return _call("/task/state", {"id": int(task_id), "state": state, "by": ME},
                 body=note, method="POST")


@mcp.tool()
def board_nodes() -> dict:
    """Who is present, and what each node declares it can and cannot do."""
    return _call("/nodes")


if __name__ == "__main__":
    mcp.run()
