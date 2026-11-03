"""SessionStore 合并策略：拟人化状态与冷却字段。"""

from __future__ import annotations

from astrbot_plugin_proactive_chat.core.store.session_store import SessionStore


def test_cooldown_until_keeps_later_value():
    merged = SessionStore.merge_session_info(
        {"cooldown_until": 100.0},
        {"cooldown_until": 200.0},
    )
    assert merged["cooldown_until"] == 200.0


def test_cooldown_until_none_does_not_clear_value():
    merged = SessionStore.merge_session_info(
        {"cooldown_until": 100.0},
        {"cooldown_until": None},
    )
    assert merged["cooldown_until"] == 100.0


def test_conversation_memory_keeps_latest_update():
    base = {
        "conversation_memory": {
            "last_updated": 100,
            "open_loops": [{"text": "old"}],
        }
    }
    incoming = {
        "conversation_memory": {
            "last_updated": 200,
            "open_loops": [{"text": "new"}],
        }
    }
    merged = SessionStore.merge_session_info(base, incoming)
    assert merged["conversation_memory"]["open_loops"][0]["text"] == "new"


def test_pause_flag_is_sticky():
    merged = SessionStore.merge_session_info(
        {"proactive_paused": True},
        {"proactive_paused": False},
    )
    assert merged["proactive_paused"] is True


def test_rhythm_keeps_latest_update():
    merged = SessionStore.merge_session_info(
        {"rhythm": {"last_update_ts": 100, "daily_proactive": {"a": 1}}},
        {"rhythm": {"last_update_ts": 200, "daily_proactive": {"b": 2}}},
    )
    assert merged["rhythm"]["last_update_ts"] == 200
