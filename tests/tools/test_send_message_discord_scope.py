import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tools.send_message_tool import _handle_send


def test_scoped_discord_target_is_rejected_before_delivery():
    gateway_config = SimpleNamespace(platforms={Platform.DISCORD: SimpleNamespace(enabled=True)})
    with patch("gateway.config.load_gateway_config", return_value=gateway_config), \
         patch("gateway.channel_directory.discord_target_is_scoped", return_value=False):
        result = json.loads(_handle_send({"target": "discord:123456", "message": "hello"}))

    assert result == {"error": "Discord target is outside this profile's approved scope; message was not sent."}


def test_scoped_discord_parent_with_unknown_thread_is_rejected_before_delivery():
    gateway_config = SimpleNamespace(platforms={Platform.DISCORD: SimpleNamespace(enabled=True)})
    with patch("gateway.config.load_gateway_config", return_value=gateway_config), \
         patch("gateway.channel_directory.discord_target_is_scoped", return_value=False), \
         patch("tools.send_message_tool._send_to_platform", new_callable=AsyncMock) as send:
        result = json.loads(_handle_send({"target": "discord:111:999", "message": "hello"}))

    assert result == {"error": "Discord target is outside this profile's approved scope; message was not sent."}
    send.assert_not_called()
