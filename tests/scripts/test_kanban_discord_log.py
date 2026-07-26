import importlib.util
import builtins
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def load_watcher(block_project_model=False):
    path = Path(__file__).resolve().parents[2] / "scripts" / "kanban_discord_log.py"
    spec = importlib.util.spec_from_file_location(
        f"kanban_discord_log_under_test_{'fallback' if block_project_model else 'normal'}",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    if not block_project_model:
        spec.loader.exec_module(module)
        return module
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_cli" and "kanban_project_model" in (fromlist or ()):
            raise ImportError("blocked test import")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded_import
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = real_import
    return module


def make_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
            priority INTEGER DEFAULT 0, created_at INTEGER, started_at INTEGER,
            completed_at INTEGER, result TEXT, last_failure_error TEXT,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, current_run_id INTEGER,
            created_by TEXT
        );
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, status TEXT, started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT);
        """
    )
    return con


FORBIDDEN = (
    "stdout",
    "stderr",
    "Traceback",
    "/Users/",
    "/private/",
    "task_id",
    "run_id",
    "t_fixture",
    "assignee",
    "run-",
    "project_hub_slug",
    "{",
)


def test_preview_fixtures_cover_pm_cases_without_forbidden_output():
    watcher = load_watcher()
    names = []
    suppressed = {}
    for name, task, kind, payload, project, hub in watcher.pm_preview_fixtures():
        names.append(name)
        rendered = watcher.kpm.render_project_pm_update(task, kind, payload, project, hub)
        if rendered.should_post:
            for pattern in FORBIDDEN:
                assert pattern not in rendered.message
        else:
            suppressed[name] = rendered.suppression_reason
    assert names == [
        "kickoff",
        "research-completion",
        "implementation-awaiting-review",
        "changes-requested",
        "blocker",
        "approval-request",
        "final-completion",
        "malformed-raw-worker-payload",
        "missing-project-hub-context",
    ]
    assert suppressed == {"malformed-raw-worker-payload": "suppressed-no-safe-human-content"}


def test_preview_fixture_stdout_has_no_raw_forbidden_output(capsys):
    watcher = load_watcher()
    watcher.preview_fixtures()
    out = capsys.readouterr().out
    for pattern in FORBIDDEN:
        assert pattern not in out


def test_project_boundary_suppression_logs_reason_without_raw_payload(monkeypatch, tmp_path):
    watcher = load_watcher()
    con = make_db()
    con.execute(
        "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
        (
            "t_raw_payload",
            "Human-readable Kanban project updates",
            "Project Hub slug: human-readable-kanban-project-updates\nKanban root task id: t_raw_payload\n",
            "forge",
            "done",
            1,
            1,
            "",
        ),
    )
    ev = {
        "id": 1,
        "task_id": "t_raw_payload",
        "run_id": 99,
        "kind": "completed",
        "payload": json.dumps({"summary": '{"stdout": "/Users/anton/.hermes/secret"}'}),
        "created_at": 1,
    }
    monkeypatch.setattr(watcher, "fetch_project_hub_context", lambda slug: {})
    watcher.SUPPRESSION_LOG_PATH = tmp_path / "pm.log"
    state = {"last_event_id": 0, "components": {}, "task_aliases": {}, "project_threads": {}}
    result = watcher.route_event(con, state, ev, "general", "project", "red", dry_run=True)
    assert result == "skipped-project-noise"
    assert state["project_threads"] == {}
    log = watcher.SUPPRESSION_LOG_PATH.read_text()
    assert "suppressed-low-information-completion" in log
    assert "stdout" not in log
    assert "/Users/anton" not in log
    assert "t_raw_payload" not in log
    assert "human-readable-kanban-project-updates" not in log


def test_import_failure_fails_closed_without_thread_or_raw_post(monkeypatch, tmp_path):
    watcher = load_watcher(block_project_model=True)
    con = make_db()
    con.execute(
        "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
        (
            "t_import_fail",
            "Project with raw fallback bait",
            "Project Hub slug: raw-fallback-bait\nKanban root task id: t_import_fail\n",
            "forge",
            "done",
            1,
            1,
            "",
        ),
    )
    ev = {
        "id": 7,
        "task_id": "t_import_fail",
        "run_id": 123,
        "kind": "completed",
        "payload": json.dumps({"summary": "Traceback stdout /Users/anton/private", "metadata": {"project_completion": True}}),
        "created_at": 1,
    }
    calls = []
    monkeypatch.setattr(watcher, "create_thread", lambda *args, **kwargs: calls.append(("thread", args, kwargs)) or {"id": "thread"})
    monkeypatch.setattr(watcher, "post", lambda *args, **kwargs: calls.append(("post", args, kwargs)))
    watcher.SUPPRESSION_LOG_PATH = tmp_path / "pm.log"
    state = {"last_event_id": 0, "components": {}, "task_aliases": {}, "project_threads": {}}
    result = watcher.route_event(con, state, ev, "general", "project", "red", dry_run=True)
    assert result == "skipped-project-model-unavailable"
    assert calls == []
    assert state["project_threads"] == {}
    log = watcher.SUPPRESSION_LOG_PATH.read_text()
    assert "suppressed-project-model-unavailable" in log
    for pattern in ("Traceback", "stdout", "/Users/", "t_import_fail", "raw-fallback-bait", "run_id", "123"):
        assert pattern not in log


def test_legacy_component_project_branch_suppresses_malicious_payload(monkeypatch, tmp_path):
    watcher = load_watcher()
    con = make_db()
    con.execute(
        "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
        ("root_legacy", "Umbrella project raw bait", "orchestration project", "forge", "running", 10, 1, ""),
    )
    con.execute(
        "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
        ("child_legacy", "Worker raw bait", "", "red-team-handle", "done", 1, 2, ""),
    )
    con.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?,?)", ("root_legacy", "child_legacy"))
    ev = {
        "id": 9,
        "task_id": "child_legacy",
        "run_id": 456,
        "kind": "completed",
        "payload": json.dumps({
            "summary": "{\"stdout\":\"/Users/anton/.hermes/secret\"}",
            "assignee": "@malicious-handle",
            "metadata": {"user_visible_change": True},
        }),
        "created_at": 3,
    }
    calls = []
    monkeypatch.setattr(watcher, "create_thread", lambda *args, **kwargs: calls.append(("thread", args, kwargs)) or {"id": "thread"})
    monkeypatch.setattr(watcher, "post", lambda *args, **kwargs: calls.append(("post", args, kwargs)))
    watcher.SUPPRESSION_LOG_PATH = tmp_path / "pm.log"
    state = {"last_event_id": 0, "components": {}, "task_aliases": {}, "project_threads": {}}
    result = watcher.route_event(con, state, ev, "general", "project", "red", dry_run=True)
    assert result == "skipped-project-missing-explicit-metadata"
    assert calls == []
    assert state["components"] == {}
    log = watcher.SUPPRESSION_LOG_PATH.read_text()
    assert "suppressed-project-missing-explicit-metadata" in log
    for pattern in (
        "stdout",
        "/Users/",
        "child_legacy",
        "root_legacy",
        "@malicious-handle",
        "red-team-handle",
        "run_id",
        "456",
    ):
        assert pattern not in log


def test_deployed_private_project_model_wins_over_missing_checkout(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    scripts = tmp_path / ".hermes" / "scripts"
    package = scripts / "lib" / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    shutil.copy2(repo / "hermes_cli" / "kanban_project_model.py", package / "kanban_project_model.py")
    shutil.copy2(repo / "scripts" / "kanban_discord_log.py", scripts / "kanban_discord_log.py")
    proc = subprocess.run(
        [sys.executable, str(scripts / "kanban_discord_log.py"), "--preview-fixtures"],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Project kickoff" in proc.stdout
    assert "suppressed-project-model-unavailable" not in proc.stdout
