"""开放话题、话题账本与轻量画像测试。"""

from __future__ import annotations

from astrbot_plugin_proactive_chat.core.domain.humanization import ReplyQuality
from astrbot_plugin_proactive_chat.core.services.conversation_memory_service import (
    ConversationMemoryService,
)

UMO = "qq_official:FriendMessage:CMEM1"
SETTINGS = {
    "humanization_settings": {
        "conversation_memory_settings": {
            "enable": True,
            "max_open_loops": 8,
            "max_recent_topics": 8,
            "ttl_days": 7,
            "followup_weight": 1.5,
            "profile_enabled": True,
        }
    }
}


class FakePlugin:
    def __init__(self, settings=None):
        self.session_data = {}
        self.config = {"private_settings": settings or SETTINGS}


def make_service(settings=None):
    plugin = FakePlugin(settings)
    return plugin, ConversationMemoryService(plugin)


class TestOpenLoops:
    def test_extracts_plan_from_user_message(self):
        plugin, service = make_service()
        service.record_user_message(
            UMO,
            "我明天下午有个面试，现在有点紧张",
            ReplyQuality.SUBSTANTIVE,
            now=1_700_000_000,
        )
        state = service.get_state(UMO)
        assert state["open_loops"]
        loop = state["open_loops"][0]
        assert loop["kind"] in {"plan", "event"}
        assert "面试" in loop["text"]

    def test_assistant_question_creates_loop(self):
        plugin, service = make_service()
        service.record_proactive(
            UMO,
            "你明天面试准备得怎么样了？",
            "care",
            now=1_700_000_000,
        )
        snapshot = service.snapshot(UMO, now=1_700_000_100)
        assert snapshot["open_loop_count"] == 1
        assert snapshot["open_loops"][0]["kind"] == "question"

    def test_user_reply_closes_question(self):
        plugin, service = make_service()
        service.record_proactive(
            UMO, "你明天面试准备得怎么样了？", "care", now=1_700_000_000
        )
        service.record_user_message(
            UMO,
            "准备得差不多了",
            ReplyQuality.SUBSTANTIVE,
            now=1_700_000_300,
        )
        snapshot = service.snapshot(UMO, now=1_700_000_400)
        assert snapshot["open_loop_count"] == 0

    def test_format_for_prompt(self):
        plugin, service = make_service()
        service.record_user_message(
            UMO,
            "明天还要去考试",
            ReplyQuality.SUBSTANTIVE,
            now=1_700_000_000,
        )
        text = service.format_for_prompt(UMO, now=1_700_000_100)
        assert "<topic_ledger>" in text
        assert "考试" in text

    def test_profile_updates(self):
        plugin, service = make_service()
        service.record_user_message(
            UMO,
            "刚看完那部电影，结尾有点意外，你怎么看？",
            ReplyQuality.QUESTION,
            now=1_700_000_000,
        )
        profile = service.snapshot(UMO, now=1_700_000_100)["profile"]
        assert profile["message_count"] == 1
        assert profile["avg_length"] > 0
        assert profile["question_ratio"] > 0

    def test_ttl_prunes_old_loops(self):
        settings = {
            "humanization_settings": {
                "conversation_memory_settings": {
                    "enable": True,
                    "ttl_days": 1,
                    "max_open_loops": 8,
                }
            }
        }
        plugin, service = make_service(settings)
        service.record_user_message(
            UMO,
            "明天有面试",
            ReplyQuality.SUBSTANTIVE,
            now=1_700_000_000,
        )
        snapshot = service.snapshot(UMO, now=1_700_000_000 + 2 * 86400)
        assert snapshot["open_loop_count"] == 0

    def test_seed_from_history(self):
        plugin, service = make_service()
        service.seed_from_history(
            UMO,
            [
                {"role": "user", "content": "我明天有个面试"},
                {"role": "assistant", "content": "准备得怎么样了？"},
            ],
            now=1_700_000_000,
        )
        assert service.open_loop_count(UMO, now=1_700_000_000) >= 1
