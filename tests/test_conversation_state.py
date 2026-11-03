"""会话关系状态、意图选择与回复反馈测试。"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from astrbot_plugin_proactive_chat.core.domain.humanization import (
    ConversationState,
    Intent,
    ReplyQuality,
)
from astrbot_plugin_proactive_chat.core.services.conversation_state_service import (
    ConversationStateService,
)

UMO = "qq_official:FriendMessage:STATE1"
SETTINGS = {
    "humanization_settings": {
        "state_settings": {
            "enable": True,
            "open_thread_hours": 24,
            "awaiting_reply_hours": 36,
            "cooling_hours": 72,
            "silent_intent_enabled": True,
            "silent_max_streak": 2,
        },
        "feedback_settings": {
            "enable": True,
            "negative_cooldown_hours": 24,
            "perfunctory_no_reset": True,
            "min_substantive_length": 6,
        },
        "persona_settings": {"enable": True, "baseline_energy": 0.6},
    }
}


def ts(hour=10, minute=0, day=5):
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc).timestamp()


class FakePlugin:
    timezone = timezone.utc

    def __init__(self):
        self.session_data = {}
        self.config = {"private_settings": SETTINGS}
        self.timezone = timezone.utc


def make_service():
    plugin = FakePlugin()
    return plugin, ConversationStateService(plugin)


class TestStateTransitions:
    def test_substantive_message_opens_thread(self):
        plugin, service = make_service()
        service.record_user_message(UMO, ReplyQuality.SUBSTANTIVE, now=ts())
        snapshot = service.snapshot(UMO, now=ts(11))
        assert snapshot["state"] == ConversationState.OPEN_THREAD.value

    def test_proactive_moves_to_awaiting_reply(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {"unanswered_count": 1}
        service.record_proactive_sent(UMO, Intent.CARE, now=ts())
        snapshot = service.snapshot(UMO, now=ts(11))
        assert snapshot["state"] == ConversationState.AWAITING_REPLY.value
        assert snapshot["last_intent"] == Intent.CARE.value

    def test_perfunctory_message_cools(self):
        plugin, service = make_service()
        service.record_user_message(UMO, ReplyQuality.PERFUNCTORY, now=ts())
        snapshot = service.snapshot(UMO, now=ts(11))
        assert snapshot["state"] == ConversationState.COOLING.value

    def test_negative_message_cools(self):
        plugin, service = make_service()
        service.record_user_message(UMO, ReplyQuality.NEGATIVE, now=ts())
        snapshot = service.snapshot(UMO, now=ts(11))
        assert snapshot["state"] == ConversationState.COOLING.value
        assert snapshot["last_user_quality"] == "negative"

    def test_paused_state(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {"proactive_paused": True}
        assert service.snapshot(UMO, now=ts())["state"] == ConversationState.PAUSED.value


class TestFeedback:
    def test_positive_feedback_recorded(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {"unanswered_count": 1}
        service.record_proactive_sent(UMO, Intent.CONTINUE_TOPIC, now=ts(10))
        service.record_user_message(
            UMO, ReplyQuality.SUBSTANTIVE, now=ts(10, 30)
        )
        state = service.get_state(UMO)
        stats = state["feedback_stats"]
        bucket = stats.get("forenoon", {})
        item = bucket.get(Intent.CONTINUE_TOPIC.value, {})
        assert item.get("positive") == 1

    def test_negative_feedback_recorded(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {"unanswered_count": 1}
        service.record_proactive_sent(UMO, Intent.NEW_TOPIC, now=ts(10))
        service.record_user_message(UMO, ReplyQuality.NEGATIVE, now=ts(10, 5))
        state = service.get_state(UMO)
        item = state["feedback_stats"]["forenoon"][Intent.NEW_TOPIC.value]
        assert item.get("negative") == 1

    def test_feedback_summary(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {"unanswered_count": 1}
        service.record_proactive_sent(UMO, Intent.CARE, now=ts(10))
        service.record_user_message(
            UMO, ReplyQuality.QUESTION, now=ts(10, 5)
        )
        summary = service.feedback_summary(UMO, now=ts(11))
        assert summary["total"] == 1
        assert summary["positive_rate"] == 1.0


class TestIntentSelection:
    def test_manual_never_silent(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {
            "unanswered_count": 5,
            "conversation_state": {
                "state": ConversationState.AWAITING_REPLY.value,
                "last_user_at": ts(9),
                "last_proactive_at": ts(10),
                "last_intent": Intent.CARE.value,
            },
        }
        for seed in range(10):
            decision = service.choose_intent(
                UMO, persona_energy=0.1, now=ts(11), rng=random.Random(seed), manual=True
            )
            assert decision["intent"] != Intent.SILENT.value

    def test_silent_weight_increases_with_unanswered(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {
            "unanswered_count": 3,
            "conversation_state": {
                "state": ConversationState.AWAITING_REPLY.value,
                "last_user_at": ts(9),
                "last_proactive_at": ts(10),
                "last_intent": Intent.CARE.value,
            },
        }
        decision = service.choose_intent(
            UMO, persona_energy=0.2, now=ts(11), rng=random.Random(1)
        )
        assert decision["weights"]["silent"] > 1.0
        assert decision["state"] == ConversationState.AWAITING_REPLY.value

    def test_silent_streak_cap_removes_silent(self):
        plugin, service = make_service()
        plugin.session_data[UMO] = {
            "unanswered_count": 3,
            "conversation_state": {
                "state": ConversationState.AWAITING_REPLY.value,
                "last_user_at": ts(9),
                "last_proactive_at": ts(10),
                "last_intent": Intent.CARE.value,
                "silent_streak": 2,
            },
        }
        decision = service.choose_intent(
            UMO, persona_energy=0.2, now=ts(11), rng=random.Random(1)
        )
        assert "silent" not in decision["weights"]
