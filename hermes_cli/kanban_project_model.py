"""Project-level helpers for the Hermes Kanban execution model.

Project Hub remains canonical; Kanban tasks carry execution metadata in event
payloads, run metadata, and lightweight body markers. These helpers are kept
pure-SQL / JSON so they can be reused by the Discord watcher, dashboard, DSR,
and janitor without importing gateway code.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Any, Iterable

PROJECT_STATUS = {"idea", "active", "blocked", "review", "done", "archived"}
PROJECT_BODY_MARKER_RE = re.compile(r"^Project Hub slug:\s*(?P<slug>[a-zA-Z0-9][a-zA-Z0-9_-]{0,100})\s*$", re.M)
THREAD_BODY_MARKER_RE = re.compile(r"^Discord thread id:\s*(?P<thread>[0-9]{6,})\s*$", re.M)
ROOT_BODY_MARKER_RE = re.compile(r"^Kanban root task id:\s*(?P<root>t_[a-f0-9]+|[A-Za-z0-9_-]+)\s*$", re.M)
STAGE_BODY_MARKER_RE = re.compile(r"^Kanban stage:\s*(?P<stage>.+?)\s*$", re.M)
RUN_KEY_BODY_MARKER_RE = re.compile(r"^Run key:\s*(?P<run_key>.+?)\s*$", re.M)

ROUTINE_EVENT_KINDS = {"created", "promoted", "claimed", "spawned", "heartbeat", "unblocked", "archived"}
THREAD_EVENT_KINDS = {"completed", "blocked", "failed", "crashed", "timed_out", "spawn_failed", "gave_up", "commented"}
PROJECT_EVENT_KINDS = {"project_kickoff", "project_stage_started", "project_stage_completed", "project_final_summary", "project_blocked", "project_review"}
DANGER_EVENT_KINDS = {"blocked", "failed", "crashed", "timed_out", "spawn_failed", "gave_up"}
PROJECT_PRESENTATION_KINDS = PROJECT_EVENT_KINDS | DANGER_EVENT_KINDS | {"completed", "commented"}
ROUTINE_PRESENTATION_KINDS = ROUTINE_EVENT_KINDS | {"janitor", "promote", "archive"}
FINAL_RECONCILIATION_KEYS = {"project_completion", "project_final", "project_final_reconciliation"}
REVIEW_WORDS = ("review", "review-required", "changes-requested", "changes requested", "approval", "approve")
BLOCKER_WORDS = ("blocked", "blocker", "needs input", "cannot proceed")
MACHINE_SHAPED_RE = re.compile(
    r"(\bTraceback\b|\bstdout\b|\bstderr\b|/Users/|/tmp/|/private/|[A-Za-z]:\\|```|"
    r"\b(?:python|pytest|git|curl|npm|node|uv|hermes)\s+|"
    r"\b(?:task|run|event|thread)_?id\b|t_[a-f0-9]{6,})",
    re.I,
)
JSONISH_RE = re.compile(r"^\s*[\[{].*[\]}]\s*$", re.S)


@dataclass
class ProjectPresentation:
    should_post: bool
    message: str = ""
    suppression_reason: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def project_thread_key(project: dict[str, Any]) -> str:
    """Return the stable Discord state key for one Project Hub run.

    Project Hub slug identifies the long-lived project. Build Lane can run the
    same project repeatedly, so Discord forum state must include run_key when it
    is present; otherwise a new run reuses/collapses into an old forum post.
    """
    slug = str(project.get("project_hub_slug") or "").strip()
    run_key = str(project.get("run_key") or project.get("execution_wave_id") or "").strip()
    return f"{slug}:{run_key}" if run_key else slug


def format_project_thread_starter(project: dict[str, Any]) -> str:
    """Format the first message for a project-run Discord forum post.

    The starter is visible in Discord before any thread update. It must be
    PM-readable only: no Project Hub slugs, Kanban/root/run IDs, paths, handles,
    task counts, or other execution telemetry.
    """
    title = _safe_human_text(project.get("project_title") or project.get("title"), max_len=90) or "Project run"
    purpose = _safe_human_text(project.get("purpose") or project.get("description"), max_len=280)
    state = _safe_human_text(project.get("project_status") or project.get("status"), max_len=80)
    next_step = _safe_human_text(project.get("next_step"), max_len=220)
    lines = [f"**Project started: {title}**"]
    if purpose:
        lines.append(f"**Purpose:** {purpose}")
    if state:
        lines.append(f"**Current state:** {state}")
    if next_step:
        lines.append(f"**Next step:** {next_step}")
    message = "\n".join(lines)
    if MACHINE_SHAPED_RE.search(message) or JSONISH_RE.search(message):
        return "**Project started: Project run**\n**Current state:** active"
    return message[:1800]


def safe_project_title(*sources: dict[str, Any] | None) -> str:
    """Return a Discord-safe project title without machine-shaped fallback."""
    for source in sources:
        source = source or {}
        title = _safe_human_text(source.get("title") or source.get("project_title"), max_len=90)
        if title:
            return title
    return "Project update"


def _json_loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _meta_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    meta = payload.get("metadata") or payload.get("project") or {}
    if isinstance(meta, str):
        meta = _json_loads(meta, {})
    return meta if isinstance(meta, dict) else {}


def _body_marker(body: str | None, regex: re.Pattern[str]) -> str | None:
    if not body:
        return None
    m = regex.search(body)
    if not m:
        return None
    return next(iter(m.groupdict().values())).strip()


def extract_task_project_metadata(task: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized project metadata from task body + event/run payload.

    Supported keys:
    - project_hub_slug
    - project_title
    - discord_thread_id
    - kanban_root_task_id
    - project_status
    - stage_name
    - execution_wave_id
    - dsr_visible
    """
    body = task.get("body") or ""
    payload = payload or {}
    meta = _meta_from_payload(payload).copy()
    out: dict[str, Any] = {}
    aliases = {
        "project_hub_slug": ("project_hub_slug", "project_slug", "hub_slug"),
        "project_title": ("project_title", "title"),
        "discord_thread_id": ("discord_thread_id", "thread_id"),
        "kanban_root_task_id": ("kanban_root_task_id", "root_task_id", "root_id"),
        "project_status": ("project_status", "status"),
        "stage_name": ("stage_name", "stage", "stage_id"),
        "execution_wave_id": ("execution_wave_id", "wave_id"),
        "run_key": ("run_key", "project_run_key"),
        "dsr_visible": ("dsr_visible", "dsr_include"),
    }
    for target, keys in aliases.items():
        for key in keys:
            value = meta.get(key) or payload.get(key)
            if value not in (None, ""):
                out[target] = value
                break
    out.setdefault("project_hub_slug", _body_marker(body, PROJECT_BODY_MARKER_RE))
    out.setdefault("discord_thread_id", _body_marker(body, THREAD_BODY_MARKER_RE))
    out.setdefault("kanban_root_task_id", _body_marker(body, ROOT_BODY_MARKER_RE))
    out.setdefault("stage_name", _body_marker(body, STAGE_BODY_MARKER_RE))
    out.setdefault("run_key", _body_marker(body, RUN_KEY_BODY_MARKER_RE))
    if out.get("project_title") in (None, "") and out.get("project_hub_slug"):
        out["project_title"] = task.get("title") or out.get("project_hub_slug")
    if out.get("kanban_root_task_id") in (None, "") and out.get("project_hub_slug"):
        out["kanban_root_task_id"] = task.get("id")
    status = str(out.get("project_status") or "").strip().lower()
    if status and status not in PROJECT_STATUS:
        status = "active"
    if status:
        out["project_status"] = status
    if "dsr_visible" in out:
        out["dsr_visible"] = bool(out["dsr_visible"])
    return {k: v for k, v in out.items() if v not in (None, "")}


def project_metadata_markers(
    *,
    project_hub_slug: str,
    project_title: str | None = None,
    discord_thread_id: str | None = None,
    kanban_root_task_id: str | None = None,
    stage_name: str | None = None,
) -> str:
    lines = ["", "---", "Kanban project metadata:", f"Project Hub slug: {project_hub_slug}"]
    if project_title:
        lines.append(f"Project title: {project_title}")
    if discord_thread_id:
        lines.append(f"Discord thread id: {discord_thread_id}")
    if kanban_root_task_id:
        lines.append(f"Kanban root task id: {kanban_root_task_id}")
    if stage_name:
        lines.append(f"Kanban stage: {stage_name}")
    return "\n".join(lines) + "\n"


def task_lookup(con: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else {"id": task_id, "title": task_id, "body": "", "status": "unknown", "assignee": "unknown"}


def latest_run_metadata(con: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT metadata, summary, outcome FROM task_runs WHERE task_id=? ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return {}
    meta = _json_loads(row["metadata"], {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    if row["summary"]:
        meta.setdefault("latest_summary", row["summary"])
    if row["outcome"]:
        meta.setdefault("latest_outcome", row["outcome"])
    return meta


def component_task_ids(con: sqlite3.Connection, task_id: str) -> set[str]:
    seen = {task_id}
    q = [task_id]
    while q:
        cur = q.pop(0)
        for row in con.execute("SELECT parent_id, child_id FROM task_links WHERE parent_id=? OR child_id=?", (cur, cur)).fetchall():
            parent, child = row["parent_id"], row["child_id"]
            for nxt in (parent, child):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
    return seen


def component_tasks(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    return [task_lookup(con, tid) for tid in sorted(component_task_ids(con, task_id))]


def component_root(con: sqlite3.Connection, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {t["id"] for t in tasks}
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = con.execute(f"SELECT child_id FROM task_links WHERE child_id IN ({placeholders})", tuple(ids)).fetchall()
    children = {r["child_id"] for r in rows}
    roots = [t for t in tasks if t["id"] not in children] or tasks
    roots.sort(key=lambda t: (-(t.get("priority") or 0), t.get("created_at") or 0, t.get("id") or ""))
    return roots[0]


def resolve_project_context(con: sqlite3.Connection, task_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    task = task_lookup(con, task_id)
    meta = extract_task_project_metadata(task, payload)
    tasks = component_tasks(con, task_id)
    root = component_root(con, tasks)
    if not meta.get("project_hub_slug"):
        for candidate in [root, *tasks]:
            candidate_meta = extract_task_project_metadata(candidate, payload if candidate.get("id") == task_id else None)
            if candidate_meta.get("project_hub_slug"):
                meta = {**candidate_meta, **meta}
                break
    if meta.get("project_hub_slug"):
        meta.setdefault("kanban_root_task_id", root.get("id") or task_id)
        meta.setdefault("project_title", root.get("title") or task.get("title") or meta["project_hub_slug"])
        meta.setdefault("project_status", map_project_status(tasks))
        meta["task_ids"] = sorted(t["id"] for t in tasks)
        return meta
    return {}


def map_project_status(tasks: Iterable[dict[str, Any]]) -> str:
    statuses = {str(t.get("status") or "").lower() for t in tasks}
    active = statuses - {"archived"}
    if not active:
        return "archived"
    if "blocked" in active:
        return "blocked"
    if "review" in active:
        return "review"
    if active and active <= {"done"}:
        return "done"
    if active & {"ready", "running", "todo", "scheduled", "triage"}:
        return "active"
    return "active"


def should_post_project_thread_event(kind: str, payload: dict[str, Any] | None = None) -> bool:
    payload = payload or {}
    if kind in PROJECT_EVENT_KINDS:
        return True
    if kind in ROUTINE_EVENT_KINDS:
        return False
    if kind in DANGER_EVENT_KINDS:
        return True
    if kind == "completed":
        meta = _meta_from_payload(payload)
        # Completion summaries are required for Kanban handoffs, but they are
        # not automatically Discord-worthy. Project-thread completion posts are
        # opt-in via explicit user-visible / DSR / final-project metadata so
        # routine leaf completions do not become thread spam.
        return bool(
            meta.get("user_visible_change")
            or meta.get("dsr_visible")
            or meta.get("dsr_include")
            or meta.get("project_final")
            or meta.get("project_completion")
        )
    if kind == "commented":
        text = str(payload.get("body") or payload.get("comment") or "").lower()
        return any(word in text for word in ("block", "approval", "review", "done", "complete", "milestone"))
    return False


def _first_text(*values: Any) -> str:
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _payload_meta(payload: dict[str, Any] | None) -> dict[str, Any]:
    return _meta_from_payload(payload or {})


def _safe_human_text(value: Any, *, max_len: int = 280) -> str:
    text = _first_text(value)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if JSONISH_RE.match(text) or MACHINE_SHAPED_RE.search(text):
        return ""
    return text[:max_len].rstrip()


def _safe_list(values: Any, *, limit: int = 3) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = _safe_human_text(value, max_len=180)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _section(lines: list[str], label: str, value: str | list[str] | None) -> None:
    if isinstance(value, list):
        clean = [v for v in value if v]
        if clean:
            lines.append(f"**{label}:** " + "; ".join(clean))
        return
    if value:
        lines.append(f"**{label}:** {value}")


def _is_final_event(kind: str, payload: dict[str, Any], meta: dict[str, Any]) -> bool:
    if kind == "project_final_summary":
        return True
    return any(bool(payload.get(k) or meta.get(k)) for k in FINAL_RECONCILIATION_KEYS)


def _meaningful_completion(kind: str, payload: dict[str, Any], meta: dict[str, Any]) -> bool:
    if kind in {"project_stage_completed", "project_final_summary"}:
        return True
    return bool(
        meta.get("user_visible_change")
        or meta.get("dsr_visible")
        or meta.get("dsr_include")
        or meta.get("meaningful_handoff")
        or meta.get("stage_completion")
        or meta.get("task_completion")
        or _is_final_event(kind, payload, meta)
    )


def _active_transition(kind: str, payload: dict[str, Any], meta: dict[str, Any]) -> bool:
    return kind == "project_stage_started" and bool(
        payload.get("stage")
        or payload.get("stage_name")
        or meta.get("stage")
        or meta.get("stage_name")
        or meta.get("active_stage_transition")
    )


def _needs_attention(kind: str, payload: dict[str, Any], meta: dict[str, Any]) -> bool:
    text = _first_text(payload.get("reason"), payload.get("summary"), payload.get("body"), payload.get("comment"), meta.get("state")).lower()
    return (
        kind in DANGER_EVENT_KINDS
        or bool(meta.get("review_required") or meta.get("changes_requested") or meta.get("human_approval_required"))
        or any(word in text for word in REVIEW_WORDS + BLOCKER_WORDS)
    )


def should_present_project_update(kind: str, payload: dict[str, Any] | None = None) -> tuple[bool, str]:
    payload = payload or {}
    meta = _payload_meta(payload)
    if kind in ROUTINE_PRESENTATION_KINDS:
        return False, f"suppressed-routine-{kind}"
    if kind not in PROJECT_PRESENTATION_KINDS:
        return False, f"suppressed-unsupported-{kind}"
    if kind == "project_kickoff":
        return True, ""
    if _active_transition(kind, payload, meta):
        return True, ""
    if kind == "completed" and not _meaningful_completion(kind, payload, meta):
        return False, "suppressed-low-information-completion"
    if kind == "commented" and not _needs_attention(kind, payload, meta):
        return False, "suppressed-low-information-comment"
    if kind in {"completed", "commented"} or kind in DANGER_EVENT_KINDS or kind in PROJECT_EVENT_KINDS:
        return True, ""
    return False, f"suppressed-unsupported-{kind}"


def render_project_pm_update(
    task: dict[str, Any],
    kind: str,
    payload: dict[str, Any] | None,
    project: dict[str, Any],
    project_hub: dict[str, Any] | None = None,
) -> ProjectPresentation:
    """Render the deterministic PM-facing Discord update for a project event."""
    payload = payload or {}
    project_hub = project_hub or {}
    meta = _payload_meta(payload)
    ok, reason = should_present_project_update(kind, payload)
    if not ok:
        return ProjectPresentation(False, suppression_reason=reason)

    is_final = _is_final_event(kind, payload, meta)
    if is_final and not any(payload.get(k) or meta.get(k) for k in FINAL_RECONCILIATION_KEYS):
        return ProjectPresentation(False, suppression_reason="suppressed-final-missing-reconciliation")

    title = _safe_human_text(
        project_hub.get("title") or project_hub.get("name") or project.get("project_title") or task.get("title"),
        max_len=90,
    )
    if not title:
        return ProjectPresentation(False, suppression_reason="suppressed-missing-safe-title")

    purpose = _safe_human_text(
        project_hub.get("purpose") or project_hub.get("description") or project.get("description") or meta.get("purpose"),
        max_len=300,
    )
    phase = _safe_human_text(project_hub.get("phase") or project.get("phase") or meta.get("phase"), max_len=80)
    status = _safe_human_text(project_hub.get("status") or project.get("project_status") or task.get("status"), max_len=80)
    next_step = _safe_human_text(
        payload.get("next_step") or meta.get("next_step") or project_hub.get("next_step") or project.get("next_step"),
        max_len=220,
    )
    stage = _safe_human_text(
        payload.get("stage_name") or payload.get("stage") or meta.get("stage_name") or meta.get("stage") or project.get("stage_name"),
        max_len=90,
    )
    role = _safe_human_text(meta.get("task_role") or meta.get("stage_role") or payload.get("role"), max_len=120)
    # Raw worker summaries/results are internal handoffs. Only fields explicitly
    # marked as public presentation copy may cross the Discord boundary.
    summary = _safe_human_text(meta.get("public_summary") or meta.get("dsr_summary"), max_len=300)
    reason_text = _safe_human_text(
        meta.get("why_it_matters") or meta.get("why") or payload.get("reason") or meta.get("rationale"),
        max_len=300,
    )
    blocker = _safe_human_text(payload.get("reason") or payload.get("error") or meta.get("blocker"), max_len=260)
    changed = _safe_list(meta.get("meaningful_handoff") or meta.get("result") or meta.get("outputs") or meta.get("findings"))
    dependency = _safe_human_text(meta.get("dependency_progress") or payload.get("dependency_progress"), max_len=220)

    review_state = ""
    action = "No action needed."
    if meta.get("changes_requested") or "changes-requested" in _first_text(payload.get("reason"), payload.get("summary")).lower():
        review_state = "Changes requested; this is not project completion."
        action = _safe_human_text(meta.get("requested_change") or payload.get("reason"), max_len=220) or "Address the requested changes before treating this as complete."
    elif meta.get("review_required") or "review-required" in _first_text(payload.get("reason"), payload.get("summary")).lower():
        review_state = "Awaiting review; this is not project completion."
        action = _safe_human_text(meta.get("review_request") or payload.get("reason"), max_len=220) or "Review is required before this can be reconciled."
    elif meta.get("human_approval_required") or "approval" in _first_text(payload.get("reason"), payload.get("summary"), payload.get("body")).lower():
        review_state = "Approval required before work continues."
        action = _safe_human_text(meta.get("approval_request") or payload.get("reason") or payload.get("body"), max_len=220) or "Approve or reject the requested next step."
    elif kind in DANGER_EVENT_KINDS:
        action = blocker or "Resolve the blocker before work continues."

    if not any([summary, reason_text, blocker, changed, action != "No action needed.", _active_transition(kind, payload, meta), kind == "project_kickoff", is_final]):
        return ProjectPresentation(False, suppression_reason="suppressed-no-safe-human-content")

    heading_prefix = {
        "project_kickoff": "Project kickoff",
        "project_stage_started": "Stage started",
        "project_stage_completed": "Stage completed",
        "project_final_summary": "Project reconciled",
        "completed": "Work completed" if not is_final else "Project reconciled",
        "commented": "Decision needed" if action != "No action needed." else "Project update",
        "blocked": "Blocked",
    }.get(kind, kind.replace("_", " ").title())

    if is_final:
        current = "Project completion has been explicitly reconciled."
    elif review_state:
        current = review_state
    elif kind == "completed":
        current = "A project task or stage completed; the whole project is still governed by remaining review and reconciliation."
    elif kind == "project_stage_started":
        current = "Work has moved into the next active stage."
    else:
        current = status or "Project work is active."

    lines = [f"**{heading_prefix}: {title}**"]
    where = ", ".join(part for part in (f"Phase {phase}" if phase else "", f"status {status}" if status else "", f"stage {stage}" if stage else "") if part)
    _section(lines, "Where we are", where)
    _section(lines, "How this work fits", role or purpose)
    if changed:
        change_text = changed
    elif summary:
        change_text = summary
    elif review_state:
        change_text = review_state
    elif kind == "project_kickoff":
        change_text = "Project run opened."
    elif kind in DANGER_EVENT_KINDS:
        change_text = blocker or "Work is blocked and needs attention."
    elif _active_transition(kind, payload, meta):
        change_text = "Active stage changed."
    else:
        change_text = "Project status updated."
    _section(lines, "What changed", change_text)
    _section(lines, "Why it was done/why it matters", reason_text or purpose)
    _section(lines, "Current state", current if dependency == "" else f"{current} Dependency progress: {dependency}")
    _section(lines, "Next step", next_step)
    _section(lines, "Action needed", action)
    message = "\n".join(lines)
    if MACHINE_SHAPED_RE.search(message) or JSONISH_RE.search(message):
        return ProjectPresentation(False, suppression_reason="suppressed-rendered-machine-shaped-content")
    return ProjectPresentation(True, message=message[:1800])


def format_project_thread_update(task: dict[str, Any], kind: str, payload: dict[str, Any] | None, project: dict[str, Any]) -> str:
    rendered = render_project_pm_update(task, kind, payload or {}, project, {})
    return rendered.message if rendered.should_post else ""


def project_rows(con: sqlite3.Connection, include_archived: bool = False) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM tasks ORDER BY created_at ASC").fetchall()
    tasks = [dict(r) for r in rows]
    by_slug: dict[str, dict[str, Any]] = {}
    for task in tasks:
        meta = extract_task_project_metadata(task)
        if not meta.get("project_hub_slug"):
            continue
        slug = str(meta["project_hub_slug"])
        entry = by_slug.setdefault(slug, {
            "project_hub_slug": slug,
            "title": meta.get("project_title") or task.get("title") or slug,
            "project_hub_status": "active",
            "kanban_root_task_id": meta.get("kanban_root_task_id") or task.get("id"),
            "discord_thread_id": meta.get("discord_thread_id"),
            "tasks": [],
            "active_agents": [],
            "blockers": [],
            "latest_update": None,
            "next_step": None,
        })
        entry["tasks"].append(task)
        if meta.get("discord_thread_id"):
            entry["discord_thread_id"] = meta["discord_thread_id"]
        if meta.get("kanban_root_task_id"):
            entry["kanban_root_task_id"] = meta["kanban_root_task_id"]
    for slug, entry in list(by_slug.items()):
        tasks = entry["tasks"]
        status = map_project_status(tasks)
        if status == "archived" and not include_archived:
            del by_slug[slug]
            continue
        entry["project_hub_status"] = status
        active_agents = sorted({t.get("assignee") for t in tasks if t.get("status") in {"ready", "running", "todo", "scheduled", "triage"} and t.get("assignee")})
        blockers = [t for t in tasks if t.get("status") == "blocked"]
        entry["active_agents"] = active_agents
        entry["blockers"] = [{"id": t["id"], "title": t.get("title"), "assignee": t.get("assignee"), "last_failure_error": t.get("last_failure_error")} for t in blockers]
        latest_task = max(tasks, key=lambda t: max(t.get("completed_at") or 0, t.get("started_at") or 0, t.get("created_at") or 0)) if tasks else None
        if latest_task:
            ts = max(latest_task.get("completed_at") or 0, latest_task.get("started_at") or 0, latest_task.get("created_at") or 0)
            entry["latest_update"] = {"at": ts, "task_id": latest_task.get("id"), "title": latest_task.get("title"), "status": latest_task.get("status")}
        next_candidates = [t for t in tasks if t.get("status") in {"ready", "running", "todo", "scheduled", "triage", "blocked"}]
        if next_candidates:
            next_candidates.sort(key=lambda t: (-(t.get("priority") or 0), t.get("created_at") or 0))
            entry["next_step"] = next_candidates[0].get("title")
        entry["counts"] = {s: sum(1 for t in tasks if t.get("status") == s) for s in sorted({t.get("status") for t in tasks})}
        entry["task_count"] = len(tasks)
        entry.pop("tasks", None)
    return sorted(by_slug.values(), key=lambda p: ((p.get("project_hub_status") == "archived"), -(p.get("latest_update") or {}).get("at", 0), p.get("title") or ""))


def archive_completed_project_tasks(con: sqlite3.Connection, older_than_seconds: int = 48 * 3600, now_ts: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Silently archive completed project-linked tasks older than retention.

    Adds only `archived` events with `discord_silent=True`; the Discord watcher
    must ignore archived events regardless.
    """
    now_ts = now_ts or int(time.time())
    cutoff = now_ts - int(older_than_seconds)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM tasks WHERE status='done' AND completed_at IS NOT NULL AND completed_at < ?", (cutoff,)).fetchall()
    candidates = []
    for row in rows:
        task = dict(row)
        if extract_task_project_metadata(task):
            candidates.append(task)
    if dry_run:
        return {"archived": [], "candidates": [t["id"] for t in candidates], "cutoff": cutoff}
    archived = []
    with con:
        for task in candidates:
            con.execute("UPDATE tasks SET status='archived' WHERE id=? AND status='done'", (task["id"],))
            con.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'archived', ?, ?)",
                (task["id"], json.dumps({"reason": "project-retention-48h", "discord_silent": True}), now_ts),
            )
            archived.append(task["id"])
    return {"archived": archived, "candidates": [t["id"] for t in candidates], "cutoff": cutoff}


def dsr_project_activity(con: sqlite3.Connection, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    out: list[dict[str, Any]] = []

    dsr_event_kinds = sorted(PROJECT_EVENT_KINDS | {"completed", "commented"})
    event_rows = con.execute(
        """
        SELECT e.id AS event_id, e.task_id, e.kind, e.payload, e.created_at,
               t.title, t.body, t.assignee, t.status, t.result
        FROM task_events e
        LEFT JOIN tasks t ON t.id=e.task_id
        WHERE e.created_at >= ? AND e.created_at < ? AND e.kind IN (%s)
        ORDER BY e.created_at DESC, e.id DESC
        LIMIT ?
        """ % ",".join("?" for _ in dsr_event_kinds),
        (start_ts, end_ts, *dsr_event_kinds, limit * 4),
    ).fetchall()
    for row in event_rows:
        event = dict(row)
        payload = _json_loads(event.get("payload"), {}) or {}
        if not isinstance(payload, dict):
            payload = {}
        payload_meta = _meta_from_payload(payload)
        visible = bool(
            payload.get("dsr_visible")
            or payload.get("dsr_include")
            or payload_meta.get("dsr_visible")
            or payload_meta.get("dsr_include")
            or payload_meta.get("user_visible_change")
            or payload_meta.get("project_final")
            or payload_meta.get("project_completion")
            or (event.get("kind") == "commented" and should_post_project_thread_event("commented", payload))
        )
        if not visible:
            continue
        task = {
            "id": event.get("task_id"),
            "title": event.get("title"),
            "body": event.get("body"),
            "assignee": event.get("assignee"),
            "status": event.get("status"),
            "result": event.get("result"),
        }
        meta = extract_task_project_metadata(task, payload)
        if not meta.get("project_hub_slug"):
            continue
        event_kind = event.get("kind")
        comment_body = payload.get("body") or payload.get("comment")
        summary = payload_meta.get("dsr_summary") or payload.get("summary") or event.get("result") or event_kind
        if event_kind == "commented" and comment_body:
            summary = str(comment_body)
        event_metadata = {
            **payload_meta,
            **{k: v for k, v in payload.items() if k in {"stage", "stage_name", "run_key", "work_kind", "dsr_visible", "dsr_include"}},
        }
        if event_kind == "commented" and comment_body:
            event_metadata["body"] = str(comment_body)
        out.append({
            "project_hub_slug": meta.get("project_hub_slug"),
            "project_title": meta.get("project_title") or event.get("title"),
            "task_id": event.get("task_id"),
            "title": event.get("title"),
            "assignee": event.get("assignee"),
            "summary": summary,
            "completed_at": event.get("created_at"),
            "metadata": event_metadata,
            "event_id": event.get("event_id"),
            "event_kind": event_kind,
        })
        if len(out) >= limit:
            return out

    rows = con.execute(
        """
        SELECT t.*, r.summary AS run_summary, r.metadata AS run_metadata, r.outcome AS run_outcome
        FROM tasks t
        LEFT JOIN task_runs r ON r.id = (
            SELECT id FROM task_runs rr WHERE rr.task_id=t.id ORDER BY COALESCE(rr.ended_at, rr.started_at) DESC, rr.id DESC LIMIT 1
        )
        WHERE t.completed_at >= ? AND t.completed_at < ?
        ORDER BY t.completed_at DESC
        LIMIT ?
        """,
        (start_ts, end_ts, limit * 4),
    ).fetchall()
    for row in rows:
        task = dict(row)
        payload_meta = _json_loads(task.get("run_metadata"), {}) or {}
        meta = extract_task_project_metadata(task, {"metadata": payload_meta})
        user_visible = bool(payload_meta.get("user_visible_change") or payload_meta.get("dsr_visible") or payload_meta.get("dsr_include") or payload_meta.get("project_final") or payload_meta.get("project_completion"))
        if not meta.get("project_hub_slug") and not user_visible:
            continue
        if not user_visible and task.get("status") == "done":
            # DSR should not become a worker attendance sheet.
            continue
        out.append({
            "project_hub_slug": meta.get("project_hub_slug"),
            "project_title": meta.get("project_title") or task.get("title"),
            "task_id": task.get("id"),
            "title": task.get("title"),
            "assignee": task.get("assignee"),
            "summary": payload_meta.get("dsr_summary") or task.get("run_summary") or task.get("result") or "completed",
            "completed_at": task.get("completed_at"),
            "metadata": payload_meta,
        })
        if len(out) >= limit:
            break
    return out


__all__ = [
    "PROJECT_STATUS",
    "extract_task_project_metadata",
    "project_metadata_markers",
    "resolve_project_context",
    "should_post_project_thread_event",
    "should_present_project_update",
    "render_project_pm_update",
    "ProjectPresentation",
    "project_thread_key",
    "format_project_thread_starter",
    "safe_project_title",
    "format_project_thread_update",
    "project_rows",
    "archive_completed_project_tasks",
    "dsr_project_activity",
]
