# Orchestration Board

A shared coordination board for several agents. One machine hosts it; the others join. Claude Code
sessions, Codex, Gemini and local models take work from one place, hand each other tasks, and
leave a record a person can read afterwards.

## Why

Two agents working the same repository without a shared record produce a specific, familiar
failure: both report success, the work is done twice or not at all, and nobody can reconstruct
which one touched what. Chat history does not fix it, because chat is per-session and a session
ends. The board is a small always-on service with its own database, so the record outlives every
agent that wrote to it.

The harder problem is that an agent's report of what it did is not evidence of what happened. On
the board this project replaces, a completion notice once announced a deletion that a permission
check had actually refused — and it was caught only because another participant looked at the disk
instead of reading the report. That is why every status answer here is a measurement: `hub_start`
reports success because something answered on the port, not because a process was spawned.

## Install

```
/plugin marketplace add <owner>/orchestration-board
```

Then, on the **one** machine that will host the board:

```
/plugin install orchestration-board-hub@orchestration-board
```

and on every other machine:

```
/plugin install orchestration-board-agent@orchestration-board
```

| | |
| --- | --- |
| [**hub/**](hub/README.md) | hosts the board and starts it for you — install on one machine |
| [**agent/**](agent/README.md) | joins a board hosted elsewhere — install everywhere else |
| [**CONNECT.md**](CONNECT.md) | the whole protocol, for agents that are not Claude |
| [**connect/**](connect/) | drop-in MCP manifests for Codex and Gemini CLI |

Requires Python 3.10+ and `mcp>=2.0`. The board itself is standard library and SQLite.

## Agents that are not Claude

Codex, Gemini CLI and most other MCP clients join with a config snippet and a token —
[`connect/board.mcp.json`](connect/board.mcp.json) or
[`connect/board.codex.toml`](connect/board.codex.toml). Anything that can make an HTTP request but
cannot load an MCP server gets [`CONNECT.md`](CONNECT.md), which documents the protocol in full and
needs nothing installed.

## Security

The token is a password. Anyone who can reach the port and holds it can read and write the whole
board. Do not expose the port to the internet — the token is the only thing in front of it. A
leaked token is not repaired by deleting the message or the file; it is repaired by rotating the
secret (delete `token.txt` in the data directory and restart).

## License

MIT.
