"""LLM 话题判定测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


from astrbot_plugin_proactive_chat.core.services.topic_judge import (
    TopicJudgeService,
    TopicJudgeSettings,
)

UMO = "qq_official:FriendMessage:TOPIC1"


class FakeSessionService:
    def get_session_log_str(self, umo, config=None):
        return umo


class FakeLLMService:
    def __init__(self, provider=None):
        self.provider = provider

    async def resolve_chat_provider(self, session_id):
        return self.provider


class FakeConfigService:
    def get_private_settings(self):
        return {"pause_settings": {"resume_mode": "llm_topic"}}


class FakePlugin:
    def __init__(self, with_provider=True):
        self.session_data = {UMO: {"unanswered_count": 4}}
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService()
        self.config = {"private_settings": {"pause_settings": {"resume_mode": "llm_topic"}}}
        self.llm_service = FakeLLMService(
            provider=object() if with_provider else None
        )
        self.context = SimpleNamespace()
        self.context_service = None
        self.topic_judge_service = TopicJudgeService(self)


def config(**overrides):
    pause = {"resume_mode": "llm_topic"}
    pause.update(overrides)
    return {"pause_settings": pause}


def test_any_message_mode_resumes_without_llm():
    plugin = FakePlugin(with_provider=False)
    called = {"n": 0}

    async def fake_invoke(provider, req):
        called["n"] += 1
        return SimpleNamespace(completion_text='{"new_topic": false, "reason": "x"}')

    import astrbot_plugin_proactive_chat.core.services.topic_judge as module

    original = module.invoke_provider
    module.invoke_provider = fake_invoke
    try:
        should, result = asyncio.run(
            plugin.topic_judge_service.should_resume(
                UMO, "随便聊聊", config(resume_mode="any_message")
            )
        )
    finally:
        module.invoke_provider = original

    assert should is True
    assert result["mode"] == "any_message"
    assert called["n"] == 0


def test_llm_true_resumes(monkeypatch):
    plugin = FakePlugin()

    async def fake_invoke(provider, req):
        assert "我们聊聊新游戏吧" in req.prompt
        return SimpleNamespace(
            completion_text='{"new_topic": true, "reason": "用户主动开启新话题"}'
        )

    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.topic_judge.invoke_provider",
        fake_invoke,
    )
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(UMO, "我们聊聊新游戏吧", config())
    )
    assert should is True
    assert result["new_topic"] is True
    assert "新话题" in result["reason"]
    assert result["fallback"] is False


def test_llm_false_keeps_paused(monkeypatch):
    plugin = FakePlugin()

    async def fake_invoke(provider, req):
        return SimpleNamespace(
            completion_text='```json\n{"new_topic": false, "reason": "还在说刚才那件事"}\n```'
        )

    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.topic_judge.invoke_provider",
        fake_invoke,
    )
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(UMO, "你到底为什么不理我", config())
    )
    assert should is False
    assert result["new_topic"] is False


def test_invalid_response_falls_back_to_resume(monkeypatch):
    plugin = FakePlugin()

    async def fake_invoke(provider, req):
        return SimpleNamespace(completion_text="我不确定，随便吧")

    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.topic_judge.invoke_provider",
        fake_invoke,
    )
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(UMO, "在吗", config())
    )
    assert should is True
    assert result["fallback"] is True


def test_invalid_response_can_keep_paused(monkeypatch):
    plugin = FakePlugin()

    async def fake_invoke(provider, req):
        return SimpleNamespace(completion_text="not json")

    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.topic_judge.invoke_provider",
        fake_invoke,
    )
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(
            UMO, "在吗", config(fallback_resume_on_error=False)
        )
    )
    assert should is False
    assert result["fallback"] is True


def test_timeout_uses_fallback(monkeypatch):
    plugin = FakePlugin()

    async def fake_invoke(provider, req):
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.topic_judge.invoke_provider",
        fake_invoke,
    )
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(
            UMO, "在吗", config(fallback_resume_on_error=False)
        )
    )
    assert should is False
    assert "timeout" in result["reason"]


def test_missing_provider_uses_fallback():
    plugin = FakePlugin(with_provider=False)
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(UMO, "在吗", config())
    )
    assert should is True
    assert result["fallback"] is True
    assert "provider_missing" in result["reason"]


def test_non_text_message_resumes():
    plugin = FakePlugin(with_provider=False)
    should, result = asyncio.run(
        plugin.topic_judge_service.should_resume(UMO, "", config())
    )
    assert should is True
    assert result["reason"] == "non_text_message"


class TestParseDecision:
    def test_json_bool(self):
        assert TopicJudgeService.parse_decision('{"new_topic": true}') == (True, "")
        assert TopicJudgeService.parse_decision('{"new_topic": false, "reason": "旧"}') == (False, "旧")

    def test_json_string_and_aliases(self):
        assert TopicJudgeService.parse_decision('{"is_new_topic": "yes", "reason": "新"}') == (True, "新")
        assert TopicJudgeService.parse_decision('{"topic_changed": "no"}') == (False, "")

    def test_code_fence(self):
        assert TopicJudgeService.parse_decision('```json\n{"new_topic": true}\n```') == (True, "")

    def test_plain_expression(self):
        assert TopicJudgeService.parse_decision("new_topic: false") == (False, "")

    def test_unparseable(self):
        assert TopicJudgeService.parse_decision("随便聊聊") is None

    def test_settings_defaults(self):
        settings = TopicJudgeSettings.from_dict({})
        assert settings.resume_mode == "llm_topic"
        assert settings.timeout_seconds == 10.0
        assert settings.fallback_resume_on_error is True

    def test_invalid_mode_falls_back(self):
        settings = TopicJudgeSettings.from_dict({"resume_mode": "whatever"})
        assert settings.resume_mode == "llm_topic"
