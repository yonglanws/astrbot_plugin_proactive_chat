"""发送前自然度守卫测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.services.guard_service import GuardService

UMO = "qq_official:FriendMessage:GUARD1"
SETTINGS = {
    "humanization_settings": {
        "guard_settings": {
            "enable": True,
            "repeat_similarity": 0.65,
            "recent_message_count": 5,
            "max_message_chars": 60,
            "max_consecutive_questions": 2,
            "rewrite_on_fail": True,
        }
    }
}


class FakeLlmService:
    async def resolve_chat_provider(self, session_id):
        return SimpleNamespace(name="fake")


class FakePlugin:
    def __init__(self, recent=None, settings=None):
        self.session_data = {UMO: {"last_proactive_texts": list(recent or [])}}
        self.config = {
            "private_settings": settings or SETTINGS
        }
        self.llm_service = FakeLlmService()


class FakeResponse:
    def __init__(self, text):
        self.completion_text = text


def make_service(recent=None, settings=None):
    plugin = FakePlugin(recent, settings)
    return plugin, GuardService(plugin)


class TestChecks:
    def test_repeated_message_blocked(self):
        _, service = make_service(["刚下班，路过那家店，突然想起你"])
        reasons = service.check_text(
            UMO, "刚下班路过那家店，突然想起你", settings=service.get_settings()
        )
        assert "repeated" in reasons

    def test_too_long_blocked(self):
        _, service = make_service()
        reasons = service.check_text(UMO, "啊" * 100, settings=service.get_settings())
        assert "too_long" in reasons

    def test_question_density_blocked(self):
        _, service = make_service(["在干嘛？", "吃了吗？"])
        reasons = service.check_text(UMO, "今天忙不忙？", settings=service.get_settings())
        assert "too_many_questions" in reasons

    def test_ai_self_reference_blocked(self):
        _, service = make_service()
        reasons = service.check_text(
            UMO, "作为一个AI，我很想你", settings=service.get_settings()
        )
        assert "ai_self_reference_or_neediness" in reasons

    def test_clean_message_passes(self):
        _, service = make_service()
        reasons = service.check_text(
            UMO, "刚看到一只很胖的猫，走两步就坐下了", settings=service.get_settings()
        )
        assert reasons == []


class TestReview:
    def test_clean_message_allowed(self, monkeypatch):
        _, service = make_service()
        result = asyncio.run(service.review(UMO, "刚看到一只很胖的猫"))
        assert result["allowed"] is True
        assert result["rewritten"] is False

    def test_blocked_without_rewrite(self, monkeypatch):
        settings = {
            "humanization_settings": {
                "guard_settings": {**SETTINGS["humanization_settings"]["guard_settings"], "rewrite_on_fail": False}
            }
        }
        _, service = make_service(["在干嘛？", "吃了吗？"], settings)
        result = asyncio.run(service.review(UMO, "今天忙不忙？"))
        assert result["allowed"] is False
        assert "too_many_questions" in result["reasons"]

    def test_rewrite_once(self, monkeypatch):
        monkeypatch.setattr(
            "astrbot_plugin_proactive_chat.core.services.guard_service.invoke_provider",
            lambda provider, req: _async_value(FakeResponse("刚看到一只很胖的猫，走两步就坐下了")),
        )
        _, service = make_service(["在干嘛？", "吃了吗？"])
        result = asyncio.run(service.review(UMO, "今天忙不忙？"))
        assert result["allowed"] is True
        assert result["rewritten"] is True
        assert "猫" in result["text"]

    def test_record_sent_text_keeps_history(self):
        _, service = make_service()
        service.record_sent_text(UMO, "第一句")
        service.record_sent_text(UMO, "第二句")
        assert service.recent_texts(UMO) == ["第一句", "第二句"]


async def _async_value(value):
    return value
