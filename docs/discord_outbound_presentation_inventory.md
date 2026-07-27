# Discord Outbound Presentation Inventory

All regular human-facing Discord text must pass through `hermes_cli.discord_presentation`
before it reaches Discord. The boundary fails closed to a generic message for
raw JSON, escaped JSON, terminal/tool output, tracebacks, absolute paths,
commands, internal ids, and metadata-shaped dumps.

## Centralized Paths

- `plugins/platforms/discord/adapter.py::DiscordAdapter.send`
  - Direct agent finals and interim commentary.
  - Gateway process/background notices.
  - Cron deliveries routed through `gateway.delivery`.
  - Specialist/delegation wrappers routed through gateway adapter sends.
  - Alert/admin/home channel sends that target Discord through the adapter.
  - Forum-channel text starters created from regular sends.
- `plugins/platforms/discord/adapter.py::DiscordAdapter.edit_message`
  - Streaming preview/final edits for interactive Discord replies.
  - Interactive metadata explicitly allows intentional code blocks.
- `plugins/platforms/discord/adapter.py::_standalone_send`
  - `send_message` standalone Discord delivery and cron/script-only sends when
    no live adapter is available.
  - Text and media captions are filtered; binary attachments are not text.
- `scripts/kanban_discord_log.py`
  - REST `post`, `create_thread`, and `post_redirect_once` calls.
  - `--audit-recent-bot-fixtures` is local dry-run only and does not load
    credentials or contact Discord.

## Trusted Direct Paths

- Discord approval/confirmation/clarify/update/model-picker component prompts
  use direct `channel.send`/interaction followups because the visible text is
  constructed by Hermes from trusted prompt fields and often intentionally
  includes command text requiring approval.
- Media upload methods filter only caption/starter text. Attachment bytes and
  filenames are governed by existing media/file safety paths.
- Voice transcript echo in `gateway/run.py` uses `adapter.send` for normal text.
  The low-level voice mixer speaker line is not a regular Hermes text reply.

## Guardrail

`tests/gateway/test_discord_presentation.py` contains bad/positive fixtures and
a static direct-send inventory. New Discord outbound sends must either route
through the presentation boundary or be added to the trusted-direct inventory
with a narrow reason.
