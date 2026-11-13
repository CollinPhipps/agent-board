"""Runs a real host process and connects to it the way remote clients do."""

import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TOKEN = "test-token"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_host(root, port, token=TOKEN):
    env = {k: v for k, v in os.environ.items() if k != "BOARD_TOKEN"}
    if token:
        env["BOARD_TOKEN"] = token
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_board", "host", "--root", str(root), "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return process
        except OSError:
            time.sleep(0.05)
    process.kill()
    raise RuntimeError(process.stdout.read())


@pytest.fixture
def host(tmp_path):
    port = free_port()
    process = start_host(tmp_path, port)
    yield f"http://127.0.0.1:{port}"
    process.terminate()
    process.wait()


def post(url, payload, headers=None):
    request = urllib.request.Request(
        f"{url}/mcp", json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content["result"]


async def agent(url, name):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as client:
                await client.initialize()
                await call(client, "post_message", sender=name, content="online")
                won = []
                for task in await call(client, "list_tasks", status="todo"):
                    if await call(client, "claim_task", task_id=task["id"], agent_name=name):
                        await call(client, "acquire_file_lock", filepath="app.py", agent_name=name)
                        await call(client, "complete_task", task_id=task["id"], agent_name=name)
                        await call(client, "release_file_lock", filepath="app.py", agent_name=name)
                        won.append(task["id"])
                return won


def test_requests_need_the_right_token(host):
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "raw", "version": "0"}}}
    assert post(host, initialize)[0] == 401
    assert post(host, initialize, {"Authorization": "Bearer wrong"})[0] == 401
    status, reply = post(host, initialize, {"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    assert "claim_task" in reply["result"]["instructions"]


def test_tunnel_hostnames_are_accepted(host):
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "raw", "version": "0"}}}
    headers = {"Authorization": f"Bearer {TOKEN}", "Host": "board.trycloudflare.com",
               "Origin": "https://board.trycloudflare.com"}
    assert post(host, request, headers)[0] == 200


def test_remote_agents_divide_the_work(host, tmp_path):
    from agent_board.board import Board

    board = Board(tmp_path)
    tasks = [board.create_task(f"task {i}") for i in range(30)]

    async def scenario():
        return await asyncio.gather(*(agent(host, f"agent-{i}") for i in range(4)))

    won = [task for result in asyncio.run(scenario()) for task in result]
    assert sorted(won) == tasks
    assert board.list_tasks("completed") and not board.list_tasks("todo")
    assert len(board.read_messages(limit=100)) == 4


def test_board_and_token_survive_a_host_restart(tmp_path):
    port = free_port()
    process = start_host(tmp_path, port, token=None)
    token = (tmp_path / ".board_token").read_text().strip()
    assert stat.S_IMODE((tmp_path / ".board_token").stat().st_mode) == 0o600
    url = f"http://127.0.0.1:{port}"
    create = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "create_task", "arguments": {"title": "kept"}}}
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "list_tasks", "arguments": {}}}
    headers = {"Authorization": f"Bearer {token}"}
    assert post(url, create, headers)[0] == 200
    process.terminate()
    process.wait()

    process = start_host(tmp_path, port, token=None)
    try:
        status, reply = post(url, listing, headers)
        assert status == 200
        assert reply["result"]["structuredContent"]["result"][0]["title"] == "kept"
    finally:
        process.terminate()
        process.wait()
