# Kanban Discord Watcher Ownership

`scripts/kanban_discord_log.py` is the durable local source of truth for the
Kanban Discord watcher. The runtime copy at
`~/.hermes/scripts/kanban_discord_log.py` is deployment output.

Preview without posting:

```bash
python scripts/kanban_discord_log.py --preview-fixtures
python scripts/kanban_discord_log.py --dry-run --once
```

Deploy locally after tests and independent review pass. The watcher imports a
version-matched presentation module from its private deployment directory, so
the dirty main Hermes checkout is never modified:

```bash
mkdir -p ~/.hermes/scripts/lib/hermes_cli
install -m 0644 /dev/null ~/.hermes/scripts/lib/hermes_cli/__init__.py
install -m 0644 hermes_cli/kanban_project_model.py ~/.hermes/scripts/lib/hermes_cli/kanban_project_model.py
install -m 0755 scripts/kanban_discord_log.py ~/.hermes/scripts/kanban_discord_log.py
```

Rollback locally by restoring the saved runtime script and private presentation
module copies, then restarting only `com.hermes.kanban-discord-log`.
This branch is local-only for the Kanban PM presentation work; do not open an
upstream PR for this watcher deployment.

Suppression decisions are logged as structured records to
`~/.hermes/logs/kanban_discord_pm.log` by default. Logs include event kind,
project slug, and a hashed event reference, but not raw worker payloads.

For a deliverable project event, the watcher preserves the prior event cursor
until Discord accepts the post. It retries a transient failure up to three
times, recording only the bounded attempt count, error class, and hashed event
reference in durable local state. A third failure is recorded as exhausted so
operators can reconcile it through Project Hub without replaying an unknown
message. This retry state is distinct from the profile-aware owner-alert
receipt used for completion and blocker notifications.
