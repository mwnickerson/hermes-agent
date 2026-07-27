import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from hermes_cli.discord_presentation import (
    DISCORD_PRESENTATION_FALLBACK,
    audit_discord_human_texts,
    render_discord_human_text,
)


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5, gold=lambda: 6)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


@pytest.mark.parametrize(
    "text,reason",
    [
        ('{"stdout": "/Users/anton/.hermes/raw-output"}', "jsonish"),
        ("Traceback (most recent call last):\n  File \"/tmp/x.py\", line 1", "traceback"),
        ("stdout: hi\nstderr: nope\nexit code: 1", "terminal-output"),
        ("Open /private/tmp/t_2f5e76f0-codex-lane/out.txt", "absolute-path"),
        ("task_id: t_abcdef123456\nrun_id: 77\nmetadata: {\"x\": 1}", "internal-id"),
        ("```python\nprint('automation leak')\n```", "untrusted-code"),
    ],
)
def test_discord_presentation_bad_fixtures_fail_closed(text, reason):
    result = render_discord_human_text(text, metadata={})
    assert result.allowed is False
    assert result.reason == reason
    assert result.text == DISCORD_PRESENTATION_FALLBACK
    assert text not in result.text


def test_discord_presentation_preserves_prose_markdown_and_interactive_code():
    prose = "**Done:** reviewed the deployment notes.\n\nNext step: approve the release."
    assert render_discord_human_text(prose, metadata={}).text == prose

    code = "Here is the helper:\n```python\nprint('ok')\n```"
    result = render_discord_human_text(
        code,
        metadata={"discord_interactive_response": True, "discord_allow_code": True},
    )
    assert result.allowed is True
    assert result.text == code


@pytest.mark.asyncio
async def test_adapter_send_replaces_rejected_text_before_discord_send():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent_msg = SimpleNamespace(id=1234)
    channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send("555", '{"stdout": "/Users/anton/.hermes/raw-output"}')

    assert result.success is True
    assert channel.send.await_args.kwargs["content"] == DISCORD_PRESENTATION_FALLBACK


def test_audit_recent_bot_fixtures_is_local_policy_only():
    rows = audit_discord_human_texts([
        "Normal update for a human.",
        '{"payload": {"thread_id": "123"}}',
    ])
    assert rows[0]["allowed"] is True
    assert rows[1]["allowed"] is False
    assert rows[1]["rendered"] == DISCORD_PRESENTATION_FALLBACK


def test_kanban_audit_option_does_not_require_discord_credentials(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "kanban_discord_log.py"), "--audit-recent-bot-fixtures"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)
    assert any(row["allowed"] is False for row in rows)


def test_discord_direct_send_inventory_is_explicit():
    repo = Path(__file__).resolve().parents[2]
    files = [
        repo / "plugins" / "platforms" / "discord" / "adapter.py",
        repo / "gateway" / "run.py",
        repo / "scripts" / "kanban_discord_log.py",
    ]
    allowed_markers = (
        "render_discord_human_text",
        "async def send(",
        "_send_to_forum",
        "_forum_post_file",
        "_edit_overflow_split",
        "_send_file_attachment",
        "send_multiple_images",
        "send_voice",
        "send_image_file",
        "send_image",
        "send_animation",
        "send_video",
        "send_document",
        "send_exec_approval",
        "Command Approval Required",
        "send_slash_confirm",
        "send_clarify",
        "send_update_prompt",
        "send_model_picker",
        "_reject_slash",
        "_handle_thread_create_slash",
        "_skill_handler",
        "ExecApprovalView",
        "SlashConfirmView",
        "ClarifyChoiceView",
        "ModelPickerView",
        "UpdatePromptView",
        "Model Configuration",
        "You're not authorized~",
        "This prompt has already been answered",
        "_create_thread",
        "_auto_create_thread",
        "Voice]",
        "Left voice channel",
        "post(",
        "create_thread(",
        "post_redirect_once(",
        "ensure_named_channel",
    )
    offenders = []
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if not any(token in line for token in ("channel.send", ".followup.send", ".response.send_message", "discord_api(\"POST\"")):
                continue
            window = "\n".join(path.read_text(encoding="utf-8").splitlines()[max(0, lineno - 80): lineno + 6])
            if not any(marker in window for marker in allowed_markers):
                offenders.append(f"{path.relative_to(repo)}:{lineno}: {line.strip()}")
    assert offenders == []
