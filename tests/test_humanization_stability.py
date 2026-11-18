"""稳定性与拟人化升级回归测试。"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from types import SimpleNamespace

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot_plugin_proactive_chat.core.domain.conversation_memory import (
    format_open_loops,
    prune_loops,
)
from astrbot_plugin_proactive_chat.core.domain.delivery_report import DeliveryReport
from astrbot_plugin_proactive_chat.core.domain.domain_enums import (
    HookPoint,
    StageResult,
)
from astrbot_plugin_proactive_chat.core.features.critic_feature import CriticStage
from astrbot_plugin_proactive_chat.core.features.deliver.split_delivery import (
    DeliverStage,
)
from astrbot_plugin_proactive_chat.core.features.delivery_plan_feature import (
    DeliveryPlanStage,
)
from astrbot_plugin_proactive_chat.core.features.feature_manifest import (
    build_default_features,
)
from astrbot_plugin_proactive_chat.core.features.record.history_writer import (
    RecordStage,
)
from astrbot_plugin_proactive_chat.core.pipeline.feature_registry import (
    FeatureRegistry,
)
from astrbot_plugin_proactive_chat.core.pipeline.hook_registry import HookRegistry
from astrbot_plugin_proactive_chat.core.services.conversation_memory_service import (
    ConversationMemoryService,
)
from astrbot_plugin_proactive_chat.core.services.conversation_state_service import (
    ConversationStateService,
)
from astrbot_plugin_proactive_chat.core.services.guard_service import GuardService
from astrbot_plugin_proactive_chat.core.services.llm_service import LlmService
from astrbot_plugin_proactive_chat.core.services.persona_state_service import (
    PersonaStateService,
)
from astrbot_plugin_proactive_chat.core.services.rhythm_service import RhythmService
from astrbot_plugin_proactive_chat.core.services.sender_service import SenderService

UMO = "qq_official:FriendMessage:HUM1"


def test_persona_state_repeated_read_idempotent_and_read_only():
    now = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc).timestamp()
    plugin = SimpleNamespace(
        timezone=timezone.utc,
        session_data={
            UMO: {
                "unanswered_count": 3,
                "conversation_state": {"last_user_quality": "negative"},
            }
        },
        config={
            "private_settings": {
                "humanization_settings": {
                    "persona_settings": {
                        "enable": True,
                        "baseline_energy": 0.6,
                    }
                }
            }
        },
    )
    service = PersonaStateService(plugin)

    # read_only=True 不应向 session_data 写入新字段。
    ro_snap = service.snapshot(UMO, now=now, read_only=True)
    assert ro_snap["energy"] > 0.3
    assert "persona_state" not in plugin.session_data[UMO]
    assert service.probability_multiplier(UMO, now=now, read_only=True) == 1.0
    assert "persona_state" not in plugin.session_data[UMO]

    # 连续读取 100 次 snapshot()，精力不会在重复读取中被吸干到 0。
    first = service.snapshot(UMO, now=now)["energy"]
    for i in range(100):
        snap = service.snapshot(UMO, now=now + i * 0.1)
    assert snap["energy"] > 0.5
    assert abs(snap["energy"] - first) < 0.05


def test_read_only_snapshots_do_not_mutate_session_data():
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    plugin = SimpleNamespace(
        timezone=timezone.utc,
        session_data={},
        config={"private_settings": {"humanization_settings": {}}},
    )
    state_svc = ConversationStateService(plugin)
    rhythm_svc = RhythmService(plugin)
    mem_svc = ConversationMemoryService(plugin)

    state_svc.snapshot(UMO, now=now, read_only=True)
    rhythm_svc.snapshot(UMO, now=now, read_only=True)
    mem_svc.snapshot(UMO, now=now, read_only=True)
    assert plugin.session_data == {}


def test_guard_consecutive_questions_and_repetitive_opener():
    plugin = SimpleNamespace(
        session_data={
            UMO: {
                "last_proactive_texts": ["在忙吗？", "吃了吗？"],
                "conversation_state": {
                    "last_user_at": 100.0,
                    "last_proactive_at": 200.0,
                },
            }
        },
        config={
            "private_settings": {
                "humanization_settings": {
                    "guard_settings": {
                        "enable": True,
                        "max_consecutive_questions": 2,
                        "repeat_similarity": 0.9,
                    }
                }
            }
        },
    )
    service = GuardService(plugin)

    # 用户尚未回复，且最近两条都是提问：新提问应被拦截。
    reasons = service.check_text(UMO, "今天加班累不累？")
    assert "too_many_questions" in reasons

    # 套路化开场白（最近发过“在忙吗？”，再发“在干嘛呢”）应触发 repetitive_opener。
    opener_reasons = service.check_text(UMO, "在干嘛呢")
    assert "repetitive_opener" in opener_reasons

    # 用户回复在最后一次主动消息之后，连续提问计数视为已重置。
    plugin.session_data[UMO]["conversation_state"]["last_user_at"] = 300.0
    after_reply = service.check_text(UMO, "今天加班累不累？")
    assert "too_many_questions" not in after_reply


def test_critic_guard_validation_and_delivery_plan_preserves_rewrite():
    class FakeConfigService:
        @staticmethod
        def get_session_config(session_id):
            return {
                "enable": True,
                "tts_settings": {"enable_tts": False},
                "humanization_settings": {
                    "guard_settings": {"enable": True, "max_message_chars": 60},
                    "delivery_plan_settings": {"enable": False},
                },
            }

    class FakeLlmService:
        @staticmethod
        async def dispatch_llm_response_hooks(event, response):
            return False

        @staticmethod
        def extract_response_text(response):
            return getattr(response, "completion_text", "")

        @staticmethod
        def extract_response_chain(response):
            rc = getattr(response, "result_chain", None)
            return list(getattr(rc, "chain", []) or [])

    class FakeCritic:
        def __init__(self, rewritten_text):
            self.rewritten_text = rewritten_text

        async def review(self, session_id, text, *, context_text, session_config=None):
            return {
                "enabled": True,
                "allowed": True,
                "text": self.rewritten_text,
                "rewritten": True,
            }

    plugin = SimpleNamespace(
        config={"private_settings": {}},
        session_data={UMO: {}},
        data_lock=asyncio.Lock(),
        session_service=SimpleNamespace(normalize_session_id=lambda s: s),
        config_service=FakeConfigService(),
        guard_service=GuardService(
            SimpleNamespace(config={"private_settings": {}}, session_data={UMO: {}})
        ),
        llm_service=FakeLlmService(),
        critic_service=FakeCritic("作为一个AI，我只是想关心你。"),
    )
    orchestrator = SimpleNamespace(plugin=plugin)

    # 1) Critic 改写若违反自然度守卫硬规则，CriticStage 必须拦截并 RESCHEDULE。
    critic_stage = CriticStage(orchestrator)
    ctx_blocked = SimpleNamespace(
        session_id=UMO,
        started_at=100.0,
        extra={"response_text": "你好", "result_chain": [], "humanization": {}},
        abort_reason="",
        abort=lambda r: setattr(ctx_blocked, "abort_reason", r),
    )
    res_blocked = asyncio.run(critic_stage.handle(ctx_blocked))
    assert res_blocked is StageResult.RESCHEDULE
    assert ctx_blocked.abort_reason == "critic_rewrite_guard_blocked"

    # 2) Critic 合法改写后，DeliveryPlanStage 派发钩子（未改写）不得还原旧 Plain 文本。
    plugin.critic_service = FakeCritic("刚下班，路过看到一只小黑猫")
    llm_resp = SimpleNamespace(
        completion_text="原始生硬文本",
        result_chain=MessageChain([Plain("原始生硬文本")]),
    )
    ctx_ok = SimpleNamespace(
        session_id=UMO,
        event=None,
        started_at=100.0,
        extra={
            "response_text": "原始生硬文本",
            "result_chain": [Plain("原始生硬文本")],
            "llm_response": llm_resp,
            "humanization": {},
        },
        abort_reason="",
        abort=lambda r: setattr(ctx_ok, "abort_reason", r),
    )
    assert asyncio.run(critic_stage.handle(ctx_ok)) is StageResult.CONTINUE
    assert ctx_ok.extra["response_text"] == "刚下班，路过看到一只小黑猫"
    assert llm_resp.completion_text == "刚下班，路过看到一只小黑猫"

    delivery_stage = DeliveryPlanStage(orchestrator)
    assert asyncio.run(delivery_stage.handle(ctx_ok)) is StageResult.CONTINUE
    assert ctx_ok.extra["response_text"] == "刚下班，路过看到一只小黑猫"
    assert ctx_ok.extra["delivery_plan"]["bubbles"][0]["text"] == "刚下班，路过看到一只小黑猫"


def test_partial_delivery_report_records_only_delivered_bubbles_and_no_duplicate_reschedule():
    sent_chains = []
    finalized = []

    class FakeConfigService:
        @staticmethod
        def get_session_config(session_id):
            return {
                "enable": True,
                "tts_settings": {"enable_tts": False},
                "segmented_reply_settings": {"enable": False},
            }

    plugin = SimpleNamespace(
        _terminating=False,
        last_message_times={},
        config_service=FakeConfigService(),
        session_service=SimpleNamespace(get_session_log_str=lambda s, c=None: s),
        telemetry=None,
    )
    sender = SenderService(plugin)

    async def fake_decorate(event, components):
        return components, True

    async def fake_send_chain(session_id, event, components):
        sent_chains.append([getattr(c, "text", "") for c in components])
        # 在第一条气泡送出后模拟用户发来新消息。
        plugin.last_message_times[UMO] = 200.0
        return True

    async def noop_persist(*args, **kwargs):
        return None

    sender.run_decorating_hooks = fake_decorate
    sender.send_chain = fake_send_chain
    sender.persist_proactive_message_to_platform_history = noop_persist
    plugin.sender_service = sender

    async def fake_finalize(session_id, conv_id, assistant_response, unanswered_count, **kwargs):
        finalized.append((session_id, assistant_response, unanswered_count))

    orchestrator = SimpleNamespace(plugin=plugin, finalize_and_reschedule=fake_finalize)
    deliver_stage = DeliverStage(orchestrator)
    record_stage = RecordStage(orchestrator)

    ctx = SimpleNamespace(
        session_id=UMO,
        conversation_id="conv_partial",
        unanswered_count=1,
        started_at=100.0,
        start_state={"last_message_time": 50.0},
        event=SimpleNamespace(result=None),
        extra={
            "response_text": "第一条已送达\n第二条未送达",
            "result_chain": [],
            "delivery_plan": {
                "bubbles": [
                    {"text": "第一条已送达", "delay_seconds": 0},
                    {"text": "第二条未送达", "delay_seconds": 0},
                ],
                "use_chain": False,
                "voice": False,
            },
            "intent": "share_self",
        },
        abort=lambda r: None,
    )

    result = asyncio.run(deliver_stage.handle(ctx))
    assert result is StageResult.CONTINUE
    report = ctx.extra["delivery_report"]
    assert isinstance(report, DeliveryReport)
    assert report.any_sent is True
    assert report.aborted_midway is True
    assert report.delivered_bubbles == ["第一条已送达"]
    assert ctx.extra["delivered_text"] == "第一条已送达"
    assert sent_chains == [["第一条已送达"]]

    asyncio.run(record_stage.handle(ctx))
    assert finalized == [(UMO, "第一条已送达", 1)]


def test_hook_registry_runs_context_before_intent_and_signoff_downweights_continue():
    assert HookRegistry.ORDER.index(HookPoint.CONTEXT) < HookRegistry.ORDER.index(
        HookPoint.INTENT
    )
    plugin = SimpleNamespace(
        config={"private_settings": {}},
        session_data={},
    )
    registry = FeatureRegistry(config={})
    registry.register_features(build_default_features(SimpleNamespace(plugin=plugin)))
    ordered_names = [s.name for s in registry.hooks.ordered_stages()]
    assert ordered_names.index("context_stage") < ordered_names.index("intent_stage")

    now = 1_700_000_000.0
    state_svc = ConversationStateService(
        SimpleNamespace(
            timezone=timezone.utc,
            session_data={
                UMO: {
                    "conversation_state": {
                        "state": "open_thread",
                        "last_user_at": now - 600,
                    }
                }
            },
            config={"private_settings": {"humanization_settings": {"state_settings": {"silent_intent_enabled": False}}}},
        )
    )
    without_signoff = state_svc.choose_intent(
        UMO,
        recent_history=[{"role": "user", "content": "那个电影确实挺有意思的"}],
        now=now,
        rng=random.Random(1),
    )
    with_signoff = state_svc.choose_intent(
        UMO,
        recent_history=[{"role": "user", "content": "晚安，先睡啦，明天聊"}],
        now=now,
        rng=random.Random(1),
    )
    assert with_signoff["weights"]["continue_topic"] < without_signoff["weights"]["continue_topic"] * 0.5


def test_open_loops_due_hint_expiry_and_seed_from_structured_history():
    now = 1_700_000_000.0
    loop = {
        "kind": "plan",
        "text": "我明天要去面试",
        "due_hint": "明天",
        "created_at": now - 3 * 86400,
        "status": "open",
    }
    # 超过 due_days(2.0) 但未到 due_days + 2.5：提示已结束。
    formatted = format_open_loops([loop], now=now)
    assert "近期事件（可能已结束，可自然问结果）" in formatted

    # 超过 due_days + 2.5(4.5 天)：自动关闭。
    pruned = prune_loops([dict(loop)], now=now + 2 * 86400, ttl_days=7, max_open=8)
    assert pruned[0]["status"] == "closed"
    assert pruned[0]["close_reason"] == "due_passed"

    # seed_from_history 支持结构化列表内容（而非转成 Python dict repr 字符串）。
    mem_plugin = SimpleNamespace(session_data={}, config={"private_settings": {}})
    mem_svc = ConversationMemoryService(mem_plugin)
    mem_svc.seed_from_history(
        UMO,
        [
            {"role": "user", "content": [{"type": "text", "text": "我明天有个面试"}]},
        ],
        now=now,
    )
    loops = mem_svc.get_state(UMO)["open_loops"]
    assert loops
    assert "[{" not in loops[0]["text"]
    assert "面试" in loops[0]["text"]


def test_llm_service_single_pass_placeholders_and_no_duplication(monkeypatch):
    captured = {}

    class FakeProvider:
        pass

    async def fake_resolve(self, session_id):
        return FakeProvider()

    async def fake_invoke(provider, req):
        captured["req"] = req
        return SimpleNamespace(
            completion_text="你好呀",
            result_chain=MessageChain([Plain("你好呀")]),
        )

    monkeypatch.setattr(LlmService, "resolve_chat_provider", fake_resolve)
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.llm_service.invoke_provider",
        fake_invoke,
    )

    plugin = SimpleNamespace(
        timezone=timezone.utc,
        telemetry=None,
        context_service=SimpleNamespace(
            sanitize_history_content=lambda h: h,
            make_temp_text_part=lambda text: text,
        ),
        intensity_service=None,
    )
    service = LlmService(plugin)
    template = "时间 {{current_time}}\n{{memory_block}}\n{{topic_ledger}}"
    # 若 memory_context 自身含 {{current_time}} 字面量，单次正则替换绝不应二次展开。
    memory_literal = "<long_term_memory>用户提到 {{current_time}} 占位符</long_term_memory>"
    topic_literal = "<topic_ledger>面试</topic_ledger>"

    asyncio.run(
        service.generate_llm_response(
            UMO,
            {"proactive_prompt": template},
            [],
            "system",
            0,
            memory_context=memory_literal,
            topic_context=topic_literal,
        )
    )
    req = captured["req"]
    assert "{{current_time}}" in req.prompt
    assert all(
        memory_literal not in str(p) and topic_literal not in str(p)
        for p in req.extra_user_content_parts
    )
