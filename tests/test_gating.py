"""GATE 阶段：私聊准入与互动强度概率测试。"""

from __future__ import annotations

import asyncio
import time

from astrbot_plugin_proactive_chat.core.domain.domain_enums import StageResult, TriggerSource
from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.features.gating_feature import GatingStage
from astrbot_plugin_proactive_chat.core.services.intensity_service import IntensityService

UMO = "qq_official:FriendMessage:GATE1"


class FakeSessionService:
    def parse_session_id(self, umo):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo):
        return umo

    def get_session_log_str(self, umo, config=None):
        return umo


class FakeConfigService:
    def __init__(self, settings):
        self.settings = settings

    def get_session_config(self, umo):
        return {
            **self.settings,
            "enable": True,
            "_session_type": "friend",
        }


class FakeSchedulerService:
    def __init__(self, plugin):
        self.plugin = plugin

    def is_session_paused(self, session_id):
        info = self.plugin.session_data.get(session_id) or {}
        return bool(info.get("proactive_paused"))

    def is_in_cooldown(self, session_id):
        info = self.plugin.session_data.get(session_id) or {}
        until = info.get("cooldown_until")
        return isinstance(until, (int, float)) and float(until) > time.time()

    def cooldown_remaining(self, session_id):
        info = self.plugin.session_data.get(session_id) or {}
        until = info.get("cooldown_until")
        if not isinstance(until, (int, float)):
            return 0.0
        return max(0.0, float(until) - time.time())

    async def pause_session(self, session_id, *, threshold=0, unanswered_count=0):
        info = self.plugin.session_data.setdefault(session_id, {})
        info["proactive_paused"] = True
        info["paused_threshold"] = threshold
        info["paused_unanswered_count"] = unanswered_count
        info.pop("next_trigger_time", None)
        return True


class FakePlugin:
    def __init__(self, intensity_settings):
        self.timezone = None
        self.session_data = {}
        self.config = {
            "private_settings": {
                "enable": True,
                "session_list": [UMO],
                "schedule_settings": {
                    "min_interval_minutes": 30,
                    "max_interval_minutes": 600,
                    "max_unanswered_times": 4,
                },
                "intensity_settings": intensity_settings,
            }
        }
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService(
            self.config["private_settings"]
        )
        self.scheduler_service = FakeSchedulerService(self)
        self.intensity_service = IntensityService(self)
        self.telemetry = None
        self.data_lock = asyncio.Lock()


class FakeOrchestrator:
    def __init__(self, plugin, allowed=True):
        self.plugin = plugin
        self.allowed = allowed

    async def is_chat_allowed(self, session_id):
        if not self.allowed:
            return False, "not_private"
        return True, "allowed"


class Context:
    def __init__(self, trigger=TriggerSource.SCHEDULE):
        self.session_id = UMO
        self.trigger = trigger
        self.unanswered_count = 0
        self.extra = {}
        self.abort_reason = ""

    def abort(self, reason):
        self.abort_reason = reason


def make_stage(probability_min, probability_max, **extra_settings):
    settings = {
        "enable": True,
        "probability_min": probability_min,
        "probability_max": probability_max,
        **extra_settings,
    }
    plugin = FakePlugin(settings)
    orchestrator = FakeOrchestrator(plugin)
    return GatingStage(orchestrator), plugin


class TestGating:
    def test_probability_zero_reschedules(self):
        stage, plugin = make_stage(0, 0)
        ctx = Context()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.RESCHEDULE
        assert ctx.abort_reason == "intensity_probability_skip"
        assert ctx.extra["retry_after_seconds"] >= 60

    def test_probability_hundred_continues(self):
        stage, plugin = make_stage(100, 100)
        ctx = Context()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        assert "intensity" in ctx.extra
        assert ctx.extra["intensity"]["probability_percent"] == 100

    def test_manual_trigger_bypasses_probability(self):
        stage, plugin = make_stage(0, 0)
        ctx = Context(trigger=TriggerSource.MANUAL)
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE

    def test_auto_trigger_can_disable_intensity(self):
        stage, plugin = make_stage(
            0, 0, apply_to_auto_trigger=False
        )
        ctx = Context(trigger=TriggerSource.AUTO_TRIGGER)
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE

    def test_max_unanswered_stops_and_pauses(self):
        stage, plugin = make_stage(100, 100)
        plugin.session_data[UMO] = {
            "unanswered_count": 4,
            "next_trigger_time": 123,
        }
        ctx = Context()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.STOP
        assert ctx.abort_reason == "proactive_paused"
        info = plugin.session_data[UMO]
        assert info["proactive_paused"] is True
        assert info["paused_threshold"] == 4
        assert info["paused_unanswered_count"] == 4
        assert "next_trigger_time" not in info

    def test_already_paused_stops_without_sending(self):
        stage, plugin = make_stage(100, 100)
        plugin.session_data[UMO] = {
            "unanswered_count": 2,
            "proactive_paused": True,
        }
        ctx = Context()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.STOP
        assert ctx.abort_reason == "proactive_paused"

    def test_non_private_stops(self):
        stage, plugin = make_stage(100, 100)
        stage.orchestrator.allowed = False
        ctx = Context()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.STOP
        assert ctx.abort_reason == "not_private"

    def test_decision_logged(self):
        stage, plugin = make_stage(0, 0)
        ctx = Context()
        asyncio.run(stage.handle(ctx))
        state = plugin.intensity_service.get_state(UMO)
        assert state["last_decision"]["will_send"] is False

    def test_negative_cooldown_reschedules(self):
        stage, plugin = make_stage(100, 100)
        plugin.session_data[UMO] = {
            "unanswered_count": 0,
            "cooldown_until": time.time() + 1800,
        }
        ctx = Context()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.RESCHEDULE
        assert ctx.abort_reason == "negative_cooldown"
        assert ctx.extra["retry_after_seconds"] > 0
