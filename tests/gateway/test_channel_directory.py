"""Tests for gateway/channel_directory.py — channel resolution and display."""

import asyncio
import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.channel_directory import (
    build_channel_directory,
    lookup_channel_type,
    resolve_channel_name,
    format_directory_for_display,
    load_directory,
    _apply_channel_aliases,
    _build_from_sessions,
    _build_slack,
)
import gateway.channel_directory as channel_directory


import pytest


@pytest.fixture(autouse=True)
def _isolate_channel_aliases(tmp_path_factory):
    """Point the alias overlay at a nonexistent path by default so a real
    ~/.hermes/channel_aliases.json never leaks into directory tests. Tests
    that exercise aliases patch CHANNEL_ALIASES_PATH themselves inside the
    test body, which takes precedence over this outer patch."""
    missing = tmp_path_factory.mktemp("aliases") / "none.json"
    with patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", missing), \
         patch("gateway.channel_directory.load_config_readonly", return_value={}):
        yield


def _write_directory(tmp_path, platforms):
    """Helper to write a fake channel directory."""
    data = {"updated_at": "2026-01-01T00:00:00", "platforms": platforms}
    cache_file = tmp_path / "channel_directory.json"
    cache_file.write_text(json.dumps(data))
    return cache_file


class TestLoadDirectory:
    def test_missing_file(self, tmp_path):
        with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "nope.json"):
            result = load_directory()
        assert result["updated_at"] is None
        assert result["platforms"] == {}

    def test_valid_file(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [{"id": "123", "name": "John", "type": "dm"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = load_directory()
        assert result["platforms"]["telegram"][0]["name"] == "John"

    def test_corrupt_file(self, tmp_path):
        cache_file = tmp_path / "channel_directory.json"
        cache_file.write_text("{bad json")
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = load_directory()
        assert result["updated_at"] is None


class TestBuildChannelDirectoryWrites:
    def test_failed_write_preserves_previous_cache(self, tmp_path, monkeypatch):
        cache_file = _write_directory(tmp_path, {
            "telegram": [{"id": "123", "name": "Alice", "type": "dm"}]
        })
        previous = json.loads(cache_file.read_text())

        def broken_dump(data, fp, *args, **kwargs):
            fp.write('{"updated_at":')
            fp.flush()
            raise OSError("disk full")

        monkeypatch.setattr(json, "dump", broken_dump)

        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            asyncio.run(build_channel_directory({}))
            result = load_directory()

        assert result == previous


class TestBuildChannelDirectoryOffload:
    def test_discord_builder_runs_off_event_loop_thread(self, tmp_path):
        from gateway.config import Platform

        cache_file = tmp_path / "channel_directory.json"
        loop_thread = threading.get_ident()
        builder_threads = []

        def fake_build_discord(_adapter, *, scoped_ids=None):
            builder_threads.append((threading.get_ident(), scoped_ids))
            return []

        with patch("gateway.channel_directory._build_discord", side_effect=fake_build_discord), \
             patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            asyncio.run(build_channel_directory({Platform.DISCORD: object()}))

        assert builder_threads
        assert all(tid != loop_thread for tid, _ in builder_threads)
        assert [scope for _, scope in builder_threads] == [None]

    def test_scoped_config_passes_configured_discord_scope(self, tmp_path):
        from gateway.config import Platform

        cache_file = tmp_path / "channel_directory.json"
        requested_scopes = []

        def fake_build_discord(_adapter, *, scoped_ids=None):
            requested_scopes.append(scoped_ids)
            return []

        with patch("gateway.channel_directory._build_discord", side_effect=fake_build_discord), \
             patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=({"222"}, set())), \
             patch(
                 "gateway.channel_directory.load_config_readonly",
                 return_value={"channel_directory": {"discord_discovery": "scoped"}, "discord": {"allowed_channels": ["111"]}},
             ):
            asyncio.run(build_channel_directory({Platform.DISCORD: object()}))

        assert requested_scopes == [{"111", "222"}]

    def test_scoped_target_check_admits_gateway_home_but_not_thread_only(self):
        config = {"channel_directory": {"discord_discovery": "scoped"}, "discord": {}}
        with patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=({"111"}, set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=[]):
            assert channel_directory.discord_target_is_scoped("111") is True
            assert channel_directory.discord_target_is_scoped("999", thread_id="111") is False

    def test_scoped_target_rejects_an_unscoped_child_thread(self):
        config = {"channel_directory": {"discord_discovery": "scoped"}, "discord": {"allowed_channels": ["111"]}}
        with patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=[]):
            assert channel_directory.discord_target_is_scoped("111", thread_id="999") is False

    def test_scoped_builder_keeps_profile_local_sessions_and_excludes_unscoped_guilds(self, monkeypatch):
        session_entry = {"id": "111", "name": "known-session", "type": "channel", "thread_id": None}
        guild_channel = SimpleNamespace(id=222, name="undiscovered", type="channel")
        guild = SimpleNamespace(name="shared-guild", text_channels=[guild_channel], forum_channels=[])
        adapter = SimpleNamespace(_client=SimpleNamespace(guilds=[guild]))
        monkeypatch.setitem(sys.modules, "discord", SimpleNamespace())
        monkeypatch.setattr(channel_directory, "_build_from_sessions", lambda platform: [session_entry])

        entries = channel_directory._build_discord(adapter, scoped_ids=set())

        assert entries == [session_entry]

    def test_scoped_builder_rejects_malformed_or_separated_profile_sessions(self, monkeypatch):
        allowed = {"id": "111:333", "name": "known-session", "type": "thread", "thread_id": "333"}
        malformed = {"id": "legacy-thread", "name": "old-session", "type": "thread", "thread_id": None}
        separated_profile = {"id": "999:444", "name": "Red Antonetta / old thread", "type": "thread", "thread_id": "444"}
        adapter = SimpleNamespace(_client=SimpleNamespace(guilds=[]))
        monkeypatch.setitem(sys.modules, "discord", SimpleNamespace())
        monkeypatch.setattr(channel_directory, "_build_from_sessions", lambda platform: [allowed, malformed, separated_profile])

        entries = channel_directory._build_discord(adapter, scoped_ids=set())

        assert entries == [allowed]

    def test_scoped_target_check_rejects_malformed_or_separated_profile_sessions(self):
        config = {"channel_directory": {"discord_discovery": "scoped"}, "discord": {}}
        sessions = [
            {"id": "111:333", "name": "known-session", "type": "thread"},
            {"id": "legacy-thread", "name": "old-session", "type": "thread"},
            {"id": "999:444", "name": "Red Antonetta / old thread", "type": "thread"},
        ]
        with patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=sessions):
            assert channel_directory.discord_target_is_scoped("111", thread_id="333") is True
            assert channel_directory.discord_target_is_scoped("999", thread_id="444") is False
            assert channel_directory.discord_target_is_scoped("legacy-thread") is False

    def test_scoped_aliases_cannot_inject_undiscovered_discord_targets(self):
        platforms = {"discord": [{"id": "111", "name": "original", "type": "channel"}]}
        with patch("gateway.channel_directory._load_channel_aliases", return_value={"discord": {"111": "renamed", "222": "undiscovered"}}):
            _apply_channel_aliases(platforms, scoped_discord=True)
        assert platforms["discord"] == [{"id": "111", "name": "renamed", "type": "channel"}]

    def test_scoped_load_filters_preexisting_directory_before_aliases(self, tmp_path):
        cache_file = _write_directory(tmp_path, {"discord": [{"id": "111", "name": "allowed", "type": "channel"}, {"id": "999", "name": "stale", "type": "channel"}]})
        config = {"channel_directory": {"discord_discovery": "scoped"}, "discord": {"allowed_channels": ["111"]}}
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=[]), \
             patch("gateway.channel_directory._load_channel_aliases", return_value={"discord": {"999": "cannot-return"}}):
            result = load_directory()
        assert result["platforms"]["discord"] == [{"id": "111", "name": "allowed", "type": "channel"}]

    def test_scoped_load_removes_cached_separated_profile_thread_even_when_its_ids_are_allowed(self, tmp_path):
        cache_file = _write_directory(tmp_path, {"discord": [
            {"id": "111", "name": "allowed", "type": "channel"},
            {"id": "111:222", "name": "Red Antonetta / old thread", "type": "thread"},
        ]})
        config = {
            "channel_directory": {"discord_discovery": "scoped"},
            "discord": {"allowed_channels": ["111"], "channel_overrides": {"222": {}}},
        }
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=[]):
            result = load_directory()
        assert result["platforms"]["discord"] == [{"id": "111", "name": "allowed", "type": "channel"}]

    def test_scoped_target_check_enforces_configured_and_session_ids(self):
        config = {"channel_directory": {"discord_discovery": "scoped"}, "discord": {"allowed_channels": ["111"]}}
        with patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=[{"id": "222:333", "name": "session", "type": "channel"}]):
            assert channel_directory.discord_target_is_scoped("111") is True
            assert channel_directory.discord_target_is_scoped("222") is True
            assert channel_directory.discord_target_is_scoped("999") is False

    def test_scoped_routing_admits_skill_bindings_and_override_keys(self):
        config = {
            "channel_directory": {"discord_discovery": "scoped"},
            "discord": {
                "channel_skill_bindings": [{"id": "222", "skills": ["triage"]}],
                "channel_overrides": {"333": {"model": "test"}},
            },
        }
        with patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())):
            scope = channel_directory._configured_discord_scope(config)
        assert scope is not None
        assert {"222", "333"} <= scope.parent_ids
        assert {"222", "333"} <= scope.thread_ids

    def test_scoped_load_removes_cached_unscoped_child_threads(self, tmp_path):
        cache_file = _write_directory(tmp_path, {"discord": [
            {"id": "111:222", "name": "known", "type": "channel"},
            {"id": "111:999", "name": "unscoped", "type": "channel"},
        ]})
        config = {
            "channel_directory": {"discord_discovery": "scoped"},
            "discord": {"allowed_channels": ["111"], "channel_overrides": {"222": {}}},
        }
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory._load_channel_directory_config", return_value=config), \
             patch("gateway.channel_directory._gateway_discord_scope", return_value=(set(), set())), \
             patch("gateway.channel_directory._build_from_sessions", return_value=[]):
            result = load_directory()
        assert result["platforms"]["discord"] == [{"id": "111:222", "name": "known", "type": "channel"}]

    def test_config_failure_denies_discovery_delivery_and_cached_entries(self, tmp_path):
        from gateway.config import Platform

        cache_file = _write_directory(tmp_path, {"discord": [{"id": "111", "name": "cached", "type": "channel"}]})
        with patch("gateway.channel_directory._load_channel_directory_config", return_value=None), \
             patch("gateway.channel_directory._build_discord") as build_discord, \
             patch("gateway.channel_directory._load_channel_aliases", return_value={"discord": {"222": "must-not-inject"}}), \
             patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            rebuilt = asyncio.run(build_channel_directory({Platform.DISCORD: object()}))
            loaded = load_directory()
            allowed = channel_directory.discord_target_is_scoped("111")
        assert rebuilt["platforms"]["discord"] == []
        assert loaded["platforms"]["discord"] == []
        assert allowed is False
        build_discord.assert_not_called()

    def test_explicit_invalid_discovery_policy_denies_instead_of_broadening(self):
        assert channel_directory._discord_directory_discovery_mode(
            {"channel_directory": {"discord_discovery": "typo"}}
        ) == "deny"
        assert channel_directory._discord_directory_discovery_mode(
            {"channel_directory": {"discord_discovery": True}}
        ) == "deny"
        assert channel_directory._discord_directory_discovery_mode(
            {"channel_directory": []}
        ) == "deny"

    def test_unscoped_target_check_preserves_existing_delivery_behavior(self):
        with patch("gateway.channel_directory._load_channel_directory_config", return_value={}):
            assert channel_directory.discord_target_is_scoped("999") is None

    def test_session_discovery_runs_off_event_loop_thread(self, tmp_path):
        from gateway.config import Platform

        cache_file = tmp_path / "channel_directory.json"
        loop_thread = threading.get_ident()
        calls = []

        def fake_build_from_sessions(platform_name):
            calls.append((platform_name, threading.get_ident()))
            return []

        with patch("gateway.channel_directory._build_from_sessions", side_effect=fake_build_from_sessions), \
             patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            asyncio.run(build_channel_directory({Platform.TELEGRAM: object()}))

        assert [name for name, _ in calls] == ["telegram"]
        assert calls[0][1] != loop_thread

    def test_plugin_session_discovery_runs_off_event_loop_thread(self, tmp_path):
        cache_file = tmp_path / "channel_directory.json"
        loop_thread = threading.get_ident()
        calls = []
        plugin_entry = SimpleNamespace(name="irc")

        def fake_build_from_sessions(platform_name):
            calls.append((platform_name, threading.get_ident()))
            return []

        with patch("gateway.channel_directory._build_from_sessions", side_effect=fake_build_from_sessions), \
             patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch(
                 "gateway.platform_registry.platform_registry.plugin_entries",
                 return_value=[plugin_entry],
             ):
            asyncio.run(build_channel_directory({"irc": object()}))

        assert [name for name, _ in calls] == ["irc"]
        assert calls[0][1] != loop_thread

    def test_slack_session_merge_runs_off_event_loop_thread(self):
        loop_thread = threading.get_ident()
        calls = []

        class FakeSlackClient:
            async def users_conversations(self, **_kwargs):
                return {"ok": True, "channels": []}

        def fake_build_from_sessions(platform_name):
            calls.append((platform_name, threading.get_ident()))
            return [{"id": "D1", "name": "Alice", "type": "dm"}]

        adapter = SimpleNamespace(_team_clients={"T1": FakeSlackClient()})
        with patch("gateway.channel_directory._build_from_sessions", side_effect=fake_build_from_sessions):
            channels = asyncio.run(_build_slack(adapter))

        assert channels == [{"id": "D1", "name": "Alice", "type": "dm"}]
        assert [name for name, _ in calls] == ["slack"]
        assert calls[0][1] != loop_thread


class TestResolveChannelName:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_exact_match(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "111", "name": "bot-home", "guild": "MyServer", "type": "channel"},
                {"id": "222", "name": "general", "guild": "MyServer", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("discord", "bot-home") == "111"
            assert resolve_channel_name("discord", "#bot-home") == "111"

    def test_case_insensitive(self, tmp_path):
        platforms = {
            "slack": [{"id": "C01", "name": "Engineering", "type": "channel"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("slack", "engineering") == "C01"
            assert resolve_channel_name("slack", "ENGINEERING") == "C01"

    def test_guild_qualified_match(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "111", "name": "general", "guild": "ServerA", "type": "channel"},
                {"id": "222", "name": "general", "guild": "ServerB", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("discord", "ServerA/general") == "111"
            assert resolve_channel_name("discord", "ServerB/general") == "222"

    def test_prefix_match_unambiguous(self, tmp_path):
        platforms = {
            "slack": [
                {"id": "C01", "name": "engineering-backend", "type": "channel"},
                {"id": "C02", "name": "design-team", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            # "engineering" prefix matches only one channel
            assert resolve_channel_name("slack", "engineering") == "C01"

    def test_prefix_match_ambiguous_returns_none(self, tmp_path):
        platforms = {
            "slack": [
                {"id": "C01", "name": "eng-backend", "type": "channel"},
                {"id": "C02", "name": "eng-frontend", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("slack", "eng") is None

    def test_no_channels_returns_none(self, tmp_path):
        with self._setup(tmp_path, {}):
            assert resolve_channel_name("telegram", "someone") is None

    def test_no_match_returns_none(self, tmp_path):
        platforms = {
            "telegram": [{"id": "123", "name": "John", "type": "dm"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "nonexistent") is None

    def test_topic_name_resolves_to_composite_id(self, tmp_path):
        platforms = {
            "telegram": [{"id": "-1001:17585", "name": "Coaching Chat / topic 17585", "type": "group"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "Coaching Chat / topic 17585") == "-1001:17585"

    def test_id_match_takes_precedence_over_name(self, tmp_path):
        """A raw channel ID resolves to itself, even when a different
        channel happens to be named the same string. Case-sensitive: Slack
        IDs are uppercase and must not be normalized away."""
        platforms = {
            "slack": [
                {"id": "C0B0QV5434G", "name": "engineering", "type": "channel"},
                {"id": "C99", "name": "c0b0qv5434g", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("slack", "C0B0QV5434G") == "C0B0QV5434G"
            # Lowercase still falls through to name matching (case-insensitive)
            assert resolve_channel_name("slack", "c0b0qv5434g") == "C99"

    def test_display_label_with_type_suffix_resolves(self, tmp_path):
        platforms = {
            "telegram": [
                {"id": "123", "name": "Alice", "type": "dm"},
                {"id": "456", "name": "Dev Group", "type": "group"},
                {"id": "-1001:17585", "name": "Coaching Chat / topic 17585", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "Alice (dm)") == "123"
            assert resolve_channel_name("telegram", "Dev Group (group)") == "456"
            assert resolve_channel_name("telegram", "Coaching Chat / topic 17585 (group)") == "-1001:17585"


class TestBuildFromSessions:
    def _write_sessions(self, tmp_path, sessions_data):
        """Write sessions.json at the path _build_from_sessions expects."""
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps(sessions_data))

    def test_builds_from_sessions_json(self, tmp_path):
        self._write_sessions(tmp_path, {
            "session_1": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "12345",
                    "chat_name": "Alice",
                },
                "chat_type": "dm",
            },
            "session_2": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "67890",
                    "user_name": "Bob",
                },
                "chat_type": "group",
            },
            "session_3": {
                "origin": {
                    "platform": "discord",
                    "chat_id": "99999",
                },
            },
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")

        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert "Alice" in names
        assert "Bob" in names

    def test_missing_sessions_file(self, tmp_path):
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")
        assert entries == []

    def test_deduplication_by_chat_id(self, tmp_path):
        self._write_sessions(tmp_path, {
            "s1": {"origin": {"platform": "telegram", "chat_id": "123", "chat_name": "X"}},
            "s2": {"origin": {"platform": "telegram", "chat_id": "123", "chat_name": "X"}},
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")

        assert len(entries) == 1

    def test_keeps_distinct_topics_with_same_chat_id(self, tmp_path):
        self._write_sessions(tmp_path, {
            "group_root": {
                "origin": {"platform": "telegram", "chat_id": "-1001", "chat_name": "Coaching Chat"},
                "chat_type": "group",
            },
            "topic_a": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_name": "Coaching Chat",
                    "thread_id": "17585",
                },
                "chat_type": "group",
            },
            "topic_b": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_name": "Coaching Chat",
                    "thread_id": "17587",
                },
                "chat_type": "group",
            },
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")

        ids = {entry["id"] for entry in entries}
        names = {entry["name"] for entry in entries}
        assert ids == {"-1001", "-1001:17585", "-1001:17587"}
        assert "Coaching Chat" in names
        assert "Coaching Chat / topic 17585" in names
        assert "Coaching Chat / topic 17587" in names


class TestFormatDirectoryForDisplay:
    def test_empty_directory(self, tmp_path):
        with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "nope.json"):
            result = format_directory_for_display()
        assert "No messaging platforms" in result

    def test_telegram_display(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [
                {"id": "123", "name": "Alice", "type": "dm"},
                {"id": "456", "name": "Dev Group", "type": "group"},
                {"id": "-1001:17585", "name": "Coaching Chat / topic 17585", "type": "group"},
            ]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        assert "Telegram:" in result
        assert "telegram:Alice" in result
        assert "telegram:Dev Group" in result
        assert "telegram:Coaching Chat / topic 17585" in result

    def test_discord_grouped_by_guild(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "discord": [
                {"id": "1", "name": "general", "guild": "Server1", "type": "channel"},
                {"id": "2", "name": "bot-home", "guild": "Server1", "type": "channel"},
                {"id": "3", "name": "chat", "guild": "Server2", "type": "channel"},
            ]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        assert "Discord (Server1):" in result
        assert "Discord (Server2):" in result
        assert "discord:#general" in result


class TestLookupChannelType:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_forum_channel(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "100", "name": "ideas", "guild": "Server1", "type": "forum"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "100") == "forum"

    def test_regular_channel(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "200", "name": "general", "guild": "Server1", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "200") == "channel"

    def test_unknown_chat_id_returns_none(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "200", "name": "general", "guild": "Server1", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "999") is None

    def test_unknown_platform_returns_none(self, tmp_path):
        with self._setup(tmp_path, {}):
            assert lookup_channel_type("discord", "100") is None

    def test_channel_without_type_key_returns_none(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "300", "name": "general", "guild": "Server1"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "300") is None


def _make_slack_adapter(team_clients):
    """Build a stand-in for SlackAdapter exposing only ``_team_clients``."""
    return SimpleNamespace(_team_clients=team_clients)


def _make_slack_client(pages):
    """Build an AsyncWebClient mock whose ``users_conversations`` returns pages."""
    client = MagicMock()
    client.users_conversations = AsyncMock(side_effect=pages)
    return client


class TestBuildSlack:
    """_build_slack actually calls users.conversations on each workspace client."""

    def test_no_team_clients_falls_back_to_sessions(self, tmp_path):
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps({
            "s1": {"origin": {"platform": "slack", "chat_id": "D123", "chat_name": "Alice"}},
        }))

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({})))

        assert len(entries) == 1
        assert entries[0]["id"] == "D123"

    def test_lists_channels_from_users_conversations(self, tmp_path):
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [
                    {"id": "C0B0QV5434G", "name": "engineering", "is_private": False},
                    {"id": "G123ABCDEF", "name": "secret-chat", "is_private": True},
                ],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        ids = {e["id"] for e in entries}
        assert ids == {"C0B0QV5434G", "G123ABCDEF"}
        types = {e["id"]: e["type"] for e in entries}
        assert types["C0B0QV5434G"] == "channel"
        assert types["G123ABCDEF"] == "private"
        client.users_conversations.assert_awaited_once()

    def test_paginates_via_response_metadata_cursor(self, tmp_path):
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [{"id": "C001", "name": "first", "is_private": False}],
                "response_metadata": {"next_cursor": "cur1"},
            },
            {
                "ok": True,
                "channels": [{"id": "C002", "name": "second", "is_private": False}],
                "response_metadata": {"next_cursor": ""},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        assert {e["id"] for e in entries} == {"C001", "C002"}
        assert client.users_conversations.await_count == 2

    def test_per_workspace_error_does_not_block_others(self, tmp_path):
        bad = MagicMock()
        bad.users_conversations = AsyncMock(side_effect=RuntimeError("boom"))
        good = _make_slack_client([
            {
                "ok": True,
                "channels": [{"id": "C999", "name": "ok-channel", "is_private": False}],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"BAD": bad, "GOOD": good})))

        assert {e["id"] for e in entries} == {"C999"}

    def test_session_dms_merged_when_not_in_api_results(self, tmp_path):
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps({
            "s1": {"origin": {"platform": "slack", "chat_id": "D456", "chat_name": "Bob"}},
            "dup": {"origin": {"platform": "slack", "chat_id": "C001", "chat_name": "first"}},
        }))
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [{"id": "C001", "name": "first", "is_private": False}],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        ids = {e["id"] for e in entries}
        assert "C001" in ids and "D456" in ids
        # Channel ID from API should not be duplicated by the session merge
        assert sum(1 for e in entries if e["id"] == "C001") == 1

    def test_skips_channels_with_no_id_or_name(self, tmp_path):
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [
                    {"id": "C001", "name": "good", "is_private": False},
                    {"id": "", "name": "no-id"},
                    {"id": "C002"},  # no name (e.g. IM)
                ],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        assert {e["id"] for e in entries} == {"C001"}

    def test_response_not_ok_breaks_pagination_for_that_workspace(self, tmp_path):
        client = _make_slack_client([
            {"ok": False, "error": "missing_scope"},
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        assert entries == []


class TestChannelAliases:
    """The user-maintained alias overlay (channel_aliases.json) gives durable
    friendly names that survive the timed directory rebuild."""

    def _setup_aliases(self, tmp_path, aliases):
        alias_file = tmp_path / "channel_aliases.json"
        alias_file.write_text(json.dumps(aliases))
        return patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", alias_file)

    def test_alias_renames_existing_entry_on_load(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "whatsapp": [{"id": "120363@g.us", "name": "120363", "type": "group"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             self._setup_aliases(tmp_path, {"whatsapp": {"120363@g.us": "general"}}):
            result = load_directory()
            assert result["platforms"]["whatsapp"][0]["name"] == "general"
            # And the friendly name resolves back to the JID
            assert resolve_channel_name("whatsapp", "general") == "120363@g.us"
            assert resolve_channel_name("whatsapp", "GENERAL") == "120363@g.us"

    def test_alias_injects_undiscovered_group(self, tmp_path):
        """A group named in the alias file but not yet seen in any session is
        still addressable by name (pre-naming before first traffic)."""
        cache_file = _write_directory(tmp_path, {"whatsapp": []})
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             self._setup_aliases(tmp_path, {"whatsapp": {"999@g.us": "marketing"}}):
            assert resolve_channel_name("whatsapp", "marketing") == "999@g.us"
            entries = load_directory()["platforms"]["whatsapp"]
            injected = [e for e in entries if e["id"] == "999@g.us"]
            assert injected and injected[0]["type"] == "group"

    def test_no_alias_file_is_noop(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "whatsapp": [{"id": "120363@g.us", "name": "120363", "type": "group"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", tmp_path / "nope.json"):
            result = load_directory()
            assert result["platforms"]["whatsapp"][0]["name"] == "120363"

    def test_corrupt_alias_file_is_ignored(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "whatsapp": [{"id": "120363@g.us", "name": "120363", "type": "group"}]
        })
        bad = tmp_path / "channel_aliases.json"
        bad.write_text("{not json")
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", bad):
            result = load_directory()
            assert result["platforms"]["whatsapp"][0]["name"] == "120363"

    def test_alias_persists_through_rebuild(self, tmp_path, monkeypatch):
        """build_channel_directory must bake aliases into the written file so
        they survive the periodic regeneration, not just live reads."""
        cache_file = tmp_path / "channel_directory.json"
        monkeypatch.setattr("gateway.channel_directory._build_from_sessions",
                            lambda plat: [{"id": "120363@g.us", "name": "120363",
                                           "type": "group", "thread_id": None}]
                            if plat == "whatsapp" else [])
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             self._setup_aliases(tmp_path, {"whatsapp": {"120363@g.us": "general"}}):
            asyncio.run(build_channel_directory({}))
            on_disk = json.loads(cache_file.read_text())
        names = [e["name"] for e in on_disk["platforms"]["whatsapp"]
                 if e["id"] == "120363@g.us"]
        assert names == ["general"]

    def test_apply_aliases_handles_malformed_map(self):
        """Non-dict alias maps and non-string aliases must not raise."""
        platforms = {"whatsapp": [{"id": "1@g.us", "name": "1", "type": "group"}]}
        with patch("gateway.channel_directory._load_channel_aliases",
                   return_value={
                       "whatsapp": "not-a-dict",
                       "telegram": None,
                       "signal": {"+15551234567": 123},
                   }):
            _apply_channel_aliases(platforms)  # should not raise
        assert platforms["whatsapp"][0]["name"] == "1"
