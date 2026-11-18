"""拟人化纯领域逻辑测试：分类、相似度、发送计划与配置解析。"""

from __future__ import annotations

import random

from astrbot_plugin_proactive_chat.core.domain.humanization import (
    DeliveryPlanSettings,
    HumanizationSettings,
    Intent,
    ReplyQuality,
    build_delivery_plan,
    classify_reply,
    has_question,
    normalize_for_similarity,
    persona_energy_curve,
    text_similarity,
)


class TestReplyQuality:
    def test_negative_feedback(self):
        for text in ("别发了", "在忙，不要打扰", "stop messaging me"):
            assert classify_reply(text) is ReplyQuality.NEGATIVE
        # 否定“忙/没空”的反例不应误判为负反馈。
        assert classify_reply("之前一直没空，现在好了") is ReplyQuality.SUBSTANTIVE

    def test_perfunctory(self):
        for text in ("嗯", "哦哦", "好", "666", "收到啦", "好滴", "😂", "……"):
            assert classify_reply(text) is ReplyQuality.PERFUNCTORY

    def test_question(self):
        assert classify_reply("你明天有空吗？") is ReplyQuality.QUESTION
        assert classify_reply("为什么突然找我") is ReplyQuality.QUESTION
        # 非疑问语气的“呢/怎么”不应误判为提问。
        assert has_question("太累了呢") is False
        assert has_question("不知道怎么选") is False

    def test_substantive(self):
        assert classify_reply("刚看完那部电影，结尾有点意外") is ReplyQuality.SUBSTANTIVE
        assert classify_reply("到了") is ReplyQuality.SUBSTANTIVE
        assert classify_reply("晚安") is ReplyQuality.SUBSTANTIVE

    def test_media(self):
        assert classify_reply("", has_media=True) is ReplyQuality.MEDIA


class TestSimilarity:
    def test_similar_messages_detected(self):
        left = "刚下班，路过那家店，突然想起你上次说想吃"
        right = "刚下班路过那家店，突然想起你上次说想吃"
        assert text_similarity(left, right) > 0.8

    def test_different_messages_low(self):
        assert text_similarity("今天天气不错", "我刚吃完晚饭") < 0.4

    def test_normalization_removes_punctuation(self):
        assert normalize_for_similarity("你好，世界！") == "你好世界"


class TestDeliveryPlan:
    def test_short_text_single_bubble(self):
        plan = build_delivery_plan(
            "在忙吗",
            DeliveryPlanSettings(),
            rng=random.Random(1),
        )
        assert len(plan.bubbles) == 1
        assert plan.bubbles[0].text == "在忙吗"

    def test_long_text_split_into_bubbles(self):
        text = (
            "刚下班，路过那家店。突然想起你上次说想吃那家的松饼。"
            "我就顺手买了一份，结果走到一半才想起来你可能还没下班。"
        )
        plan = build_delivery_plan(
            text,
            DeliveryPlanSettings(max_bubbles=3, bubble_length_threshold=30),
            rng=random.Random(7),
        )
        assert 2 <= len(plan.bubbles) <= 3
        # <=22 字的短口语气泡会去掉末尾生硬句号，但完整内容应全部保留。
        assert "".join(bubble.text.rstrip("。") for bubble in plan.bubbles).replace(
            " ", ""
        ) == text.replace("。", "").replace(" ", "")

    def test_explicit_newline_splits_short_message(self):
        plan = build_delivery_plan(
            "刚下班\n今天好累呀！",
            DeliveryPlanSettings(max_bubbles=3, bubble_length_threshold=60),
            rng=random.Random(2),
        )
        assert [b.text for b in plan.bubbles] == ["刚下班", "今天好累呀！"]

    def test_voice_probability(self):
        settings = DeliveryPlanSettings(voice_probability=1.0)
        plan = build_delivery_plan("晚安", settings, voice_allowed=True)
        assert plan.voice is True

    def test_voice_skips_url_code_and_emoji(self):
        settings = DeliveryPlanSettings(voice_probability=1.0)
        assert build_delivery_plan("看这个 https://example.com", settings, voice_allowed=True).voice is False
        assert build_delivery_plan("```py\nprint(1)\n```", settings, voice_allowed=True).voice is False
        assert build_delivery_plan("😂😂😂", settings, voice_allowed=True).voice is False

    def test_rich_chain(self):
        plan = build_delivery_plan(
            "",
            DeliveryPlanSettings(),
            has_rich_chain=True,
            rng=random.Random(1),
        )
        assert plan.use_chain is True


class TestSettings:
    def test_defaults_and_overrides(self):
        settings = HumanizationSettings.from_dict(
            {
                "rhythm_settings": {
                    "daily_proactive_limit": 3,
                    "min_gap_minutes": 20,
                },
                "state_settings": {"silent_intent_enabled": False},
                "memory_integration_settings": {"top_k": 2},
            }
        )
        assert settings.rhythm.daily_proactive_limit == 3
        assert settings.rhythm.min_gap_minutes == 20
        assert settings.state.silent_intent_enabled is False
        assert settings.memory.top_k == 2

    def test_invalid_values_clamped(self):
        settings = HumanizationSettings.from_dict(
            {
                "guard_settings": {"repeat_similarity": 5},
                "delivery_plan_settings": {"max_bubbles": 99},
            }
        )
        assert settings.guard.repeat_similarity == 1.0
        assert settings.delivery.max_bubbles == 6

    def test_energy_curve(self):
        assert persona_energy_curve(3) < persona_energy_curve(15)
        assert 0 <= persona_energy_curve(23) <= 1

    def test_intent_values(self):
        assert Intent.SILENT.value == "silent"
        assert has_question("吃了吗？") is True


def test_schema_contains_humanization_section():
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    root = schema["private_settings"]["items"]
    assert "humanization_settings" in root
    items = root["humanization_settings"]["items"]
    for key in (
        "rhythm_settings",
        "state_settings",
        "feedback_settings",
        "guard_settings",
        "delivery_plan_settings",
        "persona_settings",
        "memory_integration_settings",
        "conversation_memory_settings",
        "generation_settings",
    ):
        assert key in items
