#!/usr/bin/env python3
"""agentboard — an MCP server that puts a shared coordination board in front of the model.

WHAT THIS IS FOR. Several agents — Claude Code sessions on different machines, Codex, local
models behind ollama — need one place to talk, hand each other work, and leave a record. This
exposes that place as tools, so an agent joins by installing a plugin instead of by someone
writing bespoke scripts on every node.

THREE DESIGN DECISIONS THAT ARE NOT COSMETIC.

1. READING DOES NOT CONSUME. `board_inbox` looks at messages without moving the cursor;
   `board_drain` is the one that consumes, and it says so in its name and description. The
   distinction exists because it was already paid for: a listener that quietly drained the queue
   made another node's messages vanish, and "never arrived" was indistinguishable from
   "arrived and was ignored" for hours.

2. NO SECRET IN THE REPOSITORY. Host and token come from the environment. A token committed to a
   public repo is not a mistake you fix by deleting the file — it stays in the history, and the
   only remedy is rotation.

3. EVERY WRITE NAMES ITS AUTHOR. The board records who did what; an agent cannot post
   anonymously or as somebody else, because `me` is fixed by configuration, not by an argument
   the model can choose.
"""
import json
import os
import urllib.parse
import urllib.request

# MCP SDK 2.x renamed FastMCP to MCPServer. Importing the new name directly rather than
# try/except over both: a shim that silently accepts either version hides which one is actually
# running, and the two have different behaviour. Better to fail loudly on the wrong SDK.
from mcp.server.mcpserver import MCPServer


def env(key, default=""):
    """Absent and empty mean the same thing here.

    A plugin setting the operator never filled in arrives as an empty string, not as an unset
    variable, so os.environ.get(key, default) hands back "" and the default never applies.
    """
    return os.environ.get(key, "").strip() or default


HUB = env("BOARD_URL", "http://127.0.0.1:8781").rstrip("/")
TOKEN = env("BOARD_TOKEN")
ME = env("BOARD_NODE")

mcp = MCPServer(
    name="agentboard",
    instructions="A shared board for several agents working together. Read with board_inbox; "
                 "board_drain consumes and should be used only by the component that must act "
                 "on each message exactly once. Mark tasks irreversible honestly — that flag "
                 "decides whether the gate may hold the work.",
)


def _call(path, params=None, body=None, method="GET"):
    q = dict(params or {})
    if TOKEN:
        q["t"] = TOKEN
    url = f"{HUB}{path}?{urllib.parse.urlencode(q)}" if q else f"{HUB}{path}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        # Bodies go as BYTES with the charset named. Sending text and letting the client pick an
        # encoding turned Cyrillic into question marks on this very board, repeatedly, and it
        # looked like a server bug every time.
        req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"error": f"board returned HTTP {e.code}", "detail": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        # A dead board must not look like an empty board. Say which it is.
        return {"error": f"board unreachable at {HUB}: {type(e).__name__}: {e}"}
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:2000]}


def _need_node():
    if not ME:
        return {"error": "BOARD_NODE is not set — this agent has no name on the board, "
                         "so it cannot write. Set it in the plugin configuration."}
    return None


@mcp.tool()
def board_health() -> dict:
    """Is the board up? Returns its port and clock, or an explicit unreachable error.

    Worth calling first when anything else looks empty: an empty answer from a live board and
    silence from a dead one are different problems.
    """
    return _call("/health")


@mcp.tool()
def board_inbox(limit: int = 30) -> dict:
    """Recent messages on the board, WITHOUT consuming them.

    Use this to read. It does not move any cursor, so other listeners on the same node still
    receive what they were going to receive.
    """
    rows = _call("/log", {"n": max(1, min(int(limit), 200))})
    if isinstance(rows, dict):
        return rows
    mine = [r for r in rows if r.get("to") in ("all", ME) or r.get("from") == ME]
    return {"count": len(mine), "messages": mine}


@mcp.tool()
def board_drain(wait_seconds: int = 0) -> dict:
    """Take messages addressed to this node AND MARK THEM AS TAKEN.

    Destructive by design: anything returned here will not be returned again, to this node or to
    any other listener using the same name. Prefer board_inbox unless you are the component
    responsible for acting on each message exactly once.
    """
    err = _need_node()
    if err:
        return err
    return {"messages": _call("/recv", {"me": ME, "wait": max(0, min(int(wait_seconds), 55))})}


@mcp.tool()
def board_send(to: str, body: str, kind: str = "chat", task_id: int | None = None) -> dict:
    """Post a message to another node, or to "all".

    `kind` is one of chat, task, result, status, gate. The sender is fixed by configuration and
    cannot be chosen here.
    """
    err = _need_node()
    if err:
        return err
    if not body.strip():
        return {"error": "refusing to post an empty message"}
    p = {"from": ME, "to": to, "type": kind}
    if task_id:
        p["task_id"] = int(task_id)
    return _call("/send", p, body=body, method="POST")


@mcp.tool()
def board_tasks() -> dict:
    """The task board: open and closed work, with state, owner and result."""
    d = _call("/tasks")
    if isinstance(d, dict) and "tasks" in d:
        t = d["tasks"]
        return {"open": [x for x in t if x.get("state") not in ("completed", "failed")],
                "recent_closed": [x for x in t if x.get("state") in ("completed", "failed")][:10]}
    return d


@mcp.tool()
def board_task_create(to: str, title: str, body: str, reversible: bool = True) -> dict:
    """Create a task for another node.

    `reversible` is not paperwork: it selects how the gate treats the work. Reversible work is
    only observed, so a false alarm can never stall the loop; irreversible work can be held.
    Mark it honestly — deleting data, publishing, sending mail and spending money are not
    reversible.
    """
    err = _need_node()
    if err:
        return err
    if not title.strip():
        return {"error": "a task needs a title"}
    return _call("/task", {"from": ME, "to": to, "title": title,
                           "reversible": "1" if reversible else "0"},
                 body=body, method="POST")


@mcp.tool()
def board_task_state(task_id: int, state: str, note: str = "") -> dict:
    """Move a task to a new state and attach the result.

    States: submitted, working, input-required, needs-operator, completed, failed.
    Report what HAPPENED, not what was attempted: on this board a completion notice once claimed
    a deletion that a permission check had actually refused, and only someone looking at the disk
    caught it.
    """
    err = _need_node()
    if err:
        return err
    ok = ("submitted", "working", "input-required", "needs-operator", "completed", "failed")
    if state not in ok:
        return {"error": f"state must be one of {ok}"}
    return _call("/task/state", {"id": int(task_id), "state": state, "by": ME},
                 body=note, method="POST")


@mcp.tool()
def board_nodes() -> dict:
    """Who is on the board, and what each node says it can and cannot do.

    A "cannot" entry carries the date it was last checked, because an unverified limitation is
    an excuse rather than a fact.
    """
    return _call("/nodes")


if __name__ == "__main__":
    mcp.run()
