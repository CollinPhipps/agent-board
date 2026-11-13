import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from agent_board.board import Board


@pytest.fixture
def board(tmp_path):
    return Board(tmp_path)


def _claim(args):
    root, task_id, name = args
    return name, Board(root).claim_task(task_id, name)


def _lock(args):
    root, name = args
    return name, Board(root).acquire_file_lock("app.py", name)


def _post(args):
    root, start, count = args
    board = Board(root)
    for index in range(count):
        board.post_message(f"agent-{start}", f"message {index}")
        board.create_task(f"task {start}-{index}")


def _open(root):
    return Board(root).db_path.exists()


def test_uses_wal_mode(board):
    with sqlite3.connect(board.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_read_messages_defaults_to_the_newest(board):
    for index in range(15):
        board.post_message("a", f"m{index}")
    messages = board.read_messages()
    assert [m["content"] for m in messages] == [f"m{i}" for i in range(5, 15)]


def test_read_messages_pages_forward_without_gaps(board):
    ids = [board.post_message("a", f"m{i}") for i in range(25)]
    seen, since = [], ids[0]
    while page := board.read_messages(limit=10, since_id=since):
        seen += [m["id"] for m in page]
        since = page[-1]["id"]
    assert seen == ids[1:]


def test_read_messages_caps_limit(board):
    for index in range(120):
        board.post_message("a", "x")
    assert len(board.read_messages(limit=1000)) == 100
    assert len(board.read_messages(limit=0)) == 1


def test_read_messages_filters_for_an_agent(board):
    board.post_message("a", "everyone")
    board.post_message("a", "for b", recipient="b")
    board.post_message("a", "for c", recipient="c")
    assert [m["content"] for m in board.read_messages(agent_name="b")] == ["everyone", "for b"]
    assert len(board.read_messages()) == 3


def test_claim_is_exclusive_and_safe_to_retry(board):
    task = board.create_task("t")
    assert board.claim_task(task, "a") is True
    assert board.claim_task(task, "a") is True
    assert board.claim_task(task, "b") is False
    assert board.list_tasks("in_progress")[0]["owner"] == "a"


def test_claim_missing_task_fails(board):
    assert board.claim_task(999, "a") is False


def test_only_owner_completes(board):
    task = board.create_task("t")
    assert board.complete_task(task, "a") is False
    board.claim_task(task, "a")
    assert board.complete_task(task, "b") is False
    assert board.complete_task(task, "a") is True
    assert board.list_tasks("completed")[0]["id"] == task
    assert board.claim_task(task, "b") is False


def test_blocked_task_stays_with_owner_until_given_up(board):
    task = board.create_task("t")
    board.claim_task(task, "a")
    assert board.update_task_status(task, "b", "blocked") is False
    assert board.update_task_status(task, "a", "blocked") is True
    assert board.claim_task(task, "b") is False
    assert board.update_task_status(task, "a", "todo") is True
    assert board.claim_task(task, "b") is True


def test_status_update_cannot_reopen_or_complete(board):
    task = board.create_task("t")
    board.claim_task(task, "a")
    with pytest.raises(ValueError):
        board.update_task_status(task, "a", "completed")
    board.complete_task(task, "a")
    assert board.update_task_status(task, "a", "todo") is False


def test_list_tasks_rejects_unknown_status(board):
    with pytest.raises(ValueError):
        board.list_tasks("done")


def test_force_release_task(board):
    task = board.create_task("t")
    board.claim_task(task, "a")
    assert board.force_release_task(task) is True
    assert board.claim_task(task, "b") is True


def test_lock_is_exclusive(board):
    assert board.acquire_file_lock("app.py", "a") is True
    assert board.acquire_file_lock("app.py", "b") is False
    assert board.release_file_lock("app.py", "b") is False
    assert board.release_file_lock("app.py", "a") is True
    assert board.acquire_file_lock("app.py", "b") is True


def test_path_spellings_share_one_lock(board):
    board.acquire_file_lock("src/app.py", "a")
    for spelling in ("./src/app.py", "src//app.py", "src\\app.py", "lib/../src/app.py",
                     str(board.root / "src" / "app.py")):
        assert board.acquire_file_lock(spelling, "b") is False, spelling
    assert board.list_file_locks()[0]["filepath"] == "src/app.py"


@pytest.mark.parametrize("path", ["/etc/hosts", "C:\\repo\\app.py", "../other/app.py"])
def test_paths_outside_the_repo_are_rejected(board, path):
    with pytest.raises(ValueError, match="relative to the repository root"):
        board.acquire_file_lock(path, "a")


def test_expired_lock_can_be_taken_over(tmp_path):
    board = Board(tmp_path, lock_ttl=0.01)
    board.acquire_file_lock("app.py", "a")
    import time

    time.sleep(0.05)
    assert board.list_file_locks() == []
    assert board.acquire_file_lock("app.py", "b") is True
    assert board.release_file_lock("app.py", "a") is False


def test_reacquiring_renews_a_lock(tmp_path):
    board = Board(tmp_path, lock_ttl=1000)
    board.acquire_file_lock("app.py", "a")
    first = board.list_file_locks()[0]["locked_at"]
    assert board.acquire_file_lock("app.py", "a") is True
    assert board.list_file_locks()[0]["locked_at"] > first


def test_force_release_lock(board):
    board.acquire_file_lock("./app.py", "a")
    assert board.force_release_lock("app.py") is True
    assert board.list_file_locks() == []


@pytest.mark.parametrize("call", [
    lambda b: b.post_message(" ", "x"),
    lambda b: b.post_message("a", ""),
    lambda b: b.create_task(""),
    lambda b: b.claim_task(1, ""),
    lambda b: b.acquire_file_lock("", "a"),
])
def test_empty_values_are_rejected(board, call):
    with pytest.raises(ValueError):
        call(board)


def test_processes_race_for_one_task(tmp_path):
    board = Board(tmp_path)
    for _ in range(20):
        task = board.create_task("contested")
        with multiprocessing.Pool(8) as pool:
            results = pool.map(_claim, [(tmp_path, task, f"agent-{i}") for i in range(8)])
        assert sum(won for _, won in results) == 1


def test_processes_race_for_one_lock(tmp_path):
    board = Board(tmp_path)
    for _ in range(20):
        with multiprocessing.Pool(8) as pool:
            results = pool.map(_lock, [(tmp_path, f"agent-{i}") for i in range(8)])
        assert sum(won for _, won in results) == 1
        board.force_release_lock("app.py")


def test_concurrent_writers_lose_nothing(tmp_path):
    board = Board(tmp_path)
    with multiprocessing.Pool(8) as pool:
        pool.map(_post, [(tmp_path, i, 100) for i in range(8)])
    assert len(board.read_messages(limit=100, since_id=700)) == 100
    assert len(board.list_tasks()) == 800


def test_many_processes_can_create_the_database_together(tmp_path):
    for attempt in range(10):
        root = tmp_path / str(attempt)
        with multiprocessing.Pool(8) as pool:
            assert all(pool.map(_open, [root] * 8))
