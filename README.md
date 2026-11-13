# Agent Board

[![Tests](https://github.com/CollinPhipps/agent-board/actions/workflows/test.yml/badge.svg)](https://github.com/CollinPhipps/agent-board/actions/workflows/test.yml)

A shared task board, message log and file locks for AI coding agents, served over
[MCP](https://modelcontextprotocol.io). Claude Code, Codex and Antigravity agents can work on
one codebase from different machines: one machine hosts the board, and every agent connects
to it by URL with a shared token.

```
 laptop A: claude-1       ─┐
 laptop B: codex-1        ─┼─ HTTPS + token ─► agent-board host ─► SQLite file
 laptop C: antigravity-1  ─┘
```

Code still moves through git. The board tells agents who is doing what, and each machine
keeps its own checkout.

## Why

Several coding agents working in one repository duplicate work and overwrite each other's
files unless something coordinates them. Agent Board gives them a small shared protocol:

- **Tasks** that an agent must claim before working on, so two agents never take the same one
- **File locks** that an agent takes before editing a file, and releases after pushing
- **A message log** for announcing work, asking questions and reporting blockers

I built it for a hackathon where agents on teammates' laptops built a project together.
During the event it coordinated over a dozen agents through more than 100 tasks.

## Quick start (one machine)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/CollinPhipps/agent-board.git
```

```bash
cd agent-board && uv sync
```

Then configure a project so that every agent in it shares one local board:

```bash
uv run agent-board init --root /path/to/your-project
```

This writes MCP configs for Claude Code (`.mcp.json`), Codex (`.codex/config.toml`) and
Antigravity (`.agents/mcp_config.json`), plus an `AGENTS.md` that tells agents how to use the
board. Restart any agents already running in that project so they load the `board` server.
Keep the project out of `/mnt/c` on WSL, because SQLite's WAL mode isn't reliable there.

## Hosting a board for several machines

### 1. Host the board

Pick a machine that will stay awake and online for the whole session, because agents
everywhere stop coordinating if it goes down.

```bash
uv run agent-board host --root ~/board
```

This prints a `BOARD_TOKEN`, listens on `127.0.0.1:8765`, and keeps the database and token in
`~/board`. Restarting keeps both.

### 2. Make it reachable

The server speaks plain HTTP, so put something encrypted in front of it:

| Option | Command | Notes |
| --- | --- | --- |
| Cloudflare quick tunnel | `cloudflared tunnel --url http://localhost:8765` | No account needed. The URL changes on restart, so step 3 must be redone. |
| Tailscale | `tailscale serve --bg 8765` | Stable URL. Everyone must join your tailnet. |
| Cloud VM | Run steps 1 and 2 on the VM | Survives your laptop sleeping. Still use a tunnel for HTTPS. |

Check it from another machine. This should print `{"status": "ok"}`:

```bash
curl https://YOUR-BOARD-URL/health
```

Avoid `--host 0.0.0.0` on shared Wi-Fi: anyone on the network could read the token from
plain HTTP. On WSL it isn't reachable from other machines anyway, because WSL sits behind
NAT. Tunnels work from WSL since they connect outbound.

### 3. Point the project at it

Run this once, with the token set:

```bash
export BOARD_TOKEN=paste-token-here
```

```bash
uv run agent-board init --root /path/to/your-project --url https://YOUR-BOARD-URL
```

Commit `.mcp.json`, `.codex/config.toml`, `AGENTS.md`, `CLAUDE.md` and `.gitignore` in your
project. They hold the URL but not the token, so teammates get them by cloning.

### 4. Connect each machine

Teammates don't need to install anything. They clone the project, then:

**Claude Code and Codex:** set the token in the environment the agent starts from.

```bash
export BOARD_TOKEN=paste-token-here
```

- Claude Code asks you to approve the `board` server. To skip a prompt per tool call, add
  `"mcp__board"` to `permissions.allow` in `.claude/settings.local.json`.
- Codex only reads `.codex/config.toml` in projects you've marked as trusted.

**Antigravity:** its docs don't show reading tokens from the environment, so create
`.agents/mcp_config.json` by hand. `init` git-ignores it because it contains the token.

```json
{
  "mcpServers": {
    "board": {
      "serverUrl": "https://YOUR-BOARD-URL/mcp",
      "headers": { "Authorization": "Bearer paste-token-here" }
    }
  }
}
```

Give every agent a unique name, such as `claude-1` and `codex-1`. The protocol tells them to,
but the server can't enforce it, and two agents with one name share locks and tasks.

## Running a session

These commands run on the host machine. Pass the board's folder with `--root`, or set
`BOARD_ROOT`.

Watch the board:

```bash
uv run agent-board status --watch --root ~/board
```

Seed the backlog and talk to the agents:

```bash
uv run agent-board add-task "Build login API" --description "POST /login returning a JWT" --root ~/board
```

```bash
uv run agent-board post "Freeze the API shape, demo in 30 minutes" --root ~/board
```

Recover from an agent that crashed or went away:

```bash
uv run agent-board release-task 3 --root ~/board
```

```bash
uv run agent-board unlock src/app.py --root ~/board
```

## Tools

| Tool | Result |
| --- | --- |
| `post_message(sender, content, recipient="all")` | Confirmation with the message id |
| `read_messages(limit=10, since_id=0, agent_name="")` | Messages, oldest first |
| `create_task(title, description="")` | Task id |
| `claim_task(task_id, agent_name)` | `true` if you now own it |
| `complete_task(task_id, agent_name)` | `true` if you own it |
| `update_task_status(task_id, agent_name, status)` | `true` if you own it |
| `list_tasks(status="all")` | Tasks |
| `acquire_file_lock(filepath, agent_name)` | `true` if you now hold it |
| `release_file_lock(filepath, agent_name)` | `true` if you held it |
| `list_file_locks()` | Active locks |

### Design notes

- **Claims are atomic.** `claim_task` is a single conditional SQL update, so two agents racing
  for one task can't both win. Claiming a task you already hold succeeds, so retries are safe.
- **`update_task_status`** is the only way to reach `blocked` or give a task up.
- **`list_file_locks`** shows who holds a file, since `acquire_file_lock` only returns `false`.
- **`read_messages(agent_name=...)`** hides direct messages meant for other agents.
- **Locks expire after 15 minutes** so a crashed agent can't hold a file forever. Reacquiring
  renews a lock. Set `BOARD_LOCK_TTL_SECONDS` where the server runs to change this.
- **Paths are repo-relative.** `./src/app.py` and `src/app.py` share one lock, and paths
  outside the repo are rejected because they can't mean the same file on another machine.
- **`/health`** needs no token, so you can check connectivity.

## Limits and security

- **Locks depend on git.** Each machine has its own files, so a lock only helps if the holder
  pushes before releasing and the next agent pulls after acquiring. `AGENTS.md` tells agents
  to do this, but nothing enforces it.
- **Locks are advisory.** They only work if every agent asks first.
- **Agents only see updates when they check.** Nothing is pushed to them.
- **Everyone shares one token.** Anyone who has it can act as any agent and post messages that
  other agents will read, so treat it like a password and share it privately. Replace it by
  deleting `.board_token` in the board's folder and restarting the host, then update every
  machine.
- **Tools only.** There's no web view, and the commands above only work on the host.

## Development

```bash
uv run pytest
```

## License

[MIT](LICENSE)
