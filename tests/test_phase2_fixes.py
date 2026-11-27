"""Phase 2 修复回归测试：气泡切分、贴纸清理、钩子配对、分片快照与零成本自然度守卫。"""

from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace

from astrbot.core.message.components import Plain

from astrbot_plugin_proactive_chat.core.adapters import astrbot_conversation
from astrbot_plugin_proactive_chat.core.domain.content_text import strip_sticker_tags
from astrbot_plugin_proactive_chat.core.domain.humanization import (
    DeliveryPlanSettings,
    _split_sentences,
    build_delivery_plan,
)
from astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator import FlowOrchestrator
from astrbot_plugin_proactive_chat.core.services.guard_service import GuardService
from astrbot_plugin_proactive_chat.core.services import sender_service as sender_mod
from astrbot_plugin_proactive_chat.core.services.sender_service import SenderService
from astrbot_plugin_proactive_chat.core.store.session_store import SessionStore

UMO = "qq_official:FriendMessage:PHASE2"


def test_split_and_delivery_plan_preserve_punct_clusters_and_merge_fragments():
    assert _split_sentences("真的吗！？我刚下班……好累啊") == [
        "真的吗！？",
        "我刚下班……",
        "好累啊",
    ]

    plan = build_delivery_plan(
        '喂！\n今天怎么样……\n……\n<sticker name="smile"/>……',
        DeliveryPlanSettings(max_bubbles=3, bubble_length_threshold=60),
        rng=random.Random(1),
    )
    texts = [b.text for b in plan.bubbles]
    assert len(texts) == 1
    assert "今天怎么样" in texts[0]
    assert "<sticker" in texts[0]

    # 长文本切分时也不能产生独立纯标点或纯贴纸气泡。
    long_plan = build_delivery_plan(
        '喂！今天加班到现在才回家，路过便利店买了热牛奶。<sticker name="cat"/>……然后一抬头发现月亮好圆。',
        DeliveryPlanSettings(max_bubbles=3, bubble_length_threshold=20),
        rng=random.Random(3),
    )
    for bubble in long_plan.bubbles:
        stripped = strip_sticker_tags(bubble.text)
        assert any(ch.isalnum() for ch in stripped), f"unexpected fragment bubble: {bubble.text!r}"


def test_sticker_tags_stripped_before_persisting_history():
    assert strip_sticker_tags('早安呀 <sticker name="wave"/> 今天记得吃早饭') == "早安呀 今天记得吃早饭"
    assert strip_sticker_tags('<sticker id="1"/>') == ""

    saved_histories: list[list[dict]] = []

    class FakeTextPart:
        def __init__(self, text: str):
            self.text = text

    class FakeAssistantMsg:
        def __init__(self, content):
            self.content = content

        def model_dump(self):
            return {"role": "assistant", "content": [{"text": p.text} for p in self.content]}

    class FakeDB:
        async def get_conversation_by_id(self, cid: str):
            return SimpleNamespace(content=list(saved_histories[-1]) if saved_histories else [])

        async def update_conversation(self, cid: str, content: list):
            saved_histories.append(list(content))

    class FakeConvMgr:
        db = FakeDB()

        async def get_conversation(self, session_id: str, cid: str):
            return SimpleNamespace(history=json.dumps(saved_histories[-1] if saved_histories else []))

    orig_msg = astrbot_conversation.AssistantMessageSegment
    orig_part = astrbot_conversation.TextPart
    astrbot_conversation.AssistantMessageSegment = FakeAssistantMsg
    astrbot_conversation.TextPart = FakeTextPart
    try:
        async def scenario():
            await astrbot_conversation.add_assistant_message(
                FakeConvMgr(), "c1", '今天好开心<sticker name="happy"/>呀'
            )
            ok = await astrbot_conversation.verify_message_persisted(
                FakeConvMgr(), UMO, "c1", '今天好开心<sticker name="happy"/>呀'
            )
            assert ok is True
            assert saved_histories[-1][-1]["content"][0]["text"] == "今天好开心呀"

            # 纯 sticker 响应直接跳过写入。
            before_len = len(saved_histories)
            await astrbot_conversation.add_assistant_message(
                FakeConvMgr(), "c1", '<sticker name="only"/>'
            )
            assert len(saved_histories) == before_len

        asyncio.run(scenario())
    finally:
        astrbot_conversation.AssistantMessageSegment = orig_msg
        astrbot_conversation.TextPart = orig_part


def test_sender_service_balances_decorating_and_after_sent_hooks(monkeypatch):
    hook_calls: list[str] = []

    class FakeEvent:
        def __init__(self):
            self._extras = {}
            self._result = None
            self._stopped = False

        def set_extra(self, k, v):
            self._extras[k] = v

        def get_extra(self, k, d=None):
            return self._extras.get(k, d)

        def set_result(self, r):
            self._result = r

        def get_result(self):
            return self._result

        def clear_result(self):
            self._result = None

        def is_stopped(self):
            return self._stopped

    async def fake_dispatch(event, hook_type, *args, **kwargs):
        hook_calls.append(str(hook_type))
        if hook_type == "DEC" and getattr(event, "_stop_on_dec", False):
            event._stopped = True
            return True
        return False

    monkeypatch.setattr(sender_mod, "EventType", SimpleNamespace(
        OnDecoratingResultEvent="DEC",
        OnAfterMessageSentEvent="AFTER",
    ))
    monkeypatch.setattr(sender_mod, "dispatch_event_hook", fake_dispatch)

    plugin = SimpleNamespace(
        _terminating=False,
        last_message_times={},
        config_service=SimpleNamespace(
            get_session_config=lambda _sid: {
                "enable": True,
                "tts_settings": {"enable_tts": False},
                "segmented_reply_settings": {"enable": False},
            }
        ),
        session_service=SimpleNamespace(get_session_log_str=lambda s, c=None: s),
        telemetry=None,
    )
    sender = SenderService(plugin)

    async def always_ok(*_a, **_kw):
        return True

    async def always_fail(*_a, **_kw):
        return False

    async def noop_persist(*_a, **_kw):
        return None

    sender.persist_proactive_message_to_platform_history = noop_persist

    # 1) 3 条气泡全部发出：3 次 DEC 严格对应 3 次 AFTER。
    hook_calls.clear()
    sender.send_chain = always_ok
    plan = {
        "bubbles": [
            {"text": "第一条", "delay_seconds": 0},
            {"text": "第二条", "delay_seconds": 0},
            {"text": "第三条", "delay_seconds": 0},
        ],
        "use_chain": False,
    }
    assert asyncio.run(sender.send_proactive_message(UMO, "123", event=FakeEvent(), delivery_plan=plan)) is True
    assert hook_calls.count("DEC") == 3
    assert hook_calls.count("AFTER") == 3

    # 2) 装饰钩子终止传播：仍需对称补发 1 次 AFTER。
    hook_calls.clear()
    stopped_ev = FakeEvent()
    stopped_ev._stop_on_dec = True
    assert asyncio.run(sender.send_proactive_message(UMO, "单条", event=stopped_ev)) is False
    assert hook_calls == ["DEC", "AFTER"]

    # 3) 发送失败：仍需对称补发 1 次 AFTER。
    hook_calls.clear()
    sender.send_chain = always_fail
    assert asyncio.run(sender.send_proactive_message(UMO, "单条", event=FakeEvent())) is False
    assert hook_calls == ["DEC", "AFTER"]


def test_session_store_chunked_snapshot_handles_large_dict(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path)
        live = {
            f"qq_official:FriendMessage:U{i}": {
                "unanswered_count": i,
                "intensity": {"events": [{"ts": i, "kind": "msg"}]},
            }
            for i in range(180)
        }

        async def mutator():
            for i in range(180, 220):
                live[f"qq_official:FriendMessage:U{i}"] = {"unanswered_count": i}
                await asyncio.sleep(0)

        ok, _ = await asyncio.gather(store.save(live), mutator())
        assert ok is True
        loaded = await store.load()
        assert "qq_official:FriendMessage:U0" in loaded
        assert "qq_official:FriendMessage:U179" in loaded

    asyncio.run(scenario())


def test_guard_service_blocks_cliches_and_leading_ellipsis_without_critic():
    plugin = SimpleNamespace(
        session_data={UMO: {"last_proactive_texts": []}},
        config={
            "private_settings": {
                "humanization_settings": {
                    "critic_settings": {"enable": False},
                    "guard_settings": {"enable": True, "rewrite_on_fail": False},
                }
            }
        },
        llm_service=None,
    )
    guard = GuardService(plugin)
    settings = guard.get_settings()

    for bad in ("记得多喝温水哦", "吃热乎的没？", "昨天你不是说要早睡吗"):
        reasons = guard.check_text(UMO, bad, settings=settings)
        assert "cliche_expression" in reasons

    reasons_ell = guard.check_text(UMO, "……\n今天天气不错", settings=settings)
    assert "leading_ellipsis" in reasons_ell

    res = asyncio.run(guard.review(UMO, "记得多喝温水"))
    assert res["allowed"] is False
