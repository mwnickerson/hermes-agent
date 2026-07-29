#!/usr/bin/env python3
"""Post Hermes Kanban task events to Discord.

Watches ~/.hermes/kanban.db task_events and posts lifecycle updates. By default,
connected Kanban task graphs are grouped into project-scoped Discord threads
instead of a noisy flat #kanban-log stream.

Managed local deployment:
    install -m 0755 scripts/kanban_discord_log.py ~/.hermes/scripts/kanban_discord_log.py

The checked-in script is the runtime source of truth for the local watcher; the
copy under ~/.hermes/scripts/ is just the operator-managed deployment target.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import deque
from pathlib import Path

HERMES_AGENT_ROOT = HOME = Path.home() / ".hermes"
SOURCE_REPO = Path(__file__).resolve().parents[1]
AGENT_REPO = SOURCE_REPO if (SOURCE_REPO / "hermes_cli").exists() else HERMES_AGENT_ROOT / "hermes-agent"
DEPLOYED_LIB = Path(__file__).resolve().parent / "lib"
if str(AGENT_REPO) not in sys.path:
    sys.path.insert(0, str(AGENT_REPO))
if DEPLOYED_LIB.exists() and str(DEPLOYED_LIB) not in sys.path:
    # Runtime-private modules must win over a stale local Hermes checkout.
    sys.path.insert(0, str(DEPLOYED_LIB))

try:
    from hermes_cli import kanban_project_model as kpm  # type: ignore
except ImportError:  # pragma: no cover - local deployment may predate project model helpers
    PROJECT_MODEL_IMPORT_ERROR = True

    class _ProjectModelFallback:
        class ProjectPresentation:
            def __init__(self, should_post=False, message="", suppression_reason="suppressed-project-model-unavailable"):
                self.should_post = should_post
                self.message = message
                self.suppression_reason = suppression_reason

        @staticmethod
        def extract_task_project_metadata(*_args, **_kwargs):
            return {}

        @staticmethod
        def resolve_project_context(*_args, **_kwargs):
            return {}

        @staticmethod
        def project_thread_key(_ctx):
            return ""

        @staticmethod
        def should_post_project_thread_event(*_args, **_kwargs):
            return False

        @staticmethod
        def format_project_thread_starter(_ctx):
            return "**Project update suppressed**\n**Current state:** project model unavailable"

        @staticmethod
        def safe_project_title(*_sources):
            return "Project update"

        @staticmethod
        def render_project_pm_update(*_args, **_kwargs):
            return _ProjectModelFallback.ProjectPresentation()

        @staticmethod
        def format_project_thread_update(*_args, **_kwargs):
            return ""

    kpm = _ProjectModelFallback()
else:
    PROJECT_MODEL_IMPORT_ERROR = False

try:
    from hermes_cli.discord_presentation import audit_discord_human_texts, render_discord_human_text  # type: ignore
except ImportError:  # pragma: no cover - deployed watcher may predate presentation helper
    def render_discord_human_text(text, metadata=None):
        return type("_Presentation", (), {
            "text": str(text or ""),
            "allowed": False,
            "reason": "presentation-model-unavailable",
        })()

    def audit_discord_human_texts(texts):
        return [
            {"index": i, "allowed": False, "reason": "presentation-model-unavailable", "rendered": ""}
            for i, _text in enumerate(texts)
        ]
try:
    from hermes_cli import kanban_discord_approvals as kap  # type: ignore
except ImportError:  # pragma: no cover - local deployment may predate approval helpers
    class _ApprovalFallback:
        @staticmethod
        def is_human_approval_gate(*_args, **_kwargs):
            return False

        @staticmethod
        def build_approval_request(*_args, **_kwargs):
            return {}

        @staticmethod
        def build_message_payload(*_args, **_kwargs):
            return {"content": ""}

    kap = _ApprovalFallback()

ENV_PATH = HOME / ".env"


def resolve_db_path() -> Path:
    raw = (os.getenv("KANBAN_DB_PATH") or "").strip()
    if not raw or raw.lower() == "default":
        return HOME / "kanban.db"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = HOME / path
    return path


def resolve_state_path() -> Path:
    raw = (os.getenv("KANBAN_DISCORD_LOG_STATE") or "").strip()
    if not raw:
        return HOME / "state" / "kanban_discord_log_state.json"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = HOME / path
    return path


DB_PATH = resolve_db_path()
STATE_PATH = resolve_state_path()
CHANNEL_NAME = os.getenv("DISCORD_KANBAN_LOG_CHANNEL_NAME", "kanban-log")
PROJECT_CHANNEL_NAME = os.getenv("DISCORD_KANBAN_PROJECT_LOG_CHANNEL_NAME", "kanban-project-logs")
RED_CHANNEL_NAME = os.getenv("DISCORD_RED_KANBAN_LOG_CHANNEL_NAME", "red-kanban-log")
RED_PROJECT_RUN_FORUM_ID = "1510287471504785428"
PRINTSMITH_UPDATES_FORUM_ID = "1516199012066132059"
PRINTSMITH_VISIBLE_CHANNEL_ID = "1516198206390796320"
RED_ASSIGNEES = {
    "red-antonetta",
    "red-scribe",
    "red-recon",
    "red-labops",
    "red-reporter",
    "red-reviewer",
    "red-exploitdev",
    "red-toolsmith",
}
SYNTHETIC_CREATED_BY = {"weak-handoff-replay", "sparse-handoff-robustness", "threshold-supplement"}
SYNTHETIC_ROOT_PREFIXES = ("root ",)
POLL_SECONDS = int(os.getenv("KANBAN_LOG_POLL_SECONDS", "5"))
THREAD_MODE = os.getenv("KANBAN_DISCORD_THREAD_MODE", "1").lower() not in {"0", "false", "no"}
THREAD_DEBUG = os.getenv("KANBAN_DISCORD_THREAD_DEBUG", "").lower() in {"1", "true", "yes"}
SUPPRESSION_LOG_PATH = Path(os.getenv("KANBAN_DISCORD_PM_LOG", str(HOME / "logs" / "kanban_discord_pm.log"))).expanduser()
PROJECT_HUB_BASE_URL = os.getenv("PROJECT_HUB_BASE_URL", "https://projecthub.mwnickerson.com").rstrip("/")
NOISE_KINDS = {"claimed", "spawned", "heartbeat"}
DANGER = {"blocked", "failed", "crashed", "timed_out", "spawn_failed", "gave_up"}
TERMINAL_STATUSES = {"done", "archived"}
MAX_EVENT_DELIVERY_ATTEMPTS = 3
# Channels that Discord accepts for ordinary message creation. Forum/media
# channels are deliberately excluded: they accept starter posts only through
# the thread creation endpoint below.
MESSAGEABLE_CHANNEL_TYPES = {0, 1, 3, 5, 10, 11, 12}
# A project narrative can start only from a text/announcement channel or a
# forum/media parent. Reject voice/category/stage channels before issuing an
# API call so a configuration mistake cannot create recurring delivery noise.
THREAD_PARENT_CHANNEL_TYPES = {0, 5, 15, 16}


class NonMessageableDiscordChannelError(RuntimeError):
    """Raised when a configured Discord target cannot accept this operation."""


PROJECT_WORDS = ("project", "orchestrat", "umbrella", "fan-in", "fanout", "fan-out", "milestone", "phase")
PRINTSMITH_TERMS = ("printsmith", "3d print operator", "cad", "blender", "stl", "3mf", "slicing", "printing")


def safe_event_ref(ev):
    return hashlib.sha256(f"{ev.get('id')}:{ev.get('task_id')}:{ev.get('kind')}".encode()).hexdigest()[:12]


def state_db_ref():
    """Return a stable, non-secret identity for state bound to the active DB."""
    try:
        stat_result = DB_PATH.stat()
        raw = f"{DB_PATH.resolve()}:{stat_result.st_dev}:{stat_result.st_ino}"
    except OSError:
        raw = str(DB_PATH.expanduser().resolve())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def state_event_bucket(state, key):
    """Return an active-DB bucket, migrating the legacy flat event-id map."""
    container = state.setdefault(key, {})
    if container and all(isinstance(value, dict) and "attempts" in value for value in container.values()):
        state[key] = {state_db_ref(): container}
        container = state[key]
    return container.setdefault(state_db_ref(), {})


def log_pm_decision(action, reason, ev, project_ctx=None):
    """Write structured local PM-boundary logs without raw payload content."""
    project_ctx = project_ctx or {}
    record = {
        "at": int(time.time()),
        "action": action,
        "reason": reason,
        "event_ref": safe_event_ref(ev),
        "kind": ev.get("kind"),
        "has_thread": bool(project_ctx.get("discord_thread_id")),
    }
    try:
        SUPPRESSION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SUPPRESSION_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def fetch_project_hub_context(slug, timeout=5):
    if not slug:
        return {}
    url = f"{PROJECT_HUB_BASE_URL}/api/projects/{slug}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hermes-kanban-discord-log/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    project = data.get("project") if isinstance(data.get("project"), dict) else data
    return {
        "title": project.get("title") or project.get("name"),
        "description": project.get("description") or project.get("purpose"),
        "purpose": project.get("purpose"),
        "phase": project.get("phase"),
        "status": project.get("status"),
        "next_step": project.get("next_step") or project.get("nextStep"),
    }



def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def discord_api(method, endpoint, body=None):
    token = os.environ["DISCORD_BOT_TOKEN"]
    cmd = [
        "curl", "-sS", "-X", method,
        "-H", f"Authorization: Bot {token}",
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)",
    ]
    if body is not None:
        cmd += ["--data", json.dumps(body)]
    cmd.append(f"https://discord.com/api/v10{endpoint}")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"Discord API curl failed for {method} {endpoint}: {proc.stderr.strip()}")
    raw = proc.stdout.strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if isinstance(data, dict) and "code" in data and "message" in data and str(data.get("code")) not in ("0",):
        raise RuntimeError(f"Discord API {method} {endpoint} returned error code {data.get('code')}: {data.get('message')}")
    return data


def _channel_type(channel):
    try:
        return int(channel.get("type"))
    except (AttributeError, TypeError, ValueError):
        return None


def require_messageable_channel(channel_id, dry_run=False):
    channel = get_channel(channel_id, dry_run=dry_run)
    if _channel_type(channel) not in MESSAGEABLE_CHANNEL_TYPES:
        raise NonMessageableDiscordChannelError("Discord delivery target is not messageable")
    return channel


def post(channel_id, content, dry_run=False, components=None):
    body = content if isinstance(content, dict) else {"content": content}
    if components is not None:
        body["components"] = components
    rendered = render_discord_human_text(body.get("content") or "", metadata={})
    body["content"] = rendered.text
    text = str(body.get("content") or "")
    if len(text) > 1900:
        body["content"] = text[:1850] + "\n…[truncated]"
    if dry_run:
        print(f"DRY-RUN post channel={channel_id}:\n{body.get('content', '')}\ncomponents={json.dumps(body.get('components', []), ensure_ascii=False)}\n---")
        return {"id": f"dry-msg-{int(time.time() * 1000)}", "channel_id": channel_id}
    require_messageable_channel(channel_id)
    return discord_api("POST", f"/channels/{channel_id}/messages", body)


_CHANNEL_CACHE = {}


def get_channel(channel_id, dry_run=False):
    if dry_run:
        return {"id": channel_id, "type": 0, "name": f"dry-{channel_id}"}
    if channel_id not in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id] = discord_api("GET", f"/channels/{channel_id}")
    return _CHANNEL_CACHE[channel_id]


def create_thread(parent_channel_id, name, message, dry_run=False, applied_tags=None):
    parent = get_channel(parent_channel_id, dry_run=dry_run)
    parent_type = _channel_type(parent)
    if parent_type not in THREAD_PARENT_CHANNEL_TYPES:
        raise NonMessageableDiscordChannelError("Discord project parent is not a supported thread channel")
    rendered = render_discord_human_text(message, metadata={})
    message = rendered.text
    body = {"name": name[:90], "auto_archive_duration": 10080, "message": {"content": message}}
    # Text channels need an explicit public-thread type. Forum/media channels
    # create posts via the same endpoint but reject/ignore the text-thread type;
    # the created post's thread id is still returned as `id` and can be reused
    # by the project_threads state map.
    if parent_type not in {15, 16}:  # GUILD_FORUM / GUILD_MEDIA
        body["type"] = 11
    elif applied_tags:
        body["applied_tags"] = list(applied_tags)
    if dry_run:
        digest = hashlib.sha256(f"{parent_channel_id}\n{name}".encode()).hexdigest()[:12]
        thread_id = f"dry-thread-{parent_channel_id}-{digest}"
        surface = "forum-post" if parent_type in {15, 16} else "thread"
        print(f"DRY-RUN create_{surface} parent={parent_channel_id} name={name}:\n{message}\n---")
        return {"id": thread_id, "parent_id": parent_channel_id, "name": name}
    return discord_api("POST", f"/channels/{parent_channel_id}/threads", body)


def archive_thread(thread_id, dry_run=False):
    """Archive a Discord thread without deleting any message history."""
    if dry_run:
        print(f"DRY-RUN archive_thread thread={thread_id}")
        return {"id": thread_id, "archived": True}
    return discord_api("PATCH", f"/channels/{thread_id}", {"archived": True})


def post_redirect_once(thread_id, content, nonce, dry_run=False):
    """Post a redirect with Discord-enforced nonce idempotency."""
    rendered = render_discord_human_text(content, metadata={})
    content = rendered.text
    body = {"content": content, "nonce": str(nonce), "enforce_nonce": True}
    if dry_run:
        print(f"DRY-RUN redirect_once channel={thread_id} nonce={nonce}:\n{content}\n---")
        return {"id": f"dry-redirect-{nonce}", "channel_id": thread_id}
    return discord_api("POST", f"/channels/{thread_id}/messages", body)


def find_named_thread(parent_channel_id, name, dry_run=False):
    """Find an active or archived forum thread by exact parent/name."""
    if dry_run:
        return None
    parent = get_channel(parent_channel_id)
    guild_id = parent.get("guild_id")
    candidates = []
    if guild_id:
        candidates.extend(discord_api("GET", f"/guilds/{guild_id}/threads/active").get("threads", []))
    candidates.extend(discord_api("GET", f"/channels/{parent_channel_id}/threads/archived/public").get("threads", []))
    matches = [t for t in candidates if str(t.get("parent_id")) == str(parent_channel_id) and t.get("name") == name]
    if len(matches) > 1:
        raise RuntimeError(f"multiple Printsmith migration threads named {name!r}; refusing to guess")
    return matches[0] if matches else None


def ensure_channel():
    return ensure_named_channel(
        configured=os.getenv("DISCORD_KANBAN_LOG_CHANNEL") or os.getenv("DISCORD_KANBAN_LOG_CHANNEL_ID"),
        name=CHANNEL_NAME,
        topic="Hermes Kanban lifecycle log: project thread milestones plus compact one-off events.",
        required_label="DISCORD_KANBAN_LOG_CHANNEL",
    )


def ensure_red_channel(default_channel_id):
    configured = (
        os.getenv("DISCORD_RED_KANBAN_PROJECT_RUNS_CHANNEL")
        or os.getenv("DISCORD_RED_KANBAN_PROJECT_RUNS_CHANNEL_ID")
        or os.getenv("DISCORD_RED_KANBAN_LOG_CHANNEL")
        or os.getenv("DISCORD_RED_KANBAN_LOG_CHANNEL_ID")
        or RED_PROJECT_RUN_FORUM_ID
    )
    if not configured:
        return default_channel_id
    return ensure_named_channel(
        configured=configured,
        name=RED_CHANNEL_NAME,
        topic="Hermes Red-lane Kanban lifecycle log. Scope-sensitive red project milestones only.",
        required_label="DISCORD_RED_KANBAN_LOG_CHANNEL",
    )


def ensure_project_channel(default_channel_id):
    configured = os.getenv("DISCORD_KANBAN_PROJECT_LOG_CHANNEL") or os.getenv("DISCORD_KANBAN_PROJECT_LOG_CHANNEL_ID")
    if configured:
        return configured
    return ensure_named_channel(
        configured=None,
        name=PROJECT_CHANNEL_NAME,
        topic="Project-scoped Hermes Kanban logs. One thread per umbrella project; completion pings happen inside the project thread.",
        required_label="DISCORD_KANBAN_PROJECT_LOG_CHANNEL",
    )


def ensure_named_channel(configured=None, name=None, topic=None, required_label="DISCORD_KANBAN_LOG_CHANNEL"):
    if configured:
        return configured
    home_channel = os.environ.get("DISCORD_HOME_CHANNEL") or os.environ.get("DISCORD_SYSTEM_CHANNEL")
    if not home_channel:
        raise RuntimeError(f"Need DISCORD_HOME_CHANNEL or {required_label} in ~/.hermes/.env")
    home = discord_api("GET", f"/channels/{home_channel}")
    guild_id = home.get("guild_id")
    if not guild_id:
        raise RuntimeError("Configured Discord home channel is not a guild channel; cannot infer guild")
    channels = discord_api("GET", f"/guilds/{guild_id}/channels")
    for ch in channels:
        if ch.get("name") == name and ch.get("type") in {0, 15, 16}:
            return ch["id"]
    created = discord_api("POST", f"/guilds/{guild_id}/channels", {"name": name, "type": 0, "topic": topic})
    return created["id"]


def migrate_project_thread_state(state):
    """Invalidate stale slug-only forum thread mappings for run-keyed lanes."""
    threads = state.get("project_threads")
    if not isinstance(threads, dict):
        return state
    raw = os.getenv("KANBAN_DISCORD_RUN_KEYED_PROJECT_SLUGS", "build-implementation-lane,review-e2e-build-lane")
    run_keyed_slugs = {s.strip() for s in raw.replace(" ", ",").split(",") if s.strip()}
    migrated = state.setdefault("project_threads_migrated", {})
    for slug in sorted(run_keyed_slugs):
        if slug in threads:
            migrated.setdefault(slug, threads.pop(slug))
    return state


def _has_term(haystack, term):
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack))


def _normalized_route_text(values):
    return "\n".join(str(value) for value in values if value).lower().replace("_", " ").replace("-", " ")


def is_printsmith_task(task, project_ctx=None):
    """Match Printsmith terms in task/project titles or stable identifiers.

    Task bodies are intentionally excluded so incidental prose cannot move a
    project. Red/non-red precedence is enforced by the channel chooser.
    """
    project_ctx = project_ctx or {}
    route_text = _normalized_route_text((
        task.get("title"), task.get("id"),
        project_ctx.get("project_title"), project_ctx.get("project_hub_slug"),
        project_ctx.get("project_id"), project_ctx.get("root_task_id"),
    ))
    return any(_has_term(route_text, term) for term in PRINTSMITH_TERMS)


def choose_project_parent_channel(task, project_ctx, general_channel_id, red_channel_id, printsmith_channel_id):
    # Red routing is scope-sensitive and always wins over domain routing.
    if is_red_task(task):
        return red_channel_id
    if is_printsmith_task(task, project_ctx):
        return printsmith_channel_id
    return general_channel_id


def migrate_printsmith_component_state(state, printsmith_channel_id):
    """Compatibility no-op: migration is intentionally explicit, never load-time."""
    return state


def migrate_printsmith_state(state, component_id, printsmith_channel_id, dry_run=False, save_fn=None):
    """Checkpoint an idempotent Printsmith migration without deleting history."""
    save_fn = save_fn or save_state
    component = state.setdefault("components", {}).get(component_id)
    if not isinstance(component, dict):
        raise RuntimeError(f"component {component_id} is absent from state")
    synthetic = {"id": component_id, "title": component.get("title", ""), "body": "", "assignee": ""}
    if not is_printsmith_task(synthetic):
        raise RuntimeError(f"component {component_id} is not narrowly classified as Printsmith")
    record = state.setdefault("printsmith_migrations", {}).setdefault(component_id, {
        "old_channel_id": component.get("channel_id"),
        "old_thread_id": component.get("thread_id"),
        "completed_ping_sent": bool(component.get("completed_ping_sent")),
        "project_thread_keys": sorted(k for k, v in state.get("project_threads", {}).items() if v == component.get("thread_id")),
        "redirect_nonce": int(hashlib.sha256(f"printsmith-redirect:{component_id}".encode()).hexdigest()[:15], 16),
    })
    old_thread_id = record.get("old_thread_id")
    thread_name = str(component.get("title") or f"Printsmith {component_id}")[:90]
    if not record.get("new_thread_id"):
        existing = find_named_thread(printsmith_channel_id, thread_name, dry_run=dry_run)
        thread = existing or create_thread(printsmith_channel_id, thread_name, f"Printsmith project migrated from `{old_thread_id}`. History remains in the archived source thread.", dry_run=dry_run)
        record["new_thread_id"] = thread["id"]
        record["thread_reused"] = bool(existing)
        if not dry_run:
            save_fn(state)
    new_thread_id = record["new_thread_id"]
    if old_thread_id and not record.get("redirect_posted"):
        post_redirect_once(old_thread_id, f"This project moved to <#{new_thread_id}>. Existing history is preserved here.", record["redirect_nonce"], dry_run=dry_run)
        record["redirect_posted"] = True
        if not dry_run:
            save_fn(state)
    if old_thread_id and record.get("redirect_posted") and not record.get("old_thread_archived"):
        archive_thread(old_thread_id, dry_run=dry_run)
        record["old_thread_archived"] = True
        if not dry_run:
            save_fn(state)
    component["channel_id"] = printsmith_channel_id
    component["thread_id"] = new_thread_id
    component["completed_ping_sent"] = record["completed_ping_sent"]
    project_threads = state.setdefault("project_threads", {})
    for key in record["project_thread_keys"]:
        if project_threads.get(key) == old_thread_id:
            project_threads[key] = new_thread_id
    record["state_applied"] = True
    if not dry_run:
        save_fn(state)
    return record


def load_state():
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
            state.setdefault("components", {})
            state.setdefault("task_aliases", {})
            state.setdefault("red_thread_posts", {})
            state.setdefault("delivery_failures", {})
            state.setdefault("project_thread_posts", {})
            migrate_project_thread_state(state)
            return state
        except Exception:
            pass
    return {
        "last_event_id": 0,
        "components": {},
        "task_aliases": {},
        "red_thread_posts": {},
        "delivery_failures": {},
        "project_thread_posts": {},
    }


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def parse_payload(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def parse_task_metadata(payload):
    metadata = payload.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def extract_thread_id(task, payload):
    metadata = parse_task_metadata(payload)
    for key in ("discord_thread_id", "thread_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    body = task.get("body") or ""
    import re
    for pattern in (
        r"Discord thread id:\s*([0-9]{6,})",
        r"Discord thread:\s*`?([0-9]{6,})`?",
    ):
        m = re.search(pattern, body, flags=re.I)
        if m:
            return m.group(1)
    return None


def summarize_findings(metadata):
    findings = metadata.get("findings") or metadata.get("decisions") or metadata.get("outputs") or []
    if isinstance(findings, str):
        findings = [findings]
    if not isinstance(findings, list):
        return []
    out = []
    for item in findings:
        text = str(item).strip()
        if text:
            out.append(text.rstrip('.'))
        if len(out) >= 3:
            break
    return out


def suggest_next_action(task, payload):
    metadata = parse_task_metadata(payload)
    for key in ("recommended_next_skill", "next_wave_priority", "follow_on_branch_executed"):
        value = metadata.get(key)
        if value:
            return str(value).strip()
    summary = payload.get("summary") or payload.get("result") or ""
    if "queued successor orchestrator" in summary.lower():
        return "successor orchestrator queued to fan out the next wave"
    return f"review downstream handoff from `{task.get('id')}` and use it for the next justified branch"


def format_red_thread_update(task, ev, payload):
    metadata = parse_task_metadata(payload)
    title = (task.get("title") or task.get("id") or "task").strip()
    assignee = (task.get("assignee") or payload.get("assignee") or "unknown").strip()
    kind = ev["kind"]
    lines = [f"🧠 **Worker update:** {title}", f"👤 **Owner:** `{assignee}`"]
    if kind == "created":
        parents = payload.get("parents") or []
        if isinstance(parents, str):
            parents = [parents]
        lines.append("🚦 **Stage status:** queued")
        if parents:
            lines.append(f"🔗 **Depends on:** {', '.join(str(p) for p in parents[:4])}")
        lines.append("📌 **Why it exists:** new branch/stage created and waiting to start")
    elif kind == "claimed":
        lines.append("▶️ **Stage status:** started")
        run_id = payload.get("run_id")
        if run_id:
            lines.append(f"🆔 **Run id:** `{run_id}`")
        lines.append("⚙️ **What is happening:** worker claimed the branch and execution is underway")
    elif kind == "completed":
        summary = (payload.get("summary") or payload.get("result") or "completed").strip()
        lines.append("✅ **Stage status:** finished")
        lines.append(f"📝 **Summary:** {summary}")
        findings = summarize_findings(metadata)
        if findings:
            lines.append("🔍 **Findings:**")
            for item in findings:
                lines.append(f"- {item}")
        lines.append(f"➡️ **Suggested next action:** {suggest_next_action(task, payload)}")
    elif kind == "blocked":
        reason = payload.get("reason") or payload.get("summary") or payload.get("error") or "blocked"
        lines.append("⛔ **Stage status:** blocked")
        lines.append(f"🚧 **Blocked:** {reason}")
        lines.append("➡️ **Suggested next action:** resolve the blocker or route the missing reviewer/fan-in step, then resume the workflow")
    else:
        return None
    return "\n".join(lines)[:1800]


def inherited_component_thread_id(con, task_id):
    for linked_task in component_tasks(con, task_id):
        thread_id = extract_thread_id(linked_task, {})
        if thread_id:
            return thread_id
    return None


def maybe_post_red_thread_update(con, state, task, ev, payload, dry_run=False):
    if ev["kind"] not in {"created", "claimed", "completed", "blocked"}:
        return
    if not is_red_task(task):
        return
    thread_id = extract_thread_id(task, payload) or inherited_component_thread_id(con, ev["task_id"])
    if not thread_id:
        return
    posted = state.setdefault("red_thread_posts", {})
    event_key = str(ev["id"])
    if posted.get(event_key):
        return
    body = format_red_thread_update(task, ev, payload)
    if not body:
        return
    post(thread_id, body, dry_run=dry_run)
    posted[event_key] = {"thread_id": thread_id, "task_id": ev["task_id"], "kind": ev["kind"]}


def red_thread_target(con, task, payload, task_id):
    if not is_red_task(task):
        return None
    return extract_thread_id(task, payload) or inherited_component_thread_id(con, task_id)


def task_lookup(con, task_id):
    con.row_factory = sqlite3.Row
    row = con.execute("select id,title,body,assignee,status,priority,created_at,started_at,completed_at,current_run_id,created_by from tasks where id=?", (task_id,)).fetchone()
    return dict(row) if row else {"id": task_id, "title": "unknown", "body": "", "assignee": "unknown", "status": "unknown", "created_at": 0, "priority": 0, "created_by": ""}


def is_synthetic_test_task(task):
    created_by = (task.get("created_by") or "").strip()
    title = (task.get("title") or "").strip().lower()
    if created_by in SYNTHETIC_CREATED_BY:
        return True
    return any(title.startswith(prefix) for prefix in SYNTHETIC_ROOT_PREFIXES)


def is_red_task(task):
    assignee = (task.get("assignee") or "").strip()
    return assignee in RED_ASSIGNEES or assignee.startswith("red-")


def component_task_ids(con, task_id):
    seen = {task_id}
    q = deque([task_id])
    while q:
        cur = q.popleft()
        rows = con.execute("select parent_id, child_id from task_links where parent_id=? or child_id=?", (cur, cur)).fetchall()
        for row in rows:
            parent_id = row["parent_id"] if isinstance(row, sqlite3.Row) else row[0]
            child_id = row["child_id"] if isinstance(row, sqlite3.Row) else row[1]
            for nxt in (parent_id, child_id):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
    return seen


def component_tasks(con, task_id):
    ids = sorted(component_task_ids(con, task_id))
    return [task_lookup(con, tid) for tid in ids]


def component_root(con, tasks):
    ids = {t["id"] for t in tasks}
    child_rows = con.execute("select child_id from task_links where child_id in (%s)" % ",".join("?" for _ in ids), tuple(ids)).fetchall() if ids else []
    children = {r["child_id"] if isinstance(r, sqlite3.Row) else r[0] for r in child_rows}
    roots = [t for t in tasks if t["id"] not in children] or tasks
    roots.sort(key=lambda t: (-(t.get("priority") or 0), t.get("created_at") or 0, t.get("id") or ""))
    return roots[0]


def project_trigger(task, payload, component_size):
    if kpm.extract_task_project_metadata(task, payload):
        return True
    if component_size > 1:
        return True
    metadata = payload.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if payload.get("project_completion") or metadata.get("project_completion"):
        return True
    haystack = f"{task.get('title') or ''}\n{task.get('body') or ''}".lower()
    return any(word in haystack for word in PROJECT_WORDS)


def thread_title(root, tasks):
    # New project-thread model: Discord thread name matches the Project Hub
    # title exactly when project metadata is present. Legacy untagged project
    # graphs still get a `kanban:` prefix so they remain obviously mechanical.
    meta = kpm.extract_task_project_metadata(root)
    title = (meta.get("project_title") or root.get("title") or root.get("id") or "Kanban project").strip()
    if meta.get("project_hub_slug"):
        return title[:90]
    return f"kanban: {title}"[:90]


def choose_component_channel(tasks, general_channel_id, red_channel_id, printsmith_channel_id):
    if any(is_red_task(t) for t in tasks):
        return red_channel_id
    if any(is_printsmith_task(t) for t in tasks):
        return printsmith_channel_id
    return general_channel_id


def refresh_component_state(con, state, root, tasks, channel_id):
    root_id = root["id"]
    comp = state.setdefault("components", {}).setdefault(root_id, {})
    comp.setdefault("thread_id", None)
    comp["channel_id"] = channel_id
    comp["title"] = thread_title(root, tasks)
    comp["task_ids"] = sorted(t["id"] for t in tasks)
    comp.setdefault("status", "open")
    comp.setdefault("completed_ping_sent", False)
    for tid in comp["task_ids"]:
        state.setdefault("task_aliases", {})[tid] = root_id
    return comp


def project_terminal_state(tasks):
    active = [t for t in tasks if t.get("status") != "archived"]
    if active and all(t.get("status") == "done" for t in active):
        return "completed"
    if any(t.get("status") == "blocked" for t in active):
        return "blocked"
    if any(t.get("status") in {"failed", "crashed", "timed_out", "spawn_failed"} for t in active):
        return "failed"
    return None


def explicit_project_completion(payload):
    metadata = payload.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    return bool(payload.get("project_completion") or metadata.get("project_completion"))


def allowed_user_mentions():
    users = [u.strip() for u in os.environ.get("DISCORD_ALLOWED_USERS", "").replace(",", " ").split() if u.strip()]
    return " ".join(f"<@{u}>" for u in users)


def maybe_post_human_approval(task, ev, payload, target_channel_id, project_ctx=None, dry_run=False):
    if not kap.is_human_approval_gate(task, ev["kind"], payload):
        return False
    req = kap.build_approval_request(task, {**payload, "run_id": ev.get("run_id")}, project_ctx)
    body = kap.build_message_payload(req, allowed_user_mentions())
    post(target_channel_id, body, dry_run=dry_run)
    return True


def recent_task_failures(con, task_id):
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        select outcome, status, error, summary, profile, started_at, ended_at
        from task_runs
        where task_id=?
        order by started_at desc
        limit 5
        """,
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recovery_suggestions(task, kind, payload, failures):
    title = (task.get("title") or "").lower()
    assignee = task.get("assignee") or "unknown"
    outcomes = [f.get("outcome") or f.get("status") for f in failures if f.get("outcome") or f.get("status")]
    repeated = len([o for o in outcomes if o in {"crashed", "timed_out", "spawn_failed", "failed", "blocked"}]) >= 2
    gave_up = kind == "gave_up" or payload.get("trigger_outcome") or payload.get("failures")
    if not (repeated or gave_up or kind in {"blocked", "spawn_failed"}):
        return []
    suggestions = []
    if kind == "blocked":
        suggestions.extend([
            "answer the blocker in-thread, then unblock/requeue the same task",
            "create a smaller recovery task with the blocked task id in context",
            "if the blocker is a missing decision/credential, resolve that explicitly before retrying",
        ])
    elif "spawn_failed" in outcomes or kind == "spawn_failed":
        suggestions.extend([
            f"smoke-test profile `{assignee}` directly (`hermes --profile {assignee} chat -Q -q ...`)",
            "check profile auth/config/toolsets and missing skill names",
            "after profile is healthy, reset/requeue the task instead of cloning blindly",
        ])
    elif "timed_out" in outcomes:
        suggestions.extend([
            "split the work into a smaller recovery task with tighter acceptance criteria",
            "raise max_runtime_seconds only if progress/heartbeat evidence shows it was genuinely still working",
            "ask the worker to write partial artifacts early so retries can resume instead of restart",
        ])
    elif "crashed" in outcomes or kind == "crashed" or kind == "gave_up":
        suggestions.extend([
            f"smoke-test profile `{assignee}` outside Kanban to separate profile crash from task logic",
            "inspect the latest run/gateway logs for the first traceback or missing dependency",
            "create a bounded recovery task carrying this task id, prior errors, and any parent handoff",
        ])
    else:
        suggestions.extend([
            "inspect task comments/runs before retrying",
            "make a smaller recovery task if the same failure repeats",
            "route to a more appropriate specialist if the assignee is mismatched",
        ])
    if any(word in title for word in ("discord", "gateway", "kanban-log", "watcher")):
        suggestions.append("for Discord/gateway work, verify env channel ids and send a test message before requeueing")
    out = []
    for s in suggestions:
        if s not in out:
            out.append(s)
    return out[:4]


def format_event(con, ev, compact=False):
    task = task_lookup(con, ev["task_id"])
    payload = parse_payload(ev.get("payload"))
    kind = ev["kind"]
    title = task.get("title") or "untitled"
    assignee = task.get("assignee") or "unassigned"
    status = task.get("status") or "unknown"
    failures = recent_task_failures(con, ev["task_id"]) if (kind in DANGER or payload.get("outcome") in DANGER) else []
    suggestions = recovery_suggestions(task, kind, payload, failures)
    prefix = "Kanban" if not compact else "•"
    lines = [
        f"{prefix} {kind}: {title}",
        f"task: `{ev['task_id']}` | assignee: `{assignee}` | status: `{status}` | run: `{ev.get('run_id') or '-'}`",
    ]
    if kind == "created":
        parents = payload.get("parents") or []
        lines.append(f"created for `{payload.get('assignee', assignee)}`; parents: {', '.join(parents) if parents else 'none'}")
    elif kind == "claimed":
        lines.append(f"claimed by `{payload.get('lock', 'unknown')}`")
    elif kind == "spawned":
        lines.append(f"spawned pid `{payload.get('pid', 'unknown')}`")
    elif kind == "completed":
        summary = payload.get("summary") or payload.get("result") or "completed"
        lines.append(f"summary: {summary}")
    elif kind == "commented":
        body = payload.get("body") or payload.get("comment") or payload.get("summary") or "comment added"
        author = payload.get("author") or "unknown"
        lines.append(f"comment by `{author}`: {str(body)[:700]}")
    elif kind == "blocked":
        reason = payload.get("reason") or payload.get("summary") or payload.get("error") or "blocked"
        lines.append(f"action needed: {reason}")
    elif kind in DANGER:
        lines.append(f"issue: {payload.get('error') or payload.get('reason') or payload.get('outcome') or json.dumps(payload)[:400]}")
    else:
        small = {k: v for k, v in payload.items() if k in ("status", "outcome", "summary", "error", "reason")}
        if small:
            lines.append("details: " + json.dumps(small, ensure_ascii=False)[:700])
    if suggestions:
        lines.append("suggested next moves:")
        for idx, suggestion in enumerate(suggestions, 1):
            lines.append(f"{idx}. {suggestion}")
    return "\n".join(lines)


def should_skip_thread_event(kind):
    return kind in NOISE_KINDS and not THREAD_DEBUG


def route_event(con, state, ev, channel_id, project_channel_id, red_channel_id, dry_run=False):
    task = task_lookup(con, ev["task_id"])
    if is_synthetic_test_task(task):
        return "skipped-synthetic-test-task"
    payload = parse_payload(ev.get("payload"))
    if ev["kind"] == "archived" or payload.get("discord_silent"):
        return "skipped-silent-archive"
    project_ctx = kpm.resolve_project_context(con, ev["task_id"], payload)
    if PROJECT_MODEL_IMPORT_ERROR:
        log_pm_decision("suppressed", "suppressed-project-model-unavailable", ev, {})
        return "skipped-project-model-unavailable"
    if project_ctx:
        thread_key = kpm.project_thread_key(project_ctx)
        should_post_project_event = kpm.should_post_project_thread_event(ev["kind"], payload)
        project_hub = fetch_project_hub_context(project_ctx.get("project_hub_slug"))
        pm_update = kpm.render_project_pm_update(task, ev["kind"], payload, project_ctx, project_hub)
        if not pm_update.should_post:
            log_pm_decision("suppressed", pm_update.suppression_reason, ev, project_ctx)
            should_post_project_event = False
        thread_id = project_ctx.get("discord_thread_id") or state.setdefault("project_threads", {}).get(thread_key)
        if not thread_id and not should_post_project_event:
            return "skipped-project-noise"
        if not thread_id:
            # Discord requires a starter message for public-thread creation.
            # Keep it minimal; all real progress stays in the thread.
            project_parent_channel_id = choose_project_parent_channel(
                task, project_ctx, project_channel_id, red_channel_id, PRINTSMITH_UPDATES_FORUM_ID
            )
            thread = create_thread(
                project_parent_channel_id,
                kpm.safe_project_title(project_hub, project_ctx),
                kpm.format_project_thread_starter({**project_ctx, **project_hub}),
                dry_run=dry_run,
            )
            thread_id = thread["id"]
            state.setdefault("project_threads", {})[thread_key] = thread_id
        if should_post_project_event:
            receipts = state_event_bucket(state, "project_thread_posts")
            receipt_key = str(ev["id"])
            if receipt_key in receipts:
                log_pm_decision("suppressed", "duplicate-project-event-retry", ev, project_ctx)
            else:
                message = post(thread_id, pm_update.message, dry_run=dry_run) or {}
                receipts[receipt_key] = {
                    "thread_id": str(thread_id),
                    "message_id": str(message.get("id", "")),
                    "posted_at": int(time.time()),
                }
                if not dry_run:
                    save_state(state)
                log_pm_decision("posted", "pm-rendered", ev, project_ctx)
        if maybe_post_human_approval(task, ev, payload, thread_id, project_ctx, dry_run=dry_run):
            return "posted-human-approval"
        return "posted-project-thread" if should_post_project_event else "skipped-project-noise"

    if maybe_post_human_approval(task, ev, payload, channel_id, None, dry_run=dry_run):
        return "posted-human-approval"

    tasks = component_tasks(con, ev["task_id"])
    root = component_root(con, tasks)
    target_channel_id = choose_component_channel(tasks, channel_id, red_channel_id, PRINTSMITH_UPDATES_FORUM_ID)
    is_project_event = THREAD_MODE and project_trigger(root if root else task, payload, len(tasks))
    if is_project_event:
        log_pm_decision("suppressed", "suppressed-project-missing-explicit-metadata", ev, {})
        return "skipped-project-missing-explicit-metadata"

    maybe_post_red_thread_update(con, state, task, ev, payload, dry_run=dry_run)
    if red_thread_target(con, task, payload, ev["task_id"]):
        return "posted-red-thread"
    if not is_project_event:
        if ev["kind"] in NOISE_KINDS and not THREAD_DEBUG:
            return "skipped-flat-noise"
        post(target_channel_id, format_event(con, ev), dry_run=dry_run)
        return "posted-flat"


def run_once(state, channel_id, project_channel_id, red_channel_id, dry_run=False, limit=50):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select id, task_id, run_id, kind, payload, created_at from task_events where id > ? order by id asc limit ?",
            (int(state.get("last_event_id", 0)), limit),
        ).fetchall()
        failures = state_event_bucket(state, "delivery_failures")
        for row in rows:
            ev = dict(row)
            event_key = str(ev["id"])
            try:
                route_event(con, state, ev, channel_id, project_channel_id, red_channel_id, dry_run=dry_run)
            except NonMessageableDiscordChannelError as exc:
                receipt = failures.setdefault(event_key, {"attempts": 0})
                receipt["attempts"] = int(receipt.get("attempts", 0)) + 1
                receipt["error_class"] = type(exc).__name__
                receipt["outcome"] = "suppressed-invalid-discord-channel-type"
                receipt["updated_at"] = int(time.time())
                log_pm_decision("suppressed", "invalid-discord-channel-type", ev, {})
                print(
                    f"post suppressed event_ref={safe_event_ref(ev)} "
                    "reason=invalid-discord-channel-type",
                    file=sys.stderr,
                    flush=True,
                )
                state["last_event_id"] = ev["id"]
                if not dry_run:
                    save_state(state)
                continue
            except Exception as exc:
                receipt = failures.setdefault(event_key, {"attempts": 0})
                receipt["attempts"] = int(receipt.get("attempts", 0)) + 1
                receipt["error_class"] = type(exc).__name__
                receipt["updated_at"] = int(time.time())
                log_pm_decision("retry", "delivery-failed", ev, {})
                print(
                    f"post retry event_ref={safe_event_ref(ev)} "
                    f"attempt={receipt['attempts']}/{MAX_EVENT_DELIVERY_ATTEMPTS} "
                    f"error_class={receipt['error_class']}",
                    file=sys.stderr,
                    flush=True,
                )
                if receipt["attempts"] < MAX_EVENT_DELIVERY_ATTEMPTS:
                    if not dry_run:
                        save_state(state)
                    break
                receipt["outcome"] = "exhausted"
                log_pm_decision("suppressed", "delivery-retries-exhausted", ev, {})
                state["last_event_id"] = ev["id"]
                if not dry_run:
                    save_state(state)
                continue
            failures.pop(event_key, None)
            state["last_event_id"] = ev["id"]
            if not dry_run:
                save_state(state)
        return len(rows)
    finally:
        con.close()


def initialize_state(state, channel_id, dry_run=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if not state.get("last_event_id"):
        max_id = con.execute("select coalesce(max(id),0) from task_events").fetchone()[0]
        state["last_event_id"] = max_id
        if not dry_run:
            save_state(state)
        post(channel_id, f"Kanban log online. Watching `{DB_PATH}` from event id `{max_id}`. Project graphs will use Discord threads; one-off tasks remain compact.", dry_run=dry_run)


def build_smoke_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table tasks (id text primary key, title text not null, body text, assignee text, status text not null, priority integer default 0, created_by text, created_at integer not null, started_at integer, completed_at integer, workspace_kind text default 'scratch', workspace_path text, claim_lock text, claim_expires integer, tenant text, result text, idempotency_key text, spawn_failures integer default 0, worker_pid integer, last_spawn_error text, max_runtime_seconds integer, last_heartbeat_at integer, current_run_id integer, workflow_template_id text, current_step_key text, skills text, consecutive_failures integer default 0, last_failure_error text, max_retries integer);
        create table task_links (parent_id text not null, child_id text not null, primary key (parent_id, child_id));
        create table task_events (id integer primary key, task_id text not null, run_id integer, kind text not null, payload text, created_at integer not null);
        create table task_runs (id integer primary key, task_id text not null, profile text, step_key text, status text not null, claim_lock text, claim_expires integer, worker_pid integer, max_runtime_seconds integer, last_heartbeat_at integer, started_at integer not null, ended_at integer, outcome text, summary text, metadata text, error text);
        """
    )
    rows = [
        ("p1", "Demo umbrella project", "orchestration project", "antonetta", "done", 10, 1),
        ("c1", "Demo worker", "", "forge", "done", 0, 2),
        ("c2", "Demo red worker", "Discord thread id: 777777", "red-recon", "done", 0, 3),
        ("r1", "Standalone red thread task", "Discord thread id: 888888", "red-antonetta", "blocked", 0, 4),
        ("one", "Small one-off", "", "forge", "done", 0, 5),
        ("rev", "Autonomous review gate", "", "forge", "blocked", 0, 6),
        ("hum", "Human approval gate", "", "forge", "blocked", 0, 7),
    ]
    con.executemany("insert into tasks (id,title,body,assignee,status,priority,created_at) values (?,?,?,?,?,?,?)", rows)
    con.executemany("insert into task_links (parent_id, child_id) values (?,?)", [("p1", "c1"), ("p1", "c2")])
    events = [
        (1, "p1", None, "created", json.dumps({"assignee": "antonetta", "parents": []}), 1),
        (2, "c1", 7, "spawned", json.dumps({"pid": 123}), 2),
        (3, "c1", 7, "completed", json.dumps({"summary": "worker done"}), 3),
        (4, "c2", 8, "created", json.dumps({"assignee": "red-recon", "parents": ["p1"]}), 4),
        (5, "c2", 8, "claimed", json.dumps({"run_id": 8}), 5),
        (6, "c2", 8, "completed", json.dumps({"summary": "red worker done", "metadata": {"findings": ["validated Discord lifecycle formatting"]}}), 6),
        (7, "r1", 9, "created", json.dumps({"assignee": "red-antonetta", "parents": []}), 7),
        (8, "r1", 9, "claimed", json.dumps({"run_id": 9}), 8),
        (9, "r1", 9, "blocked", json.dumps({"reason": "need reviewer signoff"}), 9),
        (10, "p1", 10, "completed", json.dumps({"summary": "project done", "metadata": {"project_completion": True}}), 10),
        (11, "one", 11, "completed", json.dumps({"summary": "one-off done"}), 11),
        (12, "rev", 12, "blocked", json.dumps({"reason": "review-required: implementation handoff", "metadata": {"review_required": True}}), 12),
        (13, "hum", 13, "blocked", json.dumps({"reason": "human-gate: approve live decision", "metadata": {"human_approval_required": True, "what_is_approved": "continuing past the human gate", "if_approved": "the task unblocks", "risk_rollback": "deny keeps it blocked"}}), 13),
    ]
    con.executemany("insert into task_events (id, task_id, run_id, kind, payload, created_at) values (?,?,?,?,?,?)", events)
    con.commit()
    con.close()


def pm_preview_fixtures():
    project = {
        "project_hub_slug": "human-readable-kanban-project-updates",
        "project_title": "Human-readable Kanban project updates",
        "project_status": "active",
        "stage_name": "Implementation",
        "phase": "Build",
        "next_step": "Review the implementation summary and requested decision.",
    }
    hub = {
        "title": "Human-readable Kanban project updates",
        "description": "Make Kanban project-run updates understandable as project management status.",
        "phase": "Build",
        "status": "active",
        "next_step": "Review the next PM-visible checkpoint.",
    }
    task = {"id": "t_fixture", "title": "Build PM presentation boundary", "status": "running", "body": "", "assignee": "forge"}
    return [
        ("kickoff", task, "project_kickoff", {"summary": "internal handoff", "metadata": {"public_summary": "Project run opened.","task_role": "Coordinates the watcher presentation work."}}, project, hub),
        ("research-completion", task, "completed", {"summary": "internal handoff", "metadata": {"public_summary": "Mapped the watcher route and identified the safe project boundary.", "user_visible_change": True, "task_role": "Research stage", "why_it_matters": "It shows where raw worker events can be suppressed without changing routing."}}, project, hub),
        ("implementation-awaiting-review", task, "completed", {"summary": "internal handoff", "metadata": {"public_summary": "Implemented deterministic rendering and tests.", "user_visible_change": True, "review_required": True, "review_request": "Review the PM output before deployment.", "task_role": "Implementation stage"}}, project, hub),
        ("changes-requested", task, "commented", {"body": "changes-requested: tighten final reconciliation wording", "metadata": {"changes_requested": True, "requested_change": "Tighten final reconciliation wording.", "task_role": "Review gate"}}, project, hub),
        ("blocker", task, "blocked", {"reason": "Project Hub context is unavailable; using local Kanban context only.", "metadata": {"blocker": "Project Hub context is unavailable.", "task_role": "Context assembly"}}, project, {}),
        ("approval-request", task, "commented", {"body": "approval required before copying to runtime", "metadata": {"human_approval_required": True, "approval_request": "Approve copying the checked-in watcher to the runtime path.", "task_role": "Deployment gate"}}, project, hub),
        ("final-completion", task, "project_final_summary", {"summary": "internal handoff", "project_completion": True, "metadata": {"public_summary": "All PM presentation checks passed.", "project_final_reconciliation": True, "why_it_matters": "The final update is now explicitly reconciled instead of inferred from a leaf task."}}, {**project, "project_status": "done"}, {**hub, "status": "done", "next_step": "No follow-up required."}),
        ("malformed-raw-worker-payload", task, "completed", {"summary": "{\"stdout\":\"/Users/anton/.hermes secret output\"}", "metadata": {"user_visible_change": True}}, project, hub),
        ("missing-project-hub-context", task, "completed", {"summary": "internal handoff", "metadata": {"public_summary": "Completed watcher-side rendering with local Kanban context.", "user_visible_change": True, "task_role": "Implementation stage"}}, project, {}),
    ]


def preview_fixtures():
    for name, task, kind, payload, project, hub in pm_preview_fixtures():
        rendered = kpm.render_project_pm_update(task, kind, payload, project, hub)
        print(f"--- {name} ---")
        if rendered.should_post:
            print(rendered.message)
        else:
            print(f"suppressed: {rendered.suppression_reason}")


def audit_recent_bot_message_fixtures():
    fixtures = [
        "Done. The Discord presentation boundary is now active.",
        "Here is the helper you asked for:\n```python\nprint('hello')\n```",
        '{"stdout": "/Users/anton/.hermes/raw-output", "returncode": 1}',
        "Traceback (most recent call last):\n  File \"/tmp/hermes.py\", line 1, in <module>",
        "task_id: t_abcdef123456\nrun_id: 42\nmetadata: {\"raw\": true}",
    ]
    print(json.dumps(audit_discord_human_texts(fixtures), indent=2, sort_keys=True))


def smoke_test():
    global DB_PATH, STATE_PATH
    old_db, old_state = DB_PATH, STATE_PATH
    with tempfile.TemporaryDirectory() as td:
        DB_PATH = Path(td) / "kanban.db"
        STATE_PATH = Path(td) / "state.json"
        build_smoke_db(DB_PATH)
        os.environ["DISCORD_ALLOWED_USERS"] = "12345"
        state = {"last_event_id": 0, "components": {}, "task_aliases": {}, "red_thread_posts": {}}
        count = run_once(state, "general-channel", "project-channel", "red-channel", dry_run=True, limit=20)
        assert count == 13, count
        assert state["components"] == {}, state
        assert state["task_aliases"] == {}, state
        assert "one" not in state["task_aliases"], state
        assert "r1" not in state["task_aliases"], state
        # The fail-closed presentation gate does not synthesize legacy red
        # project posts from machine-shaped fixtures.
        assert state["red_thread_posts"] == {}, state
        printsmith = {"id": "print", "title": "Slice Printsmith Blender STL", "body": "", "assignee": "forge"}
        modelsmith = {"id": "model", "title": "Modelsmith MLX local-model benchmark", "body": "", "assignee": "forge"}
        red_printsmith = {"id": "red-print", "title": "Printsmith STL", "body": "", "assignee": "red-recon"}
        body_only = {"id": "body-only", "title": "Review web-service diagnostics", "body": "Please review printing logs", "assignee": "forge"}
        unrelated_cad = {"id": "cad-policy", "title": "Review CAD policy for architecture docs", "body": "", "assignee": "forge"}
        unrelated_printing = {"id": "printing-logs", "title": "Investigate printing logs from web service", "body": "", "assignee": "forge"}
        contextual_cad = {"id": "cad-3d", "title": "Prepare CAD model for 3D printer", "body": "", "assignee": "forge"}
        assert is_printsmith_task(printsmith, {"project_hub_slug": "weekend-3mf-printing"})
        assert is_printsmith_task(contextual_cad)
        assert is_printsmith_task({"title": "Generic task"}, {"project_id": "cad"})
        assert not is_printsmith_task(body_only)
        assert is_printsmith_task(unrelated_cad)
        assert is_printsmith_task(unrelated_printing)
        assert not is_printsmith_task(modelsmith, {"project_hub_slug": "death-star-mlx"})
        assert choose_project_parent_channel(printsmith, {}, "generic-project", "red-project", "printsmith-updates") == "printsmith-updates"
        assert choose_project_parent_channel(red_printsmith, {}, "generic-project", "red-project", "printsmith-updates") == "red-project"
        migration_state = {
            "components": {
                "t_0d89a789": {
                    "channel_id": "1510003270771544227",
                    "thread_id": "1530231548199305400",
                    "title": "kanban: Finish Printsmith weekend test path: Blender design",
                    "completed_ping_sent": False,
                }
            }
        }
        migrate_printsmith_state(migration_state, "t_0d89a789", PRINTSMITH_UPDATES_FORUM_ID, dry_run=True)
        assert migration_state["components"]["t_0d89a789"]["completed_ping_sent"] is False
    DB_PATH, STATE_PATH = old_db, old_state
    print("smoke-test ok: legacy project fail-closed behavior, red/Printsmith routing, and state migration passed")


def main():
    parser = argparse.ArgumentParser(description="Mirror Hermes Kanban events to Discord with project-thread grouping.")
    parser.add_argument("--dry-run", action="store_true", help="print intended Discord writes instead of sending them")
    parser.add_argument("--once", action="store_true", help="process available events once and exit")
    parser.add_argument("--smoke-test", action="store_true", help="run deterministic local smoke test without Discord")
    parser.add_argument("--preview-fixtures", action="store_true", help="render deterministic PM fixture events without Discord")
    parser.add_argument("--audit-recent-bot-fixtures", action="store_true", help="dry-run Discord presentation audit for local bot-message fixtures")
    parser.add_argument("--migrate-printsmith-component", metavar="TASK_ID", help="explicitly migrate one leaked Printsmith component")
    args = parser.parse_args()
    if args.smoke_test:
        smoke_test()
        return
    if args.preview_fixtures:
        preview_fixtures()
        return
    if args.audit_recent_bot_fixtures:
        audit_recent_bot_message_fixtures()
        return

    load_env(ENV_PATH)
    if not args.dry_run and not os.environ.get("DISCORD_BOT_TOKEN"):
        raise RuntimeError("Missing DISCORD_BOT_TOKEN")
    channel_id = os.getenv("DISCORD_KANBAN_LOG_CHANNEL") or os.getenv("DISCORD_KANBAN_LOG_CHANNEL_ID") or ("dry-general" if args.dry_run else ensure_channel())
    project_channel_id = os.getenv("DISCORD_KANBAN_PROJECT_LOG_CHANNEL") or os.getenv("DISCORD_KANBAN_PROJECT_LOG_CHANNEL_ID") or ("dry-project" if args.dry_run else ensure_project_channel(channel_id))
    red_channel_id = (
        os.getenv("DISCORD_RED_KANBAN_PROJECT_RUNS_CHANNEL")
        or os.getenv("DISCORD_RED_KANBAN_PROJECT_RUNS_CHANNEL_ID")
        or os.getenv("DISCORD_RED_KANBAN_LOG_CHANNEL")
        or os.getenv("DISCORD_RED_KANBAN_LOG_CHANNEL_ID")
        or RED_PROJECT_RUN_FORUM_ID
    )
    if not args.dry_run:
        channel_id = ensure_channel()
        red_channel_id = ensure_red_channel(channel_id)
    state = load_state()
    if args.migrate_printsmith_component:
        record = migrate_printsmith_state(state, args.migrate_printsmith_component, PRINTSMITH_UPDATES_FORUM_ID, dry_run=args.dry_run)
        print(json.dumps({"component_id": args.migrate_printsmith_component, "dry_run": args.dry_run, "migration": record}, indent=2, sort_keys=True))
        return
    initialize_state(state, channel_id, dry_run=args.dry_run)

    if args.once:
        run_once(state, channel_id, project_channel_id, red_channel_id, dry_run=args.dry_run)
        return

    while True:
        run_once(state, channel_id, project_channel_id, red_channel_id, dry_run=args.dry_run)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
