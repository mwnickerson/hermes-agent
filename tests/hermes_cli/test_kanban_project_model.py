import json
import sqlite3
import time

from hermes_cli import kanban_project_model as kpm


def make_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
            priority INTEGER DEFAULT 0, created_at INTEGER, started_at INTEGER,
            completed_at INTEGER, result TEXT, last_failure_error TEXT,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, current_run_id INTEGER
        );
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, status TEXT, started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT);
        """
    )
    return con


def test_project_metadata_from_body_and_payload():
    task = {"id": "t_root", "title": "Example Project", "body": "Project Hub slug: example-project\nDiscord thread id: 123456789\nKanban stage: Build"}
    meta = kpm.extract_task_project_metadata(task, {"metadata": {"project_status": "active"}})
    assert meta["project_hub_slug"] == "example-project"
    assert meta["discord_thread_id"] == "123456789"
    assert meta["stage_name"] == "Build"
    assert meta["project_status"] == "active"


def test_project_rows_groups_tasks_and_status():
    con = make_db()
    body = "Project Hub slug: project-one\nDiscord thread id: 111111111\n"
    now = int(time.time())
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)", ("root", "Project One", body, "antonetta", "done", 10, now - 100))
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)", ("child", "Build thing", body, "forge", "running", 0, now - 50))
    con.execute("INSERT INTO task_links(parent_id, child_id) VALUES ('root','child')")
    rows = kpm.project_rows(con)
    assert len(rows) == 1
    row = rows[0]
    assert row["project_hub_slug"] == "project-one"
    assert row["title"] == "Project One"
    assert row["project_hub_status"] == "active"
    assert row["active_agents"] == ["forge"]
    assert row["discord_thread_id"] == "111111111"


def test_archive_completed_project_tasks_silent_only_for_project_tasks():
    con = make_db()
    old = int(time.time()) - 72 * 3600
    body = "Project Hub slug: project-one\n"
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at,completed_at) VALUES (?,?,?,?,?,?,?)", ("project_done", "Done", body, "forge", "done", old, old))
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at,completed_at) VALUES (?,?,?,?,?,?,?)", ("plain_done", "Plain", "", "forge", "done", old, old))
    report = kpm.archive_completed_project_tasks(con, older_than_seconds=48 * 3600)
    assert report["archived"] == ["project_done"]
    assert con.execute("SELECT status FROM tasks WHERE id='project_done'").fetchone()[0] == "archived"
    assert con.execute("SELECT status FROM tasks WHERE id='plain_done'").fetchone()[0] == "done"
    payload = json.loads(con.execute("SELECT payload FROM task_events WHERE task_id='project_done'").fetchone()[0])
    assert payload["discord_silent"] is True


def test_dsr_project_activity_only_user_visible():
    con = make_db()
    now = int(time.time())
    body = "Project Hub slug: project-one\n"
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at,completed_at) VALUES (?,?,?,?,?,?,?)", ("visible", "Visible", body, "forge", "done", now - 10, now - 10))
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at,completed_at) VALUES (?,?,?,?,?,?,?)", ("internal", "Internal", body, "forge", "done", now - 9, now - 9))
    con.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,summary,metadata) VALUES (?,?,?,?,?,?,?,?)", ("visible", "forge", "done", now - 20, now - 10, "done", "user visible", json.dumps({"user_visible_change": True, "dsr_summary": "Visible outcome"})))
    con.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,summary,metadata) VALUES (?,?,?,?,?,?,?,?)", ("internal", "forge", "done", now - 20, now - 9, "done", "internal", json.dumps({})))
    rows = kpm.dsr_project_activity(con, now - 100, now + 100)
    assert [r["task_id"] for r in rows] == ["visible"]
    assert rows[0]["summary"] == "Visible outcome"


def test_completed_project_thread_posts_are_metadata_gated():
    assert kpm.should_post_project_thread_event(
        "completed",
        {"summary": "routine handoff", "metadata": {"project_hub_slug": "project-one"}},
    ) is False
    assert kpm.should_post_project_thread_event(
        "completed",
        {"summary": "visible", "metadata": {"project_hub_slug": "project-one", "user_visible_change": True}},
    ) is True
