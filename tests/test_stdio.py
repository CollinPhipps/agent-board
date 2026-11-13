"""Drives real server processes the way clients do, including raw JSON-RPC."""

import asyncio
import json
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

SERVER = [sys.executable, "-m", "agent_board", "serve"]


def session(root, cwd):
    return stdio_client(
        StdioServerParameters(
            command=SERVER[0], args=[*SERVER[1:], "--root", str(root)], cwd=str(cwd)
        )
    )


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content["result"]


def test_two_clients_in_different_directories_share_a_board(tmp_path):
    root, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()

    async def scenario():
        async with session(root, root) as (read_a, write_a), session(root, elsewhere) as (read_b, write_b):
            async with ClientSession(read_a, write_a) as a, ClientSession(read_b, write_b) as b:
                await a.initialize()
                await b.initialize()

                names = {tool.name for tool in (await a.list_tools()).tools}
                assert {"post_message", "read_messages", "create_task", "claim_task",
                        "complete_task", "list_tasks", "acquire_file_lock",
                        "release_file_lock"} <= names

                task = await call(a, "create_task", title="Build API")
                assert await call(b, "claim_task", task_id=task, agent_name="codex-1") is True
                assert await call(a, "claim_task", task_id=task, agent_name="claude-1") is False
                assert await call(a, "acquire_file_lock", filepath="app.py", agent_name="claude-1")
                assert not await call(b, "acquire_file_lock", filepath="./app.py", agent_name="codex-1")
                locks = await call(b, "list_file_locks")
                assert locks[0]["locked_by"] == "claude-1"
                await call(a, "post_message", sender="claude-1", content="hello")
                messages = await call(b, "read_messages")
                assert messages[0]["content"] == "hello"

                bad = await a.call_tool("list_tasks", {"status": "nonsense"})
                assert bad.is_error

    asyncio.run(scenario())
    assert not (elsewhere / "hackathon_board.db").exists()


def test_plain_json_rpc_handshake_on_an_older_protocol(tmp_path):
    """Other clients do not use this SDK, so check the wire format directly."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "raw", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "create_task", "arguments": {"title": "wire"}}},
    ]
    process = subprocess.Popen(
        [*SERVER, "--root", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    replies = {}
    with process:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            if "id" in request:
                reply = json.loads(process.stdout.readline())
                replies[reply["id"]] = reply
        process.stdin.close()
    assert replies[1]["result"]["protocolVersion"] == "2025-03-26"
    assert "claim_task" in replies[1]["result"]["instructions"]
    assert len(replies[2]["result"]["tools"]) == 10
    assert replies[3]["result"]["isError"] is False
    assert json.loads(replies[3]["result"]["content"][0]["text"]) == 1
