import json
import subprocess
import sys
import tomllib

import pytest

from agent_board.board import Board


def run(root, *args, check=True):
    return subprocess.run(
        [sys.executable, "-m", "agent_board", *args], cwd=root,
        capture_output=True, text=True, check=check,
    )


def test_init_configures_every_client(tmp_path):
    run(tmp_path, "init")
    expected_args = ["-m", "agent_board", "serve", "--root", str(tmp_path.resolve())]

    for config in (".mcp.json", ".agents/mcp_config.json"):
        server = json.loads((tmp_path / config).read_text())["mcpServers"]["board"]
        assert server == {"command": sys.executable, "args": expected_args}

    codex = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert codex["mcp_servers"]["board"] == {"command": sys.executable, "args": expected_args}

    assert "claim_task" in (tmp_path / "AGENTS.md").read_text()
    assert "@AGENTS.md" in (tmp_path / "CLAUDE.md").read_text()
    assert "hackathon_board.db*" in (tmp_path / ".gitignore").read_text()


def test_init_preserves_existing_content_and_can_repeat(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}')
    (tmp_path / "AGENTS.md").write_text("# House rules\n")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model = "gpt"\n')

    run(tmp_path, "init")
    first = (tmp_path / "AGENTS.md").read_text()
    run(tmp_path, "init")

    assert (tmp_path / "AGENTS.md").read_text() == first
    assert first.startswith("# House rules")
    assert first.count("agent-board:start") == 1
    assert "other" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    codex = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert codex["model"] == "gpt" and "board" in codex["mcp_servers"]


def test_remote_init_keeps_the_token_out_of_committed_files(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARD_TOKEN", "s3cret")
    run(tmp_path, "init", "--url", "https://board.example.com/")
    url = "https://board.example.com/mcp"

    claude = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["board"]
    assert claude == {
        "type": "http", "url": url, "headers": {"Authorization": "Bearer ${BOARD_TOKEN}"},
    }
    codex = tomllib.loads((tmp_path / ".codex/config.toml").read_text())["mcp_servers"]["board"]
    assert codex == {"url": url, "bearer_token_env_var": "BOARD_TOKEN"}
    antigravity = json.loads((tmp_path / ".agents/mcp_config.json").read_text())
    assert antigravity["mcpServers"]["board"] == {
        "serverUrl": url, "headers": {"Authorization": "Bearer s3cret"},
    }

    ignored = (tmp_path / ".gitignore").read_text()
    assert ".agents/mcp_config.json" in ignored and ".board_token" in ignored
    assert not (tmp_path / "hackathon_board.db").exists()
    for committed in (".mcp.json", ".codex/config.toml", "AGENTS.md", "CLAUDE.md"):
        assert "s3cret" not in (tmp_path / committed).read_text()


def test_remote_init_without_a_token_skips_antigravity(tmp_path, monkeypatch):
    monkeypatch.delenv("BOARD_TOKEN", raising=False)
    output = run(tmp_path, "init", "--url", "https://board.example.com/mcp").stdout
    assert "Skipped Antigravity" in output
    assert not (tmp_path / ".agents/mcp_config.json").exists()


def test_init_refuses_to_replace_invalid_json(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    result = run(tmp_path, "init", check=False)
    assert result.returncode != 0
    assert (tmp_path / ".mcp.json").read_text() == "{not json"


def test_commands_find_the_board_from_a_subdirectory(tmp_path):
    run(tmp_path, "init")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    run(nested, "add-task", "Ship it")
    run(nested, "post", "hello team")
    output = run(nested, "status").stdout
    assert "Ship it" in output and "hello team" in output
    assert not (nested / "hackathon_board.db").exists()


def test_commands_do_not_create_a_stray_board(tmp_path):
    result = run(tmp_path, "status", check=False)
    assert result.returncode != 0
    assert not (tmp_path / "hackathon_board.db").exists()


def test_human_overrides(tmp_path):
    run(tmp_path, "init")
    board = Board(tmp_path)
    task = board.create_task("stuck")
    board.claim_task(task, "gone")
    board.acquire_file_lock("app.py", "gone")

    run(tmp_path, "release-task", str(task))
    run(tmp_path, "unlock", "./app.py")

    assert board.list_tasks("todo")[0]["id"] == task
    assert board.list_file_locks() == []
    assert run(tmp_path, "unlock", "app.py", check=False).returncode == 1


@pytest.mark.parametrize("flag_first", [True, False])
def test_root_flag_works_in_either_position(tmp_path, flag_first):
    run(tmp_path, "init")
    args = ["--root", str(tmp_path), "status"] if flag_first else ["status", "--root", str(tmp_path)]
    assert "TASKS" in run(tmp_path.parent, *args).stdout
