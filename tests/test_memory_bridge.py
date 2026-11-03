"""外部长期记忆桥接测试（使用假 LivingMemory 实例，不触碰真实数据）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.services.memory_bridge import MemoryBridge

UMO = "qq_offical:FriendMessage:MEM1"
SETTINGS = {
    "humanization_settings": {
        "memory_integration_settings": {
            "enable": True,
            "plugin_id": "astrbot_plugin_livingmemory",
            "top_k": 3,
            "inject_into_prompt": True,
            "write_proactive": True,
            "timeout_seconds": 2,
        }
    }
}


class FakeMemoryEngine:
    def __init__(self):
        self.calls = []

    async def search_memories(self, query, k=5, session_id=None, persona_id=None):
        self.calls.append(
            {
                "query": query,
                "k": k,
                "session_id": session_id,
                "persona_id": persona_id,
            }
        )
        return [
            SimpleNamespace(
                content="对方下周三有面试，最近在准备",
                final_score=0.91,
                metadata={"importance": 0.8, "create_time": 1700000000},
            ),
            SimpleNamespace(
                content="对方喜欢喝无糖乌龙茶",
                final_score=0.75,
                metadata={"importance": 0.4},
            ),
        ]


class FakeConversationManager:
    def __init__(self):
        self.messages = []

    async def get_last_message(self, session_id):
        return self.messages[-1] if self.messages else None

    async def add_message(self, **kwargs):
        message = SimpleNamespace(**kwargs)
        self.messages.append(message)
        return message


class FakeLivingMemory:
    def __init__(self):
        self.initializer = SimpleNamespace(
            memory_engine=FakeMemoryEngine(),
            conversation_manager=FakeConversationManager(),
        )


FakeLivingMemory.__name__ = "LivingMemoryPlugin"


class FakeContext:
    def __init__(self, stars):
        self._stars = stars

    def get_all_stars(self):
        return self._stars


class FakePlugin:
    def __init__(self, stars):
        self.config = {"private_settings": SETTINGS}
        self.context = FakeContext(stars)


class TestMemoryRecall:
    def test_recall_and_format(self):
        star = FakeLivingMemory()
        plugin = FakePlugin([object(), star])
        bridge = MemoryBridge(plugin)
        memories = asyncio.run(
            bridge.recall(UMO, "最近在准备面试", persona_id="p1")
        )
        assert len(memories) == 2
        call = star.initializer.memory_engine.calls[0]
        assert call["session_id"] == UMO
        assert call["k"] == 3
        assert call["persona_id"] == "p1"

        text = bridge.format_memories(memories)
        assert "<long_term_memory>" in text
        assert "面试" in text
        assert "无糖乌龙茶" in text

    def test_missing_plugin_returns_empty(self):
        bridge = MemoryBridge(FakePlugin([]))
        assert asyncio.run(bridge.recall(UMO, "在吗")) == []
        assert asyncio.run(bridge.is_available()) is False

    def test_empty_query_skipped(self):
        star = FakeLivingMemory()
        bridge = MemoryBridge(FakePlugin([star]))
        assert asyncio.run(bridge.recall(UMO, "  ")) == []
        assert star.initializer.memory_engine.calls == []

    def test_resolves_star_metadata_wrapper(self):
        star = FakeLivingMemory()
        wrapper = SimpleNamespace(
            module_path="astrbot_plugin_livingmemory.main",
            star_cls=star,
            activated=True,
        )
        bridge = MemoryBridge(FakePlugin([wrapper]))
        memories = asyncio.run(bridge.recall(UMO, "面试"))
        assert memories
        assert star.initializer.memory_engine.calls


class TestMemoryWriteBack:
    def test_store_proactive_message(self):
        star = FakeLivingMemory()
        bridge = MemoryBridge(FakePlugin([star]))
        ok = asyncio.run(bridge.store_proactive(UMO, "刚下班，路过那家店"))
        assert ok is True
        messages = star.initializer.conversation_manager.messages
        assert len(messages) == 1
        assert messages[0].role == "assistant"
        assert messages[0].is_bot_message is True
        assert messages[0].metadata["source"] == "proactive_chat"

    def test_duplicate_write_skipped(self):
        star = FakeLivingMemory()
        bridge = MemoryBridge(FakePlugin([star]))
        asyncio.run(bridge.store_proactive(UMO, "同一句话"))
        ok = asyncio.run(bridge.store_proactive(UMO, "同一句话"))
        assert ok is False
        assert len(star.initializer.conversation_manager.messages) == 1

    def test_write_disabled(self):
        settings = {
            "humanization_settings": {
                "memory_integration_settings": {
                    "enable": True,
                    "write_proactive": False,
                }
            }
        }
        star = FakeLivingMemory()
        plugin = FakePlugin([star])
        plugin.config = {"private_settings": settings}
        bridge = MemoryBridge(plugin)
        assert asyncio.run(bridge.store_proactive(UMO, "hello")) is False
