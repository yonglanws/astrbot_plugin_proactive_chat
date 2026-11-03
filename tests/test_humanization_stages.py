"""拟人化流水线阶段测试：意图、守卫与发送计划。"""

from __future__ import annotations

import asyncio

from astrbot_plugin_proactive_chat.core.domain.domain_enums import StageResult
from astrbot_plugin_proactive_chat.core.features.delivery_plan_feature import (
    DeliveryPlanStage,
)
from astrbot_plugin_proactive_chat.core.features.guard_feature import GuardStage
from astrbot_plugin_proactive_chat.core.features.intent_feature import IntentStage

UMO = "qq_official:FriendMessage:STAGE1"
SETTINGS = {
    "enable": True,
    "schedule_settings": {"min_interval_minutes": 30, "max_interval_minutes": 120},
    "humanization_settings": {
        "delivery_plan_settings": {
            "enable": True,
            "max_bubbles": 3,
            "bubble_length_threshold": 30,
        },
        "guard_settings": {"enable": True},
    },
}


class FakeConfigService:
    def get_session_config(self, session_id):
        return SETTINGS


class FakeSessionService:
    def get_session_log_str(self, session_id, config=None):
        return session_id


class FakeStorage:
    async def save_data(self):
        return None


class FakeConversationStateService:
    def __init__(self, decision):
        self.decision = decision

    def choose_intent(self, *args, **kwargs):
        return self.decision


class FakePersonaService:
    def snapshot(self, *args, **kwargs):
        return {"energy": 0.2}


class FakeIntentPlugin:
    def __init__(self, decision):
        self.session_data = {}
        self.config_service = FakeConfigService()
        self.session_service = FakeSessionService()
        self.conversation_state_service = FakeConversationStateService(decision)
        self.persona_state_service = FakePersonaService()
        self.data_lock = asyncio.Lock()


class FakeOrchestrator:
    def __init__(self, plugin):
        self.plugin = plugin


class FakeContext:
    def __init__(self, extra=None, trigger=None):
        self.session_id = UMO
        self.extra = extra or {}
        self.trigger = trigger
        self.started_at = 1_700_000_000.0
        self.abort_reason = ""

    def abort(self, reason):
        self.abort_reason = reason


class TestIntentStage:
    def test_silent_reschedules(self):
        decision = {
            "intent": "silent",
            "intent_label": "暂不发言",
            "brief": "",
            "state": "awaiting_reply",
            "silent": True,
        }
        stage = IntentStage(FakeOrchestrator(FakeIntentPlugin(decision)))
        ctx = FakeContext()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.RESCHEDULE
        assert ctx.abort_reason == "intent_silent"
        assert ctx.extra["retry_after_seconds"] >= 600

    def test_normal_intent_continues(self):
        decision = {
            "intent": "care",
            "intent_label": "关心近况",
            "brief": "轻一点",
            "state": "open_thread",
            "silent": False,
        }
        stage = IntentStage(FakeOrchestrator(FakeIntentPlugin(decision)))
        ctx = FakeContext()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        assert ctx.extra["intent"] == "care"


class Plain:
    pass


class Media:
    pass


class FakeGuardService:
    def __init__(self, result):
        self.result = result

    async def review(self, *args, **kwargs):
        return self.result


class FakeGuardPlugin:
    def __init__(self, result):
        self.session_data = {UMO: {}}
        self.config_service = FakeConfigService()
        self.session_service = FakeSessionService()
        self.storage_service = FakeStorage()
        self.guard_service = FakeGuardService(result)


class TestGuardStage:
    def test_blocked_reschedules(self):
        plugin = FakeGuardPlugin(
            {
                "allowed": False,
                "text": "bad",
                "reasons": ["repeated"],
                "rewritten": False,
                "guard": {"passed": False},
            }
        )
        stage = GuardStage(FakeOrchestrator(plugin))
        ctx = FakeContext({"response_text": "bad", "result_chain": []})
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.RESCHEDULE
        assert ctx.extra["retry_after_seconds"] == 900

    def test_rewritten_text_keeps_media(self):
        plugin = FakeGuardPlugin(
            {
                "allowed": True,
                "text": "改写后的自然句子",
                "reasons": ["repeated"],
                "rewritten": True,
                "guard": {"passed": True, "rewritten": True},
            }
        )
        stage = GuardStage(FakeOrchestrator(plugin))
        ctx = FakeContext(
            {"response_text": "原文", "result_chain": [Plain(), Media()]}
        )
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        assert ctx.extra["response_text"] == "改写后的自然句子"
        assert len(ctx.extra["result_chain"]) == 1
        assert isinstance(ctx.extra["result_chain"][0], Media)


class DeliveryPlugin:
    def __init__(self):
        self.config_service = FakeConfigService()


class TestDeliveryPlanStage:
    def test_builds_plan(self):
        stage = DeliveryPlanStage(FakeOrchestrator(DeliveryPlugin()))
        ctx = FakeContext(
            {
                "response_text": (
                    "刚下班，路过那家店，突然想起你上次说想吃。"
                    "我就顺手买了一份，结果走到一半才想起来你可能还没下班。"
                ),
                "result_chain": [],
            }
        )
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        plan = ctx.extra["delivery_plan"]
        assert 2 <= len(plan["bubbles"]) <= 3
