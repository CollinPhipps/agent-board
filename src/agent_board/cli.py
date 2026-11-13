"""`agent-board` command: run the server, set up a project, and let humans intervene."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from agent_board.board import DB_NAME, DEFAULT_LOCK_TTL, Board
from agent_board.protocol import PROTOCOL

TOKEN_FILE = ".board_token"
TOKEN_ENV = "BOARD_TOKEN"


def find_root(explicit: str | None) -> Path:
    """Use --root, then BOARD_ROOT, then the nearest parent that already has a board."""
    chosen = explicit or os.environ.get("BOARD_ROOT")
    if chosen:
        return Path(chosen).expanduser().resolve()
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        if (directory / DB_NAME).exists():
            return directory
    return cwd


def open_board(args: argparse.Namespace, must_exist: bool = True) -> Board:
    root = find_root(args.root)
    if must_exist and not (root / DB_NAME).exists():
        sys.exit(f"No board at {root / DB_NAME}. Run `agent-board init` in the project first.")
    ttl = float(os.environ.get("BOARD_LOCK_TTL_SECONDS", DEFAULT_LOCK_TTL))
    return Board(root, lock_ttl=ttl)


def upsert_block(path: Path, start: str, end: str, body: str) -> None:
    """Insert or replace a delimited block, leaving the rest of the file alone."""
    block = f"{start}\n{body.rstrip()}\n{end}\n"
    text = path.read_text() if path.exists() else ""
    pattern = re.escape(start) + r".*?" + re.escape(end) + r"\n?"
    if re.search(pattern, text, flags=re.DOTALL):
        text = re.sub(pattern, lambda _: block, text, flags=re.DOTALL)
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def read_token(root: Path) -> str | None:
    if token := os.environ.get(TOKEN_ENV, "").strip():
        return token
    path = root / TOKEN_FILE
    return path.read_text().strip() if path.exists() else None


def host_token(root: Path) -> str:
    """Reuse the existing token, or create one only the host's user can read."""
    if token := read_token(root):
        return token
    token = secrets.token_urlsafe(32)
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(token + "\n")
    return token


def upsert_json_server(path: Path, server: dict) -> None:
    config = {}
    if path.exists() and path.read_text().strip():
        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            sys.exit(f"Not overwriting {path}: invalid JSON ({error})")
    config.setdefault("mcpServers", {})["board"] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def write_shared_files(root: Path, ignored: list[str]) -> None:
    upsert_block(
        root / "AGENTS.md", "<!-- agent-board:start -->", "<!-- agent-board:end -->",
        f"## Multi-agent coordination\n\n{PROTOCOL}",
    )
    upsert_block(
        root / "CLAUDE.md", "<!-- agent-board:start -->", "<!-- agent-board:end -->", "@AGENTS.md"
    )
    upsert_block(
        root / ".gitignore", "# >>> agent-board", "# <<< agent-board", "\n".join(ignored)
    )


def upsert_codex_server(root: Path, body: str) -> None:
    upsert_block(
        root / ".codex" / "config.toml", "# >>> agent-board", "# <<< agent-board", body
    )


def init_remote(root: Path, url: str) -> None:
    """Configure clients for a hosted board. Claude and Codex read the token from the
    environment, so their configs can be committed. Antigravity needs it written out."""
    url = url.rstrip("/")
    url = url if url.endswith("/mcp") else f"{url}/mcp"
    root.mkdir(parents=True, exist_ok=True)

    upsert_json_server(
        root / ".mcp.json",
        {"type": "http", "url": url, "headers": {"Authorization": f"Bearer ${{{TOKEN_ENV}}}"}},
    )
    upsert_codex_server(
        root,
        f"[mcp_servers.board]\nurl = {json.dumps(url)}\n"
        f"bearer_token_env_var = {json.dumps(TOKEN_ENV)}",
    )
    write_shared_files(root, [DB_NAME + "*", TOKEN_FILE, ".agents/mcp_config.json"])

    print(f"Configured Claude Code and Codex for {url}.")
    if token := read_token(root):
        upsert_json_server(
            root / ".agents" / "mcp_config.json",
            {"serverUrl": url, "headers": {"Authorization": f"Bearer {token}"}},
        )
        print("Configured Antigravity. Its config holds the token and is git-ignored.")
    else:
        print(f"Skipped Antigravity: set {TOKEN_ENV} and rerun to write its config.")
    print(f"Each machine needs {TOKEN_ENV} set in the environment agents start from.")


def cmd_init(args: argparse.Namespace) -> None:
    if args.url:
        init_remote(find_root(args.root), args.url)
        return

    board = open_board(args, must_exist=False)
    root = board.root
    # An absolute interpreter and --root mean every client reaches the same database,
    # whatever PATH or working directory it launches the server with.
    command = [sys.executable, "-m", "agent_board", "serve", "--root", str(root)]
    server = {"command": command[0], "args": command[1:]}

    upsert_json_server(root / ".mcp.json", server)  # Claude Code
    upsert_json_server(root / ".agents" / "mcp_config.json", server)  # Antigravity
    upsert_codex_server(
        root,
        f"[mcp_servers.board]\ncommand = {json.dumps(command[0])}\n"
        f"args = {json.dumps(command[1:])}",
    )
    write_shared_files(root, [DB_NAME + "*"])

    print(f"Board ready at {board.db_path}")
    print("Configured Claude Code, Codex and Antigravity. Restart any agents already running.")


def render_status(board: Board) -> str:
    lines = [f"Board: {board.db_path}", "", "TASKS"]
    for task in board.list_tasks():
        owner = task["owner"] or "-"
        lines.append(f"  #{task['id']:<3} {task['status']:<11} {owner:<16} {task['title']}")
    lines += ["", "FILE LOCKS"]
    for lock in board.list_file_locks():
        lines.append(f"  {lock['filepath']:<40} {lock['locked_by']:<16} {lock['locked_at'][11:19]}")
    lines += ["", "RECENT MESSAGES"]
    for message in board.read_messages(limit=15):
        target = "" if message["recipient"] == "all" else f" -> {message['recipient']}"
        time_part = message["timestamp"][11:19]
        lines.append(f"  [{message['id']}] {time_part} {message['sender']}{target}: {message['content']}")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> None:
    board = open_board(args)
    if not args.watch:
        print(render_status(board))
        return
    try:
        while True:
            print("\033[2J\033[H" + render_status(board), flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def cmd_serve(args: argparse.Namespace) -> None:
    from agent_board.server import create_server  # slow import, only needed here

    create_server(open_board(args, must_exist=False)).run("stdio")


def cmd_host(args: argparse.Namespace) -> None:
    import uvicorn

    from agent_board.server import create_http_app  # slow import, only needed here

    board = open_board(args, must_exist=False)
    token = host_token(board.root)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "Warning: plain HTTP exposes the token on this network. "
            "Prefer a tunnel or VPN.", file=sys.stderr,
        )
    print(f"Board: {board.db_path}")
    print(f"Listening on http://{args.host}:{args.port}/mcp")
    print(f"{TOKEN_ENV}={token}", flush=True)
    uvicorn.run(
        create_http_app(board, token), host=args.host, port=args.port, log_level="warning"
    )


def cmd_post(args: argparse.Namespace) -> None:
    board = open_board(args)
    print(f"Posted message {board.post_message(args.sender, args.content, args.to)}")


def cmd_add_task(args: argparse.Namespace) -> None:
    print(f"Created task {open_board(args).create_task(args.title, args.description)}")


def report(changed: bool, success: str, failure: str) -> None:
    print(success if changed else failure)
    if not changed:
        sys.exit(1)


def cmd_release_task(args: argparse.Namespace) -> None:
    released = open_board(args).force_release_task(args.task_id)
    report(released, f"Task {args.task_id} is back to todo", f"No task {args.task_id}")


def cmd_unlock(args: argparse.Namespace) -> None:
    released = open_board(args).force_release_lock(args.filepath)
    report(released, f"Unlocked {args.filepath}", f"{args.filepath} is not locked")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-board", description=__doc__)
    parser.add_argument("--root", help="project directory holding the board")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--root", default=argparse.SUPPRESS, help="project directory")
        sub.set_defaults(handler=handler)
        return sub

    add("serve", cmd_serve, "run the MCP server over stdio (clients do this)")
    host = add("host", cmd_host, "run the shared board over HTTP for other machines")
    host.add_argument("--host", default="127.0.0.1", help="address to bind")
    host.add_argument("--port", type=int, default=8765)
    add("init", cmd_init, "configure all three clients").add_argument(
        "--url", help="URL of a hosted board (omit for a single-machine board)"
    )
    add("status", cmd_status, "show tasks, locks and messages").add_argument(
        "--watch", action="store_true", help="refresh every 2 seconds"
    )
    post = add("post", cmd_post, "post a message as a human")
    post.add_argument("content")
    post.add_argument("--sender", default="human")
    post.add_argument("--to", default="all")
    add_task = add("add-task", cmd_add_task, "add a task to the backlog")
    add_task.add_argument("title")
    add_task.add_argument("--description", default="")
    add("release-task", cmd_release_task, "force a stuck task back to todo").add_argument(
        "task_id", type=int
    )
    add("unlock", cmd_unlock, "force-release a file lock").add_argument("filepath")

    args = parser.parse_args()
    args.handler(args)
