"""针对 P1 审计问题的回归单测。

覆盖 5 大修复项：
1. persona_state_service 移除硬编码的“准备睡了/在吃饭”作息诱导；
2. context_service 读取 session_overrides、包裹 <persona_instructions> 并在切卡时重置话题账本；
3. KV Prompt Cache 历史前缀不可变 + LivingMemory 单路径召回去重 + 全局并发 Semaphore 限流；
4. 永久性投递失败（无好友关系/无权限等）自动暂停会话并 STOP，避免无限重调度烧 LLM；
5. ProactiveMessageEvent 继承会话级 plugin_set + hybrid 模式去除重复 [系统任务] 模板。
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

from astrbot_plugin_proactive_chat.core.adapters import astrbot_event as event_mod
from astrbot_plugin_proactive_chat.core.domain.delivery_report import (
    DeliveryReport,
    is_permanent_delivery_error,
)
from astrbot_plugin_proactive_chat.core.domain.domain_enums import (
    StageResult,
    TriggerSource,
)
from astrbot_plugin_proactive_chat.core.domain.request_context import ProactiveContext
from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.features.context.conversation_source import (
    ContextStage,
)
from astrbot_plugin_proactive_chat.core.features.deliver.split_delivery import (
    DeliverStage,
)
from astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator import FlowOrchestrator
from astrbot_plugin_proactive_chat.core.services.context_service import (
    ContextService,
    DEFAULT_PLATFORM_HISTORY_PROMPT,
    HYBRID_PLATFORM_HISTORY_PROMPT,
)
from astrbot_plugin_proactive_chat.core.services.llm_service import LlmService
from astrbot_plugin_proactive_chat.core.services.persona_state_service import (
    PersonaStateService,
)
from astrbot_plugin_proactive_chat.core.services.scheduler_service import (
    PAUSE_FLAG,
    PAUSE_REASON,
    PAUSE_REASON_PERMANENT_SEND_ERROR,
    SchedulerService,
)
from astrbot_plugin_proactive_chat.core.services.sender_service import SenderService

UMO = "mzkbot:FriendMessage:2155762216"


class _FakeSessionService:
    def parse_session_id(self, umo):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo):
        return umo

    def build_session_key(self, umo):
        return SessionKey.parse(umo)

    def get_session_log_str(self, umo, config=None):
        return umo


# ---------------------------------------------------------------------------
# 1. persona_state_service: 移除硬编码睡/吃诱导
# ---------------------------------------------------------------------------
class TestPersonaStateNeutralActivity:
    def test_no_hardcoded_sleep_or_meal_across_all_hours(self):
        forbidden = ("准备睡了", "准备休息", "还没睡", "刚睡下", "吃饭", "吃点东西", "赖床")
        for is_weekend in (False, True):
            for hour in range(24):
                act = PersonaStateService._derive_activity(hour, is_weekend=is_weekend)
                assert act, f"empty activity for weekend={is_weekend} hour={hour}"
                for bad in forbidden:
                    assert bad not in act, (
                        f"unexpected hardcoded routine '{bad}' at "
                        f"weekend={is_weekend} hour={hour}: {act}"
                    )


# ---------------------------------------------------------------------------
# 2. 双人格拼接修复 + session_overrides + 切卡重置状态
# ---------------------------------------------------------------------------
class TestPersonaAlignmentAndReset:
    def test_wrap_persona_instructions_idempotent(self):
        raw = "你是25时KAITO。"
        wrapped = ContextService.wrap_persona_instructions(raw)
        assert wrapped == "\n<persona_instructions>\n你是25时KAITO。\n</persona_instructions>\n"
        assert ContextService.wrap_persona_instructions(wrapped) == wrapped

    def test_prepare_llm_request_prefers_session_overrides_and_wraps(self, monkeypatch):
        fake_overrides_mod = ModuleType("astrbot.core.session_overrides")

        async def _fake_get_overrides(umo: str):
            return {"agent.config.persona.persona_id": "25时KAITO"}

        fake_overrides_mod.get_overrides = _fake_get_overrides  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules, "astrbot.core.session_overrides", fake_overrides_mod
        )

        class FakeConvMgr:
            async def get_curr_conversation_id(self, umo):
                return "cid-1"

            async def get_conversation(self, umo, cid):
                return SimpleNamespace(cid=cid, persona_id="旧人格", history="[]")

        class FakePersonaMgr:
            async def get_persona(self, pid):
                prompts = {
                    "25时KAITO": "你是25时KAITO，深夜也会清醒。",
                    "旧人格": "你是旧人格。",
                }
                if pid in prompts:
                    return SimpleNamespace(persona_id=pid, system_prompt=prompts[pid])
                return None

            async def get_default_persona_v3(self, umo=None):
                return {"name": "default", "prompt": "默认人格"}

        plugin = SimpleNamespace(
            session_service=_FakeSessionService(),
            config_service=SimpleNamespace(
                get_session_config=lambda umo: {
                    "context_settings": {"source_mode": "conversation_history"}
                }
            ),
            context=SimpleNamespace(
                conversation_manager=FakeConvMgr(),
                persona_manager=FakePersonaMgr(),
            ),
            session_data={},
            timezone=timezone.utc,
            telemetry=None,
        )
        svc = ContextService(plugin)
        pkg = asyncio.run(svc.prepare_llm_request(UMO))
        assert pkg is not None
        assert pkg["persona_id"] == "25时KAITO"
        assert "<persona_instructions>" in pkg["system_prompt"]
        assert "</persona_instructions>" in pkg["system_prompt"]
        assert "25时KAITO" in pkg["system_prompt"]
        assert "旧人格" not in pkg["system_prompt"]

    def test_reset_session_state_on_persona_change(self):
        plugin = SimpleNamespace(
            session_data={
                UMO: {
                    "last_persona_id": "东云彰人",
                    "conversation_memory": {
                        "open_loops": [{"text": "明天练歌"}],
                        "recent_topics": [{"text": "街头演出"}],
                    },
                    "last_proactive_texts": ["练完歌了吗"],
                    "persona_state": {"energy": 0.4},
                    "rhythm": {"active_hours": [20, 21, 22]},
                }
            }
        )
        svc = ContextService(plugin)
        # 相同人格不重置
        assert svc.reset_session_state_on_persona_change(UMO, "东云彰人") is False
        assert "conversation_memory" in plugin.session_data[UMO]

        # 切换到新人格后重置旧人格话题与文本缓存，但保留用户作息
        assert svc.reset_session_state_on_persona_change(UMO, "25时KAITO") is True
        assert plugin.session_data[UMO]["last_persona_id"] == "25时KAITO"
        assert "conversation_memory" not in plugin.session_data[UMO]
        assert "last_proactive_texts" not in plugin.session_data[UMO]
        assert "persona_state" not in plugin.session_data[UMO]
        assert plugin.session_data[UMO]["rhythm"] == {"active_hours": [20, 21, 22]}


# ---------------------------------------------------------------------------
# 3. KV Prompt Cache + LivingMemory 单路径去重 + 并发 Semaphore 限流
# ---------------------------------------------------------------------------
class TestKvCacheMemoryDedupAndConcurrency:
    def test_history_prefix_immutable_for_kv_cache(self, monkeypatch):
        captured_req = {}

        async def fake_invoke(provider, req):
            captured_req["req"] = req
            return SimpleNamespace(completion_text="你好呀", result_chain=None)

        monkeypatch.setattr(
            "astrbot_plugin_proactive_chat.core.services.llm_service.invoke_provider",
            fake_invoke,
        )

        class FakeCtxSvc:
            @staticmethod
            def sanitize_history_content(history):
                return list(history)

            @staticmethod
            def make_temp_text_part(text):
                return text

        plugin = SimpleNamespace(
            timezone=timezone.utc,
            context_service=FakeCtxSvc(),
            telemetry=None,
            intensity_service=None,
            context=SimpleNamespace(
                get_using_provider=lambda umo=None: object(),
            ),
        )
        llm = LlmService(plugin)
        old_ts = time.time() - 7200
        history = [
            {"role": "user", "content": "刚写完曲子", "timestamp": old_ts},
            {"role": "assistant", "content": "听起来挺顺手", "timestamp": old_ts + 60},
        ]

        asyncio.run(
            llm.generate_llm_response(
                UMO,
                {"proactive_prompt": "发一条消息"},
                history,
                "\n<persona_instructions>\n测试人格\n</persona_instructions>\n",
                0,
                humanization={},
            )
        )
        req = captured_req["req"]
        # 历史正文绝对不能被注入 "[2小时前] " 之类的动态前缀
        assert req.contexts[0]["content"] == "刚写完曲子"
        assert req.contexts[1]["content"] == "听起来挺顺手"
        # 相对时间间隔放在尾部 extra_user_content_parts 的 <dynamic_context> 中
        extra_joined = "\n".join(str(p) for p in req.extra_user_content_parts)
        assert "<dynamic_context>" in extra_joined
        assert "距上一条历史消息" in extra_joined

    def test_livingmemory_hook_deduped_when_bridge_recalled(self, monkeypatch):
        called: list[str] = []

        async def lm_handler(event, req):
            called.append("livingmemory")

        async def persona_handler(event, req):
            called.append("persona_plus")

        fake_handlers = [
            SimpleNamespace(
                handler=lm_handler,
                handler_module_path="data.plugins.astrbot_plugin_livingmemory.main",
                handler_full_name="astrbot_plugin_livingmemory.on_llm_request",
                handler_name="on_llm_request",
            ),
            SimpleNamespace(
                handler=persona_handler,
                handler_module_path="data.plugins.astrbot_plugin_persona_plus.main",
                handler_full_name="astrbot_plugin_persona_plus.on_llm_request",
                handler_name="on_llm_request",
            ),
        ]

        class FakeRegistry:
            def get_handlers_by_event_type(self, event_type, plugins_name=None):
                return fake_handlers

        fake_event_type = SimpleNamespace(
            OnLLMRequestEvent="OnLLMRequestEvent",
            OnLLMResponseEvent="OnLLMResponseEvent",
        )
        monkeypatch.setattr(event_mod, "star_handlers_registry", FakeRegistry())
        monkeypatch.setattr(event_mod, "EventType", fake_event_type)

        class DummyEvent:
            plugins_name = None

            def __init__(self, extra):
                self._extra = extra

            def get_extra(self, key, default=None):
                return self._extra.get(key, default)

            def is_stopped(self):
                return False

        # 已通过 memory_bridge 召回 -> OnLLMRequestEvent 跳过 livingmemory，保留 persona_plus
        ev_recalled = DummyEvent(
            {
                "_proactive_memory_recalled": True,
                "_proactive_memory_plugin_id": "astrbot_plugin_livingmemory",
            }
        )
        asyncio.run(
            event_mod.dispatch_event_hook(
                ev_recalled, fake_event_type.OnLLMRequestEvent, object()
            )
        )
        assert called == ["persona_plus"]

        # OnLLMResponseEvent 不跳过 livingmemory（写回仍正常执行）
        called.clear()
        asyncio.run(
            event_mod.dispatch_event_hook(
                ev_recalled, fake_event_type.OnLLMResponseEvent, object()
            )
        )
        assert called == ["livingmemory", "persona_plus"]

    def test_context_stage_sets_trace_persona_and_memory_dedup_extras(self):
        extras = {}

        class DummyEvent:
            def set_extra(self, k, v):
                extras[k] = v

        class FakeContextSvc:
            def __init__(self):
                self.reset_calls = []

            async def prepare_llm_request(self, session_id, **kwargs):
                return {
                    "conv_id": "c1",
                    "history": [{"role": "user", "content": "最近在练琴"}],
                    "raw_history": [{"role": "user", "content": "最近在练琴"}],
                    "platform_context": "",
                    "system_prompt": "\n<persona_instructions>\n宵崎奏\n</persona_instructions>\n",
                    "persona_id": "宵崎奏",
                    "persona_name": "宵崎奏",
                    "session_id": session_id,
                    "conversation": SimpleNamespace(persona_id="旧卡"),
                }

            def reset_session_state_on_persona_change(self, sid, pid):
                self.reset_calls.append((sid, pid))
                return True

        class FakeBridge:
            async def recall(self, sid, query, persona_id=None):
                assert persona_id == "宵崎奏"
                return [{"content": "对方最近在学编曲", "importance": 0.7}]

            @staticmethod
            def format_memories(memories):
                return "<long_term_memory>编曲</long_term_memory>"

        ctx_svc = FakeContextSvc()
        plugin = SimpleNamespace(
            context_service=ctx_svc,
            session_service=_FakeSessionService(),
            config_service=SimpleNamespace(get_session_config=lambda umo: {}),
            conversation_state_service=SimpleNamespace(snapshot=lambda *a, **k: {}),
            rhythm_service=SimpleNamespace(snapshot=lambda *a, **k: {}),
            persona_state_service=SimpleNamespace(snapshot=lambda *a, **k: {}),
            conversation_memory_service=None,
            memory_bridge=FakeBridge(),
            sender_service=SimpleNamespace(build_proactive_event=lambda umo: DummyEvent()),
            data_lock=asyncio.Lock(),
        )
        stage = ContextStage(SimpleNamespace(plugin=plugin))
        ctx = ProactiveContext(
            session_id=UMO,
            session=SessionKey.parse(UMO),
            trigger=TriggerSource.SCHEDULE,
        )
        res = asyncio.run(stage.handle(ctx))
        assert res is StageResult.CONTINUE
        assert ctx_svc.reset_calls == [(UMO, "宵崎奏")]
        assert extras["_trace_persona_id"] == "宵崎奏"
        assert extras["_proactive_memory_recalled"] is True

    def test_global_semaphore_limits_concurrent_proactive_runs(self):
        plugin = SimpleNamespace(
            config={"private_settings": {"schedule_settings": {"max_concurrent_tasks": 2}}},
            session_service=_FakeSessionService(),
            manual_trigger_sessions=set(),
            active_trigger_sessions=set(),
            _terminating=False,
        )
        orch = FlowOrchestrator(plugin)
        running = 0
        peak = 0

        async def slow_locked(sid, trigger):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.03)
            running -= 1

        orch._run_locked = slow_locked  # type: ignore[method-assign]

        async def _go():
            await asyncio.gather(
                *[
                    orch.run(f"mzkbot:FriendMessage:U{i}", TriggerSource.SCHEDULE)
                    for i in range(6)
                ]
            )

        asyncio.run(_go())
        assert peak == 2


# ---------------------------------------------------------------------------
# 4. 永久性发送失败自动暂停，避免无限重调度
# ---------------------------------------------------------------------------
class TestPermanentDeliveryErrorPause:
    def test_permanent_error_classifier(self):
        assert is_permanent_delivery_error("send_private_msg failed: 无好友关系") is True
        assert is_permanent_delivery_error("API error: 无权限发送私聊") is True
        assert is_permanent_delivery_error("errcode=11292 user not found") is True
        assert is_permanent_delivery_error("Not a friend") is True
        assert is_permanent_delivery_error("connection timeout after 10s") is False
        assert is_permanent_delivery_error("") is False

    def test_deliver_stage_pauses_and_stops_on_permanent_error(self):
        class Storage:
            async def save_data(self):
                return None

        plugin = SimpleNamespace(
            _terminating=False,
            session_service=_FakeSessionService(),
            config_service=SimpleNamespace(get_session_config=lambda umo: {"enable": True}),
            storage_service=Storage(),
            last_message_times={},
            session_data={UMO: {}},
            data_lock=asyncio.Lock(),
            scheduler=None,
            auto_trigger_timers={},
        )
        plugin.scheduler_service = SchedulerService(plugin)

        async def fake_send(session_id, text, report_out=None, **kwargs):
            if report_out is not None:
                report_out.error_message = "OneBot retcode=1200: 无好友关系"
                report_out.permanent_failure = True
            return False

        plugin.sender_service = SimpleNamespace(send_proactive_message=fake_send)
        stage = DeliverStage(SimpleNamespace(plugin=plugin))
        ctx = ProactiveContext(
            session_id=UMO,
            session=SessionKey.parse(UMO),
            trigger=TriggerSource.SCHEDULE,
            extra={"response_text": "嗨"},
        )
        res = asyncio.run(stage.handle(ctx))
        assert res is StageResult.STOP
        assert plugin.session_data[UMO][PAUSE_FLAG] is True
        assert plugin.session_data[UMO][PAUSE_REASON] == PAUSE_REASON_PERMANENT_SEND_ERROR

    def test_deliver_stage_applies_backoff_on_repeated_transient_error(self):
        plugin = SimpleNamespace(
            _terminating=False,
            session_service=_FakeSessionService(),
            config_service=SimpleNamespace(get_session_config=lambda umo: {"enable": True}),
            last_message_times={},
            session_data={UMO: {}},
            scheduler_service=SimpleNamespace(),
        )

        async def fake_send(session_id, text, report_out=None, **kwargs):
            if report_out is not None:
                report_out.error_message = "temporary network timeout"
                report_out.permanent_failure = False
            return False

        plugin.sender_service = SimpleNamespace(send_proactive_message=fake_send)
        stage = DeliverStage(SimpleNamespace(plugin=plugin))

        ctx1 = ProactiveContext(
            session_id=UMO,
            session=SessionKey.parse(UMO),
            trigger=TriggerSource.SCHEDULE,
            extra={"response_text": "嗨"},
        )
        assert asyncio.run(stage.handle(ctx1)) is StageResult.RESCHEDULE
        assert "retry_after_seconds" not in ctx1.extra

        ctx2 = ProactiveContext(
            session_id=UMO,
            session=SessionKey.parse(UMO),
            trigger=TriggerSource.SCHEDULE,
            extra={"response_text": "嗨"},
        )
        assert asyncio.run(stage.handle(ctx2)) is StageResult.RESCHEDULE
        assert ctx2.extra.get("retry_after_seconds", 0) >= 1800

    def test_sender_service_propagates_event_error_into_report(self):
        plugin = SimpleNamespace(
            config_service=SimpleNamespace(
                get_session_config=lambda umo: {
                    "enable": True,
                    "tts_settings": {"enable_tts": False},
                    "segmented_reply_settings": {"enable": False},
                }
            ),
            session_service=_FakeSessionService(),
            telemetry=None,
        )
        sender = SenderService(plugin)

        async def no_decorate(ev, comps):
            return comps, True

        async def fail_send(sid, ev, comps):
            ev.proactive_last_error = "发送失败：无好友关系"
            return False

        sender.run_decorating_hooks = no_decorate  # type: ignore[method-assign]
        sender.send_chain = fail_send  # type: ignore[method-assign]

        ev = SimpleNamespace(proactive_last_error="")
        report = DeliveryReport()
        ok = asyncio.run(
            sender.send_proactive_message(UMO, "测试", event=ev, report_out=report)
        )
        assert ok is False
        assert "无好友关系" in report.error_message
        assert report.permanent_failure is True


# ---------------------------------------------------------------------------
# 5. plugin_set 继承 + hybrid 模式去重 [系统任务]
# ---------------------------------------------------------------------------
class TestPluginSetAndHybridPrompt:
    def test_resolve_session_plugins_name(self):
        # 受限 bot（如 wsbot）：只允许白名单内插件
        restricted_plugin = SimpleNamespace(
            context=SimpleNamespace(
                get_config=lambda umo=None: {
                    "plugin_set": ["astrbot_plugin_proactive_chat", "astrbot_plugin_persona_plus"]
                }
            )
        )
        assert event_mod.resolve_session_plugins_name(
            restricted_plugin, "wsbot:FriendMessage:123"
        ) == ["astrbot_plugin_proactive_chat", "astrbot_plugin_persona_plus"]

        # 通配符 ["*"] -> None（与 AstrBot 本体一致）
        wildcard_plugin = SimpleNamespace(
            context=SimpleNamespace(get_config=lambda umo=None: {"plugin_set": ["*"]})
        )
        assert event_mod.resolve_session_plugins_name(wildcard_plugin, UMO) is None

        # 空列表 -> []（禁用所有非保留插件）
        empty_plugin = SimpleNamespace(
            context=SimpleNamespace(get_config=lambda umo=None: {"plugin_set": []})
        )
        assert event_mod.resolve_session_plugins_name(empty_plugin, UMO) == []

    def test_hybrid_mode_strips_duplicate_system_task_prompt(self):
        plugin = SimpleNamespace(timezone=timezone.utc)
        svc = ContextService(plugin)
        records = [
            {
                "sender_name": "小月",
                "sender_id": "2155762216",
                "content": {"type": "user", "message": [{"type": "plain", "text": "刚到家"}]},
                "created_at": datetime.now(timezone.utc).timestamp() - 600,
            }
        ]
        legacy_dup_prompt = (
            "[系统任务：私聊主动对话]\n你现在需要在私聊中发起一次主动消息。\n"
            "[真实平台聊天流水开始]\n{{platform_history_lines}}\n[真实平台聊天流水结束]\n"
            "[最终指令]\n生成一句开场白。"
        )
        # hybrid 模式下命中重复 [系统任务] 模板 -> 自动降级为纯 <recent_chat> 块
        hybrid_text, used, _ = svc.format_platform_history_as_context(
            records,
            include_bot_messages=True,
            context_settings={
                "source_mode": "hybrid",
                "platform_history_prompt": legacy_dup_prompt,
            },
        )
        assert used >= 1
        assert "<recent_chat>" in hybrid_text
        assert "刚到家" in hybrid_text
        assert "[系统任务" not in hybrid_text
        assert "[最终指令]" not in hybrid_text
        assert HYBRID_PLATFORM_HISTORY_PROMPT.splitlines()[0] in hybrid_text

        # 非 hybrid（platform_message_history）模式仍保留原模板行为
        plat_text, _, _ = svc.format_platform_history_as_context(
            records,
            include_bot_messages=True,
            context_settings={
                "source_mode": "platform_message_history",
                "platform_history_prompt": "",
            },
        )
        assert DEFAULT_PLATFORM_HISTORY_PROMPT.splitlines()[0] in plat_text
