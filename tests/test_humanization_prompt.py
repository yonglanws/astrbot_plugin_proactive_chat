"""拟人化上下文与提示词注入测试。"""

from __future__ import annotations

from types import SimpleNamespace
from datetime import timezone

from astrbot_plugin_proactive_chat.core.features.context.conversation_source import ContextStage
from astrbot_plugin_proactive_chat.core.services.llm_service import LlmService

HUMANIZATION = {
    "intent": "care",
    "intent_label": "关心近况",
    "intent_brief": "关心对方最近提到的事。",
    "conversation_state": {
        "state": "open_thread",
        "state_label": "话题未结束",
        "reason": "recent_message",
        "last_user_at": 1_700_000_000,
        "last_proactive_at": 1_700_001_000,
    },
    "persona": {
        "enabled": True,
        "mood_label": "有点累",
        "energy_percent": 42,
        "activity": "在忙，但偶尔会看手机",
    },
    "rhythm": {
        "enabled": True,
        "daily_count": 1,
        "daily_limit": 6,
        "active_hours_label": "9-12、20-23",
    },
}


class FakePlugin:
    timezone = timezone.utc


def test_memory_query_uses_recent_history_and_state():
    history = [
        {"role": "user", "content": "我下周三有个面试"},
        {"role": "assistant", "content": "那要好好准备"},
        {"role": "user", "content": "有点紧张"},
    ]
    query = ContextStage.build_memory_query(history, HUMANIZATION)
    assert "面试" in query
    assert "关系状态：话题未结束" in query
    assert "本轮意图：关心近况" in query


def test_dynamic_context_contains_humanization():
    service = LlmService(FakePlugin())
    text = service.build_dynamic_context_text(
        1,
        humanization=HUMANIZATION,
        intensity_snapshot={"score": 55, "tier": "warm", "tier_label": "热络", "probability_percent": 70},
    )
    assert "话题未结束" in text
    assert "关心近况" in text
    assert "有点累" in text
    assert "42%" in text
    assert "9-12、20-23" in text
    assert "<dynamic_context>" in text


def test_prompt_placeholders_replaced():
    plugin = FakePlugin()
    plugin.context_service = SimpleNamespace(make_temp_text_part=lambda text: text)
    service = LlmService(plugin)
    template = "状态={{conversation_state}} 意图={{intent}} 精力={{persona_state}}"
    parts = service.build_extra_content_parts(
        platform_context="",
        prompt_template=template,
        unanswered_count=0,
        humanization=HUMANIZATION,
        memory_context="<long_term_memory>记忆</long_term_memory>",
    )
    joined = "\n".join(str(part) for part in parts)
    assert "话题未结束" in joined
    assert "关心近况" in joined
    assert "记忆" in joined
