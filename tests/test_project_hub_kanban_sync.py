from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_hub_kanban_sync.py"


def run_report(event: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


def run_helper(event: dict) -> dict:
    return run_report(event)["intents"][0]


def base_event(**overrides):
    event = {
        "project_hub_slug": "build-implementation-lane",
        "kanban_root_task_id": "t_root",
        "kanban_task_id": "t_root",
        "summary": "Build Lane implementation accepted for execution.",
    }
    event.update(overrides)
    return event


def test_helper_is_side_effect_free():
    report = run_report(base_event(event="kickoff"))

    assert report["writes_performed"] is False


def test_kickoff_maps_to_concise_event():
    intent = run_helper(base_event(event="kickoff", next_step="Run the approved Kanban implementation lane."))

    assert intent["action"] == "add_event"
    assert intent["event_type"] == "kanban_kickoff"
    assert intent["event_summary"] == "Kanban kickoff: Build Lane implementation accepted for execution."
    assert intent["next_step"] == "Run the approved Kanban implementation lane."


def test_human_blocker_updates_review_status_and_next_step():
    intent = run_helper(
        base_event(
            event="blocked",
            summary="Missing GitHub token requires Matthew approval.",
            next_step="Matthew provides token or approves tokenless fallback.",
        )
    )

    assert intent["action"] == "update_project"
    assert intent["review"] == "human_blocker"
    assert intent["status"] == "blocked"
    assert intent["dsr_visibility"] == "include"
    assert intent["next_step"] == "Matthew provides token or approves tokenless fallback."


def test_not_adopted_review_sets_review_next_step_without_closeout():
    intent = run_helper(
        base_event(
            event="review_not_adopted",
            summary="Reviewer rejected the implementation as not adopted.",
            next_step="Revise the helper contract before another implementation run.",
        )
    )

    assert intent["action"] == "update_project"
    assert intent["event_type"] == "kanban_not_adopted_review"
    assert intent["review"] == "not_adopted"
    assert intent["status"] == "review"
    assert intent["next_step"] == "Revise the helper contract before another implementation run."


def test_final_adopted_closeout_updates_status_and_next_step():
    intent = run_helper(
        base_event(
            event="final",
            summary="Reviewer adopted the deterministic sync helper.",
            next_step="No further execution required.",
        )
    )

    assert intent["action"] == "closeout_project"
    assert intent["event_type"] == "kanban_adopted_closeout"
    assert intent["review"] == "adopted"
    assert intent["status"] == "completed"
    assert intent["next_step"] == "No further execution required."


def test_leaf_lifecycle_chatter_is_suppressed():
    intent = run_helper(
        base_event(
            event="heartbeat",
            kanban_task_id="t_leaf",
            summary="worker still running",
        )
    )

    assert intent["action"] == "none"
    assert intent["suppressed"] is True
    assert "chatter" in intent["reason"]


def test_routine_leaf_done_is_suppressed():
    intent = run_helper(
        base_event(
            event="done",
            kanban_task_id="t_leaf",
            summary="Updated one internal test fixture.",
        )
    )

    assert intent["action"] == "none"
    assert intent["suppressed"] is True
    assert "Routine leaf" in intent["reason"]


def test_output_is_deterministic_for_same_event():
    event = base_event(event="blocked", summary="Approval needed from Matthew.")

    assert run_helper(event) == run_helper(event)
