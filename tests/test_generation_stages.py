"""P2 多阶段生成：planner / writer / critic 测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.domain.domain_enums import StageResult
from astrbot_plugin_proactive_chat.core.features.critic_feature import CriticStage
from astrbot_plugin_proactive_chat.core.features.plan_feature import PlanStage
from astrbot_plugin_proactive_chat.core.services.generation_service import (
    CriticService,
    PlannerService,
)

UMO = "qq_official:FriendMessage:GEN1"
SETTINGS = {
    "humanization_settings": {
        "generation_settings": {
            "planner_enabled": True,
            "critic_enabled": True,
            "planner_timeout_seconds": 5,
            "critic_timeout_seconds": 5,
        }
    }
}


class FakeConfigService:
    def get_session_config(self, session_id):
        return SETTINGS


class FakePlanner:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def plan(self, session_id, *, context_text, session_config=None):
        self.calls += 1
        return self.result


class FakeCritic:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def review(self, session_id, text, *, context_text, session_config=None):
        self.calls += 1
        return self.result


class FakePlugin:
    def __init__(self):
        self.config_service = FakeConfigService()
        self.session_data = {UMO: {}}
        self.planner_service = FakePlanner(
            {"topic": "面试", "goal": "关心", "tone": "轻松", "avoid": "追问"}
        )
        self.critic_service = FakeCritic(
            {"enabled": True, "allowed": True, "text": "好的", "rewritten": False}
        )


class FakeOrchestrator:
    def __init__(self, plugin):
        self.plugin = plugin


class FakeContext:
    def __init__(self, extra=None):
        self.session_id = UMO
        self.extra = extra or {}
        self.started_at = 1_700_000_000.0
        self.abort_reason = ""

    def abort(self, reason):
        self.abort_reason = reason


class TestPlanStage:
    def test_plan_injected(self):
        plugin = FakePlugin()
        stage = PlanStage(FakeOrchestrator(plugin))
        ctx = FakeContext({"humanization": {"intent_label": "关心近况"}})
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        assert ctx.extra["plan"]["topic"] == "面试"
        assert ctx.extra["humanization"]["plan"]["goal"] == "关心"

    def test_planner_failure_degrades(self):
        plugin = FakePlugin()
        plugin.planner_service = FakePlanner(None)
        stage = PlanStage(FakeOrchestrator(plugin))
        ctx = FakeContext()
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        assert "plan" not in ctx.extra


class TestCriticStage:
    def test_reject_reschedules(self):
        plugin = FakePlugin()
        plugin.critic_service = FakeCritic(
            {"enabled": True, "allowed": False, "text": "bad", "reason": "太像客服"}
        )
        stage = CriticStage(FakeOrchestrator(plugin))
        ctx = FakeContext({"response_text": "bad", "result_chain": []})
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.RESCHEDULE
        assert ctx.abort_reason == "critic_rejected"
        assert plugin.session_data[UMO]["last_critic"]["allowed"] is False

    def test_rewrite_applied(self):
        plugin = FakePlugin()
        plugin.critic_service = FakeCritic(
            {
                "enabled": True,
                "allowed": True,
                "text": "刚看到一只很胖的猫",
                "rewritten": True,
            }
        )
        stage = CriticStage(FakeOrchestrator(plugin))
        ctx = FakeContext(
            {
                "response_text": "作为一个AI，我想你",
                "result_chain": [SimpleNamespace(), SimpleNamespace()],
            }
        )
        # 用类名模拟 Plain/Image 的区分。
        class Plain:
            pass

        class Image:
            pass

        ctx.extra["result_chain"] = [Plain(), Image()]
        result = asyncio.run(stage.handle(ctx))
        assert result is StageResult.CONTINUE
        assert ctx.extra["response_text"] == "刚看到一只很胖的猫"
        assert len(ctx.extra["result_chain"]) == 1


class FakeLlm:
    async def resolve_chat_provider(self, session_id):
        return SimpleNamespace()


async def _fake_invoke(provider, req):
    prompt = str(getattr(req, "prompt", ""))
    if "plan" in prompt or "规划" in prompt:
        return SimpleNamespace(
            completion_text='{"topic":"面试","goal":"关心","tone":"轻松","avoid":"追问"}'
        )
    return SimpleNamespace(
        completion_text='{"pass":false,"reason":"像客服","rewrite":"刚看到一只猫"}'
    )


def test_planner_service_parses_json(monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.generation_service.invoke_provider",
        _fake_invoke,
    )
    plugin = SimpleNamespace(
        config={"private_settings": SETTINGS},
        llm_service=FakeLlm(),
    )
    service = PlannerService(plugin)
    plan = asyncio.run(
        service.plan(UMO, context_text="意图：关心", session_config=SETTINGS)
    )
    assert plan["topic"] == "面试"
    assert plan["avoid"] == "追问"


def test_critic_service_parses_rewrite(monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.generation_service.invoke_provider",
        _fake_invoke,
    )
    plugin = SimpleNamespace(
        config={"private_settings": SETTINGS},
        llm_service=FakeLlm(),
    )
    service = CriticService(plugin)
    result = asyncio.run(
        service.review(
            UMO,
            "作为一个AI，我想你",
            context_text="上下文",
            session_config=SETTINGS,
        )
    )
    assert result["allowed"] is True
    assert result["rewritten"] is True
    assert result["text"] == "刚看到一只猫"
