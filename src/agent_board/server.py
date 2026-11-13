"""MCP tools, served over HTTP to many machines or over stdio to one."""

from __future__ import annotations

import hmac
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from agent_board.board import Board
from agent_board.protocol import PROTOCOL


def create_server(board: Board) -> MCPServer:
    server = MCPServer("board", instructions=PROTOCOL)

    @server.tool()
    def post_message(sender: str, content: str, recipient: str = "all") -> str:
        """Post an update, question or alert to the shared log.

        Set recipient to an agent's name to address it, or leave "all" to broadcast.
        Direct messages are still visible to everyone.
        """
        return f"Posted message {board.post_message(sender, content, recipient)}"

    @server.tool()
    def read_messages(limit: int = 10, since_id: int = 0, agent_name: str = "") -> list[dict]:
        """Read the shared log, oldest first (limit is capped at 100).

        since_id=0 returns the newest messages. since_id=N returns messages after
        id N; repeat with the highest id received to read everything without gaps.
        Set agent_name to skip direct messages addressed to other agents.
        """
        return board.read_messages(limit, since_id, agent_name)

    @server.tool()
    def create_task(title: str, description: str = "") -> int:
        """Add a task to the backlog and return its id."""
        return board.create_task(title, description)

    @server.tool()
    def claim_task(task_id: int, agent_name: str) -> bool:
        """Atomically claim a todo task. False means it is unavailable: do not work on it."""
        return board.claim_task(task_id, agent_name)

    @server.tool()
    def complete_task(task_id: int, agent_name: str) -> bool:
        """Mark a task you own as completed. False means you do not own it."""
        return board.complete_task(task_id, agent_name)

    @server.tool()
    def update_task_status(task_id: int, agent_name: str, status: str) -> bool:
        """Change a task you own: "blocked", "in_progress", or "todo" to give it up.

        False means you do not own it, or it is already completed.
        """
        return board.update_task_status(task_id, agent_name, status)

    @server.tool()
    def list_tasks(status: str = "all") -> list[dict]:
        """List tasks: "all", "todo", "in_progress", "blocked" or "completed"."""
        return board.list_tasks(status)

    @server.tool()
    def acquire_file_lock(filepath: str, agent_name: str) -> bool:
        """Lock a file before editing it, using a path relative to the repository root.

        False means another agent holds it: do not edit it. Call again on a file
        you hold to renew the lock. Unrenewed locks expire. Locks are advisory, and
        only protect files when every agent uses them.
        """
        return board.acquire_file_lock(filepath, agent_name)

    @server.tool()
    def release_file_lock(filepath: str, agent_name: str) -> bool:
        """Release a lock you hold. False means you do not hold it (it may have expired)."""
        return board.release_file_lock(filepath, agent_name)

    @server.tool()
    def list_file_locks() -> list[dict]:
        """List active file locks and who holds them."""
        return board.list_file_locks()

    return server


class BearerAuth:
    """Require the shared token on everything except the /health connectivity check."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/health":
            await self._respond(send, 200, {"status": "ok"})
            return
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if hmac.compare_digest(supplied, self.expected):
            await self.app(scope, receive, send)
        else:
            await self._respond(send, 401, {"error": "missing or invalid bearer token"})

    @staticmethod
    async def _respond(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        headers = [(b"content-type", b"application/json")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def create_http_app(board: Board, token: str):
    # Stateless JSON responses keep working through tunnels and across host restarts.
    # Host checks are off because tunnels use arbitrary hostnames; the token is the guard.
    app = create_server(board).streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuth(app, token)
