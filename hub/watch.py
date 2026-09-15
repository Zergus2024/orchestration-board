#!/usr/bin/env python3
"""watch — turn board activity into wake-ups. Every line printed here is one notification.

Point a monitor at this script and the agent stops needing to remember to look. Without it,
coordination degrades in a way that is invisible from both ends: on the board this replaces, a
listener died with a machine reboot and messages addressed to that node sat unread for hours
while both sides assumed the channel was fine.

FOUR DECISIONS, EACH PAID FOR ONCE.

1. It subscribes to /events, never to /recv. /recv consumes — using it merely to stay awake
   would take messages away from whatever component was supposed to act on them. Reading and
   consuming are different verbs and this is the reading one.

2. It ignores messages this node sent. A watcher that reports your own actions back to you
   trains you to stop reading it, which is worse than no watcher at all.

3. It reports the board being unreachable, loudly, on the first failure and then rarely.
   Silence is not success: a dead board and a quiet board look identical, and only one of them
   needs attention.

4. It prints a compact line per event, not the message body in full. Every line is a
   notification, so a chatty watcher gets muted by its reader — the same failure as (2),
   arrived at from the other direction.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

def env(key, default=""):
    """Absent and empty mean the same thing: an unfilled setting arrives as "", not as unset."""
    return os.environ.get(key, "").strip() or default


BOARD = env("BOARD_URL", "http://127.0.0.1:8781").rstrip("/")
TOKEN = env("BOARD_TOKEN")
ME = env("BOARD_NODE")

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def short(s, n=140):
    s = " ".join((s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def stream():
    url = f"{BOARD}/events?" + urllib.parse.urlencode({"t": TOKEN})
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=90) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            frm = d.get("from") or "?"
            to = d.get("to") or "?"
            if ME and frm == ME:
                continue                       # our own echo is not news
            if to not in ("all", ME) and ME:
                continue                       # somebody else's conversation
            print(f"BOARD [{d.get('kind', 'chat')}] {frm} -> {to}: {short(d.get('body'))}")


fails = 0
while True:
    try:
        stream()
        print("BOARD: event stream closed by the server, reconnecting")
        fails = 0
    except urllib.error.HTTPError as e:
        fails += 1
        if fails in (1, 5, 20):
            print(f"BOARD REFUSED the event stream: HTTP {e.code} "
                  f"({'check BOARD_TOKEN' if e.code == 401 else 'unexpected'})")
    except Exception as e:
        fails += 1
        if fails in (1, 5, 20):
            print(f"BOARD UNREACHABLE at {BOARD} ({fails} failures in a row): "
                  f"{type(e).__name__}: {e}")
    time.sleep(min(5 * fails, 60) if fails else 2)
