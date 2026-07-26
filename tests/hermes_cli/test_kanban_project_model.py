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


def test_dsr_project_activity_includes_visible_project_events():
    con = make_db()
    now = int(time.time())
    body = "Project Hub slug: build-implementation-lane\nKanban root task id: root\nKanban stage: build-lane-root\n"
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at) VALUES (?,?,?,?,?,?)", ("root", "Build Lane run", body, "antonetta", "done", now - 10))
    con.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
        ("root", "project_stage_started", json.dumps({
            "project_hub_slug": "build-implementation-lane",
            "project_title": "Run 3",
            "kanban_root_task_id": "root",
            "stage": "build-lane-root",
            "run_key": "run-3-forum-dsr-visibility",
            "summary": "Build Lane Kanban graph initialized.",
            "dsr_visible": True,
        }), now - 5),
    )
    rows = kpm.dsr_project_activity(con, now - 100, now + 100)
    assert rows[0]["task_id"] == "root"
    assert rows[0]["project_hub_slug"] == "build-implementation-lane"
    assert rows[0]["summary"] == "Build Lane Kanban graph initialized."
    assert rows[0]["metadata"]["run_key"] == "run-3-forum-dsr-visibility"
    assert rows[0]["event_kind"] == "project_stage_started"


def test_project_thread_key_and_starter_are_run_keyed():
    project = {
        "project_hub_slug": "build-implementation-lane",
        "project_title": "Run 3",
        "kanban_root_task_id": "t_root",
        "stage_name": "build-lane-root",
        "run_key": "run-3-forum-dsr-visibility",
        "task_ids": ["t_root", "t_child"],
        "dsr_visible": True,
        "purpose": "Coordinate implementation work into a readable project update.",
        "project_status": "active",
        "next_step": "Review the first PM-visible checkpoint.",
    }
    assert kpm.project_thread_key(project) == "build-implementation-lane:run-3-forum-dsr-visibility"
    starter = kpm.format_project_thread_starter(project)
    assert "Project started: Run 3" in starter
    assert "Coordinate implementation work" in starter
    assert "Review the first PM-visible checkpoint." in starter
    for forbidden in (
        "build-implementation-lane",
        "Project Hub slug",
        "t_root",
        "build-lane-root",
        "run-3-forum-dsr-visibility",
        "Tracked tasks",
        "DSR",
        "task_ids",
        "run_key",
        "kanban_root_task_id",
        "{",
    ):
        assert forbidden not in starter


def test_dsr_project_activity_includes_completed_metadata():
    con = make_db()
    now = int(time.time())
    body = "Project Hub slug: build-implementation-lane\nKanban root task id: root\n"
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at,completed_at,result) VALUES (?,?,?,?,?,?,?,?)", ("worker", "Build worker", body, "forge", "done", now - 20, now - 5, "worker result"))
    con.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
        ("worker", "completed", json.dumps({
            "summary": "worker completed",
            "metadata": {
                "project_hub_slug": "build-implementation-lane",
                "run_key": "run-4",
                "dsr_visible": True,
                "dsr_summary": "Visible worker completion",
                "changed_files": ["hermes_cli/kanban_project_model.py"],
            },
        }), now - 4),
    )
    rows = kpm.dsr_project_activity(con, now - 100, now + 100)
    assert rows[0]["event_kind"] == "completed"
    assert rows[0]["summary"] == "Visible worker completion"
    assert rows[0]["metadata"]["changed_files"] == ["hermes_cli/kanban_project_model.py"]
    assert rows[0]["metadata"]["run_key"] == "run-4"


def test_dsr_project_activity_includes_relevant_comment_body():
    con = make_db()
    now = int(time.time())
    body = "Project Hub slug: build-implementation-lane\nKanban root task id: root\n"
    con.execute("INSERT INTO tasks(id,title,body,assignee,status,created_at) VALUES (?,?,?,?,?,?)", ("review", "Review gate", body, "reviewer", "blocked", now - 20))
    con.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
        ("review", "commented", json.dumps({
            "body": "review-required: watcher smoke-test passes; please verify DSR visibility",
            "metadata": {
                "project_hub_slug": "build-implementation-lane",
                "run_key": "run-4",
            },
        }), now - 4),
    )
    rows = kpm.dsr_project_activity(con, now - 100, now + 100)
    assert rows[0]["event_kind"] == "commented"
    assert rows[0]["summary"] == "review-required: watcher smoke-test passes; please verify DSR visibility"
    assert rows[0]["metadata"]["body"] == "review-required: watcher smoke-test passes; please verify DSR visibility"


def test_completed_project_thread_posts_are_metadata_gated():
    assert kpm.should_post_project_thread_event(
        "completed",
        {"summary": "routine handoff", "metadata": {"project_hub_slug": "project-one"}},
    ) is False
    assert kpm.should_post_project_thread_event(
        "completed",
        {"summary": "visible", "metadata": {"project_hub_slug": "project-one", "user_visible_change": True}},
    ) is True


FORBIDDEN_PM_PATTERNS = (
    "{",
    "stdout",
    "stderr",
    "Traceback",
    "/Users/",
    "task_id",
    "run_id",
    "t_abc123",
    "assignee",
)


def _pm_base():
    task = {"id": "t_abc123", "title": "Research implementation path", "status": "done", "assignee": "forge"}
    project = {
        "project_hub_slug": "human-readable-kanban-project-updates",
        "project_title": "Human-readable Kanban project updates",
        "description": "Make Kanban updates readable as project management status.",
        "project_status": "active",
        "stage_name": "Research",
        "phase": "Build",
        "next_step": "Review the handoff and move to implementation.",
    }
    hub = {
        "title": "Human-readable Kanban project updates",
        "description": "Make Kanban updates readable as project management status.",
        "phase": "Build",
        "status": "active",
        "next_step": "Review the handoff and move to implementation.",
    }
    return task, project, hub


def _assert_pm_contract(message):
    assert message.startswith("**")
    assert "**Where we are:**" in message
    assert "**How this work fits:**" in message
    assert "**What changed:**" in message
    assert "**Why it was done/why it matters:**" in message
    assert "**Current state:**" in message
    assert "**Action needed:**" in message
    for pattern in FORBIDDEN_PM_PATTERNS:
        assert pattern not in message


def test_pm_render_kickoff():
    task, project, hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "project_kickoff",
        {"summary": "internal handoff", "metadata": {"public_summary": "Project run opened.", "task_role": "Coordinates presentation work."}},
        project,
        hub,
    )
    assert rendered.should_post is True
    _assert_pm_contract(rendered.message)
    assert "Project kickoff" in rendered.message
    assert "No action needed." in rendered.message


def test_pm_render_research_completion():
    task, project, hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "completed",
        {"summary": "internal handoff", "metadata": {"public_summary": "Mapped the route and identified the project boundary.", "user_visible_change": True, "task_role": "Research stage", "why_it_matters": "It prevents raw worker events from becoming project status."}},
        project,
        hub,
    )
    assert rendered.should_post is True
    _assert_pm_contract(rendered.message)
    assert "whole project is still governed" in rendered.message


def test_pm_render_implementation_completion_awaiting_review_not_project_done():
    task, project, hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "completed",
        {"summary": "internal handoff", "metadata": {"public_summary": "Implemented the renderer.", "user_visible_change": True, "review_required": True, "review_request": "Review the output before deployment.", "task_role": "Implementation stage"}},
        project,
        hub,
    )
    assert rendered.should_post is True
    _assert_pm_contract(rendered.message)
    assert "Awaiting review; this is not project completion." in rendered.message
    assert "Project completion has been explicitly reconciled" not in rendered.message


def test_pm_render_changes_requested_not_completion():
    task, project, hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "commented",
        {"body": "changes-requested: tighten wording", "metadata": {"changes_requested": True, "requested_change": "Tighten wording.", "task_role": "Review gate"}},
        project,
        hub,
    )
    assert rendered.should_post is True
    _assert_pm_contract(rendered.message)
    assert "Changes requested; this is not project completion." in rendered.message


def test_pm_render_blocker_and_approval_request():
    task, project, hub = _pm_base()
    blocked = kpm.render_project_pm_update(
        task,
        "blocked",
        {"reason": "Project Hub context unavailable.", "metadata": {"task_role": "Context assembly"}},
        project,
        {},
    )
    approval = kpm.render_project_pm_update(
        task,
        "commented",
        {"body": "approval required", "metadata": {"human_approval_required": True, "approval_request": "Approve copying to runtime.", "task_role": "Deployment gate"}},
        project,
        hub,
    )
    assert blocked.should_post is True
    assert approval.should_post is True
    _assert_pm_contract(blocked.message)
    _assert_pm_contract(approval.message)
    assert "Project Hub context unavailable." in blocked.message
    assert "Approve copying to runtime." in approval.message
    assert "Active stage changed." not in blocked.message
    assert "**What changed:** Project Hub context unavailable." in blocked.message


def test_pm_render_final_requires_reconciliation_metadata():
    task, project, hub = _pm_base()
    missing = kpm.render_project_pm_update(task, "project_final_summary", {"summary": "Done."}, project, hub)
    assert missing.should_post is False
    assert missing.suppression_reason == "suppressed-final-missing-reconciliation"
    final = kpm.render_project_pm_update(
        task,
        "project_final_summary",
        {"summary": "internal handoff", "project_completion": True, "metadata": {"public_summary": "All checks passed.", "why_it_matters": "The final state was reconciled explicitly."}},
        {**project, "project_status": "done"},
        {**hub, "status": "done"},
    )
    assert final.should_post is True
    _assert_pm_contract(final.message)
    assert "Project completion has been explicitly reconciled." in final.message


def test_pm_render_malformed_raw_worker_payload_fails_closed():
    task, project, hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "completed",
        {"summary": '{"stdout": "/Users/anton/.hermes/secret"}', "metadata": {"user_visible_change": True}},
        project,
        hub,
    )
    assert rendered.should_post is False
    assert rendered.suppression_reason == "suppressed-no-safe-human-content"


def test_pm_render_missing_project_hub_context_uses_kanban_context():
    task, project, _hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "completed",
        {"summary": "internal handoff", "metadata": {"public_summary": "Completed watcher-side rendering with local Kanban context.", "user_visible_change": True, "task_role": "Implementation stage"}},
        project,
        {},
    )
    assert rendered.should_post is True
    _assert_pm_contract(rendered.message)


def test_pm_renderer_rejects_unfiltered_worker_summary_and_machine_title():
    task, project, hub = _pm_base()
    rendered = kpm.render_project_pm_update(
        task,
        "completed",
        {"summary": "Updated src/models/client.py for forge run-123", "metadata": {"user_visible_change": True}},
        project,
        hub,
    )
    assert rendered.should_post is False
    assert rendered.suppression_reason == "suppressed-no-safe-human-content"
    assert kpm.safe_project_title(
        {"title": "Traceback /Users/private"},
        {"project_title": "t_abc123"},
    ) == "Project update"
