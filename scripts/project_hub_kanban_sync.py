#!/usr/bin/env python3
"""Deterministic Kanban project event -> Project Hub update mapper.

Project Hub remains canonical: this helper does not read or mutate Project Hub.
It converts an already-approved Kanban project lifecycle event into the smallest
Project Hub update intent that an external sync/apply layer may review/apply.

The mapper is intentionally conservative and low-noise:
- root/project kickoff becomes one Project Hub event;
- human blockers and not-adopted reviews update review/next_step;
- final adopted closeout updates status/next_step and emits a closeout event;
- leaf lifecycle chatter (claimed/spawned/heartbeat/routine child done) is suppressed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

LEAF_LIFECYCLE_EVENTS = {
    "claimed",
    "spawned",
    "heartbeat",
    "started",
    "running",
    "retry",
    "reclaimed",
    "worker_comment",
}

HUMAN_BLOCKER_RE = re.compile(
    r"\b(human|matthew|owner|approval|approve|review|credential|token|secret|account|"
    r"budget|payment|external|destructive|confirm|decision|blocked for review)\b",
    re.IGNORECASE,
)

NOT_ADOPTED_RE = re.compile(r"\b(not[- ]adopted|not adopted|rejected|declined|abandoned|cancelled|canceled)\b", re.IGNORECASE)
ADOPTED_RE = re.compile(r"\b(adopted|accepted|approved|final|closeout|completed|done|shipped)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ProjectHubIntent:
    """A deterministic, side-effect-free Project Hub mutation proposal."""

    action: str
    project_slug: str
    source: str = "kanban"
    kanban_task_id: str | None = None
    event_type: str | None = None
    event_summary: str | None = None
    review: str | None = None
    status: str | None = None
    next_step: str | None = None
    dsr_visibility: str = "omit"
    reason: str = ""
    idempotency_key: str | None = None
    suppressed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _meta(event: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = event.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _project_slug(event: Mapping[str, Any]) -> str:
    metadata = _meta(event)
    for key in ("project_hub_slug", "project_slug", "slug"):
        value = _text(event.get(key) or metadata.get(key))
        if value:
            return value
    project_hub = metadata.get("project_hub")
    if isinstance(project_hub, Mapping):
        value = _text(project_hub.get("slug"))
        if value:
            return value
    return "unresolved"


def _task_id(event: Mapping[str, Any]) -> str | None:
    value = _text(event.get("kanban_task_id") or event.get("task_id") or _meta(event).get("task_id"))
    return value or None


def _root_task_id(event: Mapping[str, Any]) -> str | None:
    value = _text(event.get("kanban_root_task_id") or event.get("root_task_id") or _meta(event).get("root_task_id"))
    return value or None


def _event_kind(event: Mapping[str, Any]) -> str:
    return _text(event.get("event") or event.get("event_kind") or event.get("kind") or event.get("status")).lower()


def _stage(event: Mapping[str, Any]) -> str:
    return _text(event.get("stage") or event.get("kanban_stage") or _meta(event).get("stage")).lower()


def _summary(event: Mapping[str, Any]) -> str:
    for key in ("summary", "result", "title", "reason", "body"):
        value = _text(event.get(key))
        if value:
            return " ".join(value.split())
    return "Kanban project lifecycle event recorded."


def _next_step(event: Mapping[str, Any], default: str) -> str:
    metadata = _meta(event)
    project_hub = metadata.get("project_hub") if isinstance(metadata.get("project_hub"), Mapping) else {}
    for value in (
        event.get("next_step"),
        metadata.get("next_step"),
        project_hub.get("next_step") if isinstance(project_hub, Mapping) else None,
    ):
        text = _text(value)
        if text:
            return text
    return default


def _is_leaf(event: Mapping[str, Any]) -> bool:
    task_id = _task_id(event)
    root_task_id = _root_task_id(event)
    explicit = event.get("is_leaf") or _meta(event).get("is_leaf")
    if isinstance(explicit, bool):
        return explicit
    if task_id and root_task_id and task_id != root_task_id:
        return True
    parent_ids = event.get("parent_ids") or _meta(event).get("parent_ids")
    if isinstance(parent_ids, Iterable) and not isinstance(parent_ids, (str, bytes, Mapping)):
        return bool(list(parent_ids)) and not event.get("aggregate", False)
    return False


def _idempotency(task_id: str | None, kind: str, project_slug: str) -> str:
    basis = task_id or "no-task"
    return f"project-hub-sync:{project_slug}:{basis}:{kind}"


def suppress_intent(event: Mapping[str, Any], reason: str) -> ProjectHubIntent:
    slug = _project_slug(event)
    task_id = _task_id(event)
    return ProjectHubIntent(
        action="none",
        project_slug=slug,
        kanban_task_id=task_id,
        reason=reason,
        suppressed=True,
        idempotency_key=_idempotency(task_id, "none", slug),
    )


def map_kanban_event(event: Mapping[str, Any]) -> ProjectHubIntent:
    """Map one Kanban lifecycle event to a Project Hub update intent.

    Input is a normalized event dict from Kanban/project orchestration. Unknown
    fields are ignored; output is stable for the same input.
    """

    kind = _event_kind(event)
    stage = _stage(event)
    slug = _project_slug(event)
    task_id = _task_id(event)
    summary = _summary(event)
    blob = "\n".join(_text(event.get(k)) for k in ("event", "kind", "status", "stage", "summary", "result", "reason", "title", "body"))

    if kind in LEAF_LIFECYCLE_EVENTS:
        return suppress_intent(event, "Leaf/internal lifecycle chatter is kept in Kanban only.")
    if _is_leaf(event) and kind in {"done", "completed", "complete", "blocked", "cancelled", "canceled"} and not event.get("aggregate"):
        # The lifecycle kind itself (for example "done") is not enough to make a
        # child task Project Hub-worthy. Only promote leaf events when their
        # human-authored text names a blocker/adoption/not-adoption boundary.
        human_text = "\n".join(_text(event.get(k)) for k in ("summary", "result", "reason", "title", "body"))
        if not HUMAN_BLOCKER_RE.search(human_text) and not ADOPTED_RE.search(human_text) and not NOT_ADOPTED_RE.search(human_text):
            return suppress_intent(event, "Routine leaf lifecycle event is included in its parent Kanban aggregate.")

    if kind in {"kickoff", "created", "project_started"} or stage in {"kickoff", "project-kickoff"}:
        next_step = _next_step(event, "Continue execution in Kanban; update Project Hub only at blocker/review/closeout boundaries.")
        return ProjectHubIntent(
            action="add_event",
            project_slug=slug,
            kanban_task_id=task_id,
            event_type="kanban_kickoff",
            event_summary=f"Kanban kickoff: {summary}",
            next_step=next_step,
            dsr_visibility="watch",
            reason="Root project kickoff is operator-relevant but should remain concise.",
            idempotency_key=_idempotency(task_id, "kickoff", slug),
        )

    if kind in {"blocked", "blocker", "human_blocker"} or HUMAN_BLOCKER_RE.search(blob):
        if not HUMAN_BLOCKER_RE.search(blob):
            return suppress_intent(event, "Blocked event is not human/project-level; Kanban remains the ledger.")
        next_step = _next_step(event, "Human/operator decision required; resolve blocker in Kanban before continuing.")
        return ProjectHubIntent(
            action="update_project",
            project_slug=slug,
            kanban_task_id=task_id,
            event_type="kanban_human_blocker",
            event_summary=f"Kanban blocker: {summary}",
            review="human_blocker",
            status="blocked",
            next_step=next_step,
            dsr_visibility="include",
            reason="Human-relevant blocker should be visible in Project Hub review/next_step.",
            idempotency_key=_idempotency(task_id, "human_blocker", slug),
        )

    if kind in {"not_adopted", "review_not_adopted"} or NOT_ADOPTED_RE.search(blob):
        next_step = _next_step(event, "Project Hub remains canonical; choose a revised path or close the initiative.")
        return ProjectHubIntent(
            action="update_project",
            project_slug=slug,
            kanban_task_id=task_id,
            event_type="kanban_not_adopted_review",
            event_summary=f"Kanban review not adopted: {summary}",
            review="not_adopted",
            status="review",
            next_step=next_step,
            dsr_visibility="watch",
            reason="Not-adopted review changes the canonical Project Hub next step without treating Kanban as canonical.",
            idempotency_key=_idempotency(task_id, "not_adopted", slug),
        )

    if kind in {"final", "closeout", "adopted", "completed", "done"} or ADOPTED_RE.search(blob):
        next_step = _next_step(event, "No further Kanban execution required unless Project Hub opens follow-up work.")
        return ProjectHubIntent(
            action="closeout_project",
            project_slug=slug,
            kanban_task_id=task_id,
            event_type="kanban_adopted_closeout",
            event_summary=f"Kanban closeout adopted: {summary}",
            review="adopted",
            status="completed",
            next_step=next_step,
            dsr_visibility="include",
            reason="Final/adopted closeout is a canonical Project Hub status/next_step update.",
            idempotency_key=_idempotency(task_id, "adopted_closeout", slug),
        )

    return suppress_intent(event, "No deterministic Project Hub mapping rule matched; leave the detail in Kanban.")


def map_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [map_kanban_event(event).to_dict() for event in events]


def _load_events(path: str | None) -> list[Mapping[str, Any]]:
    raw = sys.stdin.read() if not path or path == "-" else open(path, "r", encoding="utf-8").read()
    payload = json.loads(raw)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        events = payload.get("events")
        if isinstance(events, list):
            return [item for item in events if isinstance(item, Mapping)]
        return [payload]
    raise ValueError("input must be a JSON object, a JSON array, or an object with an events array")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Map Kanban project lifecycle events to deterministic Project Hub update intents")
    parser.add_argument("events_json", nargs="?", help="Path to JSON event/object list; omit or '-' to read stdin")
    parser.add_argument("--json", action="store_true", help="Emit JSON only (default also emits JSON; retained for explicit callers)")
    args = parser.parse_args(argv)

    try:
        events = _load_events(args.events_json)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"writes_performed": False, "intents": map_events(events)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
