"""真实磁盘持久化测试：SessionStore、StorageService 与 OverrideStore。"""

from __future__ import annotations

import asyncio
import json

from astrbot_plugin_proactive_chat.core.store.override_store import OverrideStore
from astrbot_plugin_proactive_chat.core.store.session_store import SessionStore

UMO = "qq_official:FriendMessage:DISK1"


def test_session_store_save_and_load_roundtrip(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path)
        payload = {
            UMO: {
                "unanswered_count": 3,
                "proactive_paused": True,
                "pause_reason": "max_unanswered",
                "last_sender_name": "测试用户",
            }
        }
        await store.save(payload)
        loaded = await store.load()
        assert loaded == payload
        assert not list(tmp_path.glob("session_data.json.*.tmp"))

    asyncio.run(scenario())


def test_session_store_save_concurrent_mutation_safe(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path)
        live = {
            f"qq_official:FriendMessage:U{i}": {"unanswered_count": i}
            for i in range(40)
        }

        async def mutator():
            for i in range(40, 80):
                live[f"qq_official:FriendMessage:U{i}"] = {"unanswered_count": i}
                await asyncio.sleep(0)

        await asyncio.gather(store.save(live), mutator())
        loaded = await store.load()
        assert isinstance(loaded, dict)
        assert "qq_official:FriendMessage:U0" in loaded

    asyncio.run(scenario())


def test_session_store_merge_extended_fields():
    merged = SessionStore.merge_session_info(
        {
            "unanswered_count": 2,
            "last_sender_id": "old_id",
            "last_reply_quality": "perfunctory",
            "paused_at": 100.0,
            "pause_reason": "max_unanswered",
        },
        {
            "unanswered_count": 1,
            "last_sender_id": "new_id",
            "last_sender_name": "新昵称",
            "session_name": "备注名",
            "schedule_cancelled": True,
            "paused_at": 200.0,
            "pause_reason": "negative_feedback",
        },
    )
    assert merged["unanswered_count"] == 2
    assert merged["last_sender_id"] == "new_id"
    assert merged["last_sender_name"] == "新昵称"
    assert merged["session_name"] == "备注名"
    assert merged["schedule_cancelled"] is True
    assert merged["pause_reason"] == "negative_feedback"


def test_override_store_normalizes_keys_and_persists(tmp_path):
    async def scenario():
        store = OverrideStore(
            tmp_path,
            normalizer=lambda raw: raw.replace("raw_plat:", "qq_official:"),
        )
        await store.set_override("raw_plat:FriendMessage:U99", {"enable": True})
        assert store.list_sessions() == ["qq_official:FriendMessage:U99"]
        assert store.get_override("raw_plat:FriendMessage:U99") == {"enable": True}

        reloaded = OverrideStore(
            tmp_path,
            normalizer=lambda raw: raw.replace("raw_plat:", "qq_official:"),
        )
        assert reloaded.get_override("qq_official:FriendMessage:U99") == {
            "enable": True
        }

        await reloaded.delete_override("raw_plat:FriendMessage:U99")
        assert reloaded.list_sessions() == []
        saved = json.loads(
            (tmp_path / OverrideStore.OVERRIDES_FILE).read_text(encoding="utf-8")
        )
        assert saved == {}

    asyncio.run(scenario())
