"""针对外部审计问题的回归测试。

覆盖：重试比例单位、手动触发免打扰、重启补排期、遥测脱敏、
对话历史不再写入主动提示词、发送后钩子、正则捕获组与日志底数、
原子落盘以及 Provider 下拉数据源。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timezone
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.adapters.astrbot_conversation import (
    add_assistant_message,
)
from astrbot_plugin_proactive_chat.core.adapters.event_handlers import EventHandlers
from astrbot_plugin_proactive_chat.core.console.page_api import PageApiRoutes
from astrbot_plugin_proactive_chat.core.features.delivery_plan_feature import (
    DeliveryPlanStage,
)
from astrbot_plugin_proactive_chat.core.features.guard_feature import GuardStage
from astrbot_plugin_proactive_chat.core.services.lifecycle_service import LifecycleService
from astrbot_plugin_proactive_chat.core.services.session_control_service import (
    SessionControlService,
)
from astrbot_plugin_proactive_chat.core.services.storage_service import (
    StorageService,
)
from astrbot_plugin_proactive_chat.core.console.state_builder import StateBuilder
from astrbot_plugin_proactive_chat.core.domain.domain_enums import TriggerSource
from astrbot_plugin_proactive_chat.core.domain.intensity import (
    IntensitySettings,
    skip_retry_seconds,
)
from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator import FlowOrchestrator
from astrbot_plugin_proactive_chat.core.remote.telemetry_manager import TelemetryManager
from astrbot_plugin_proactive_chat.core.services.config_service import ConfigService
from astrbot_plugin_proactive_chat.core.services.scheduler_service import SchedulerService
from astrbot_plugin_proactive_chat.core.services.sender_service import SenderService
from astrbot_plugin_proactive_chat.core.store.session_store import SessionStore

UMO = "qq_official:FriendMessage:REG1"


class FakeSessionService:
    def parse_session_id(self, umo):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo):
        return umo

    def get_session_log_str(self, umo, config=None):
        return umo


def test_skip_retry_ratio_percent_is_parsed_as_ratio():
    settings = IntensitySettings.from_dict({"skip_retry_ratio": 20})
    assert settings.skip_retry_ratio == 0.2
    # 有效最小间隔 2070 秒时，20% 应约为 414 秒，而不是完整 2070 秒。
    assert skip_retry_seconds(2070, settings) == 414

    legacy = IntensitySettings.from_dict({"skip_retry_ratio": 0.2})
    assert legacy.skip_retry_ratio == 0.2


def test_manual_trigger_bypasses_quiet_hours():
    class ConfigService:
        def get_session_config(self, umo):
            return {
                "enable": True,
                "schedule_settings": {"quiet_hours": "0-23"},
            }

    plugin = SimpleNamespace(
        session_service=FakeSessionService(),
        config_service=ConfigService(),
        timezone=timezone.utc,
    )
    orchestrator = FlowOrchestrator(plugin)

    allowed, reason = asyncio.run(
        orchestrator.is_chat_allowed(UMO, TriggerSource.MANUAL)
    )
    assert (allowed, reason) == (True, "allowed")

    allowed, reason = asyncio.run(
        orchestrator.is_chat_allowed(UMO, TriggerSource.SCHEDULE)
    )
    assert (allowed, reason) == (False, "quiet_hours")


def test_restart_reschedules_missing_and_expired_tasks():
    settings = {
        "enable": True,
        "schedule_settings": {
            "min_interval_minutes": 30,
            "max_interval_minutes": 60,
            "max_unanswered_times": 4,
        },
    }

    class ConfigService:
        def get_session_config(self, umo):
            return settings

    class Storage:
        def cleanup_invalid_session_data(self):
            return 0

        async def save_data(self):
            return None

    plugin = SimpleNamespace(
        session_service=FakeSessionService(),
        config_service=ConfigService(),
        storage_service=Storage(),
        session_data={
            "qq_official:FriendMessage:EXPIRED": {
                "next_trigger_time": time.time() - 3600,
            },
            "qq_official:FriendMessage:MISSING": {
                "unanswered_count": 1,
            },
        },
        timezone=timezone.utc,
        data_lock=asyncio.Lock(),
    )
    service = SchedulerService(plugin)
    scheduled: list[str] = []

    async def fake_schedule(session_id, **kwargs):
        scheduled.append(session_id)

    service.schedule_next_chat_and_save = fake_schedule  # type: ignore[method-assign]

    asyncio.run(service.init_jobs_from_data())

    assert set(scheduled) == {
        "qq_official:FriendMessage:EXPIRED",
        "qq_official:FriendMessage:MISSING",
    }


def test_telemetry_config_snapshot_is_recursively_redacted():
    manager = object.__new__(TelemetryManager)
    snapshot = manager._sanitize_config_snapshot(
        {
            "private_settings": {
                "session_list": ["a", "b"],
                "proactive_prompt": "SECRET1",
                "context_settings": {"platform_history_prompt": "SECRET2"},
                "pause_settings": {"topic_judge_prompt": "SECRET3"},
                "custom_settings": {"provider_id": "openai"},
            },
            "web_admin": {"password": "SECRET4", "host": "127.0.0.1"},
            "telemetry_config": {"enabled": True},
        }
    )

    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "SECRET" not in serialized
    assert snapshot["private_settings"]["session_list_count"] == 2
    assert snapshot["private_settings"]["custom_settings"]["provider_id"] == "openai"
    assert "password" not in snapshot["web_admin"]


def test_assistant_archive_does_not_write_user_prompt():
    class DB:
        def __init__(self):
            self.saved = None

        async def get_conversation_by_id(self, cid):
            return SimpleNamespace(content=[{"role": "assistant", "content": ["旧回复"]}])

        async def update_conversation(self, cid, content):
            self.saved = content

    db = DB()
    manager = SimpleNamespace(db=db)

    asyncio.run(add_assistant_message(manager, "conv1", "新的主动消息"))

    assert db.saved is not None
    assert db.saved[-1]["role"] == "assistant"
    assert db.saved[-1]["content"] == ["新的主动消息"]
    assert all(item["role"] != "user" for item in db.saved)


def test_sender_regex_capture_group_and_invalid_log_base():
    service = SenderService(SimpleNamespace())

    segments = service.split_text(
        "你好。今天还好吗？",
        {"split_mode": "regex", "regex": r"(.*?)([。？！])"},
    )
    assert segments == ["你好。", "今天还好吗？"]

    interval = asyncio.run(
        service.calc_interval("hello world", {"interval_method": "log", "log_base": 1})
    )
    assert interval >= 0


def test_after_sent_hook_is_not_dispatched_on_failure(monkeypatch):
    calls: list[str] = []

    async def fake_dispatch(event, event_type, *args, **kwargs):
        calls.append(str(event_type))
        return False

    import astrbot_plugin_proactive_chat.core.services.sender_service as sender_module

    monkeypatch.setattr(sender_module, "dispatch_event_hook", fake_dispatch)

    class ConfigService:
        def get_session_config(self, umo):
            return {
                "enable": True,
                "tts_settings": {"enable_tts": False},
                "segmented_reply_settings": {"enable": False},
            }

    plugin = SimpleNamespace(
        config_service=ConfigService(),
        session_service=FakeSessionService(),
        telemetry=None,
    )
    service = SenderService(plugin)

    async def no_decorate(event, components):
        return components, True

    async def fail_send(session_id, event, components):
        return False

    async def no_persist(session_id, chain):
        return None

    service.run_decorating_hooks = no_decorate  # type: ignore[method-assign]
    service.send_chain = fail_send  # type: ignore[method-assign]
    service.persist_proactive_message_to_platform_history = no_persist  # type: ignore[method-assign]

    result = asyncio.run(
        service.send_proactive_message(UMO, "测试文本", event=SimpleNamespace())
    )
    assert result is False
    assert calls == []


def test_session_store_save_is_atomic(tmp_path):
    store = SessionStore(tmp_path)
    asyncio.run(store.save({UMO: {"unanswered_count": 2}}))

    target = tmp_path / "session_data.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        UMO: {"unanswered_count": 2}
    }
    assert not (tmp_path / "session_data.json.tmp").exists()


def test_provider_list_is_exposed_for_dropdowns():
    class Meta:
        def __init__(self, provider_id, model):
            self.id = provider_id
            self.model = model
            self.type = "openai_chat_completion"

    class Provider:
        def __init__(self, provider_id, model):
            self.provider_id = provider_id
            self.model = model

        def meta(self):
            return Meta(self.provider_id, self.model)

    manager = SimpleNamespace(
        provider_insts=[Provider("p1", "gpt-4o"), Provider("p2", "claude")]
    )
    plugin = SimpleNamespace(
        context=SimpleNamespace(provider_manager=manager),
        config={},
    )
    service = ConfigService(plugin)

    providers = service.list_chat_providers()
    assert [item["id"] for item in providers] == ["p1", "p2"]
    assert providers[0]["label"] == "p1 · gpt-4o"


def test_guard_block_never_dispatches_llm_response_hooks():
    calls: list[str] = []

    class GuardService:
        async def review(self, *args, **kwargs):
            return {"allowed": False, "reasons": ["too_many_questions"]}

    class LlmService:
        async def dispatch_llm_response_hooks(self, event, response):
            calls.append("hook")
            return False

    plugin = SimpleNamespace(
        guard_service=GuardService(),
        llm_service=LlmService(),
        config_service=SimpleNamespace(get_session_config=lambda umo: {"enable": True}),
        session_service=FakeSessionService(),
        session_data={UMO: {}},
        storage_service=SimpleNamespace(save_data=lambda: asyncio.sleep(0)),
        data_lock=asyncio.Lock(),
    )

    class Orchestrator:
        pass

    orchestrator = Orchestrator()
    orchestrator.plugin = plugin
    stage = GuardStage(orchestrator)

    ctx = SimpleNamespace(
        session_id=UMO,
        started_at=time.time(),
        extra={
            "response_text": "你吃了吗？你睡了吗？你在干嘛？",
            "result_chain": [],
            "llm_response": SimpleNamespace(completion_text="x"),
        },
        abort=lambda reason: None,
    )

    result = asyncio.run(stage.handle(ctx))
    assert result.value == "reschedule"
    assert calls == []


def test_delivery_plan_stage_dispatches_response_hooks_after_guard():
    calls: list[str] = []

    class LlmService:
        async def dispatch_llm_response_hooks(self, event, response):
            calls.append("hook")
            return False

        @staticmethod
        def extract_response_text(response):
            return "守卫通过后的文本"

        @staticmethod
        def extract_response_chain(response):
            return []

    class ConfigService:
        def get_session_config(self, umo):
            return {
                "enable": True,
                "tts_settings": {"enable_tts": False},
                "humanization_settings": {
                    "delivery_plan_settings": {"enable": False}
                },
            }

    plugin = SimpleNamespace(
        llm_service=LlmService(),
        config_service=ConfigService(),
    )

    class Orchestrator:
        pass

    orchestrator = Orchestrator()
    orchestrator.plugin = plugin
    stage = DeliveryPlanStage(orchestrator)

    ctx = SimpleNamespace(
        session_id=UMO,
        event=None,
        extra={
            "response_text": "旧文本",
            "result_chain": [],
            "llm_response": SimpleNamespace(completion_text="x"),
        },
        abort=lambda reason: None,
    )

    result = asyncio.run(stage.handle(ctx))
    assert result.value == "continue"
    assert calls == ["hook"]
    assert ctx.extra["response_text"] == "守卫通过后的文本"


def test_finalize_skips_unanswered_count_when_user_replied_after_send(monkeypatch):
    import astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator as flow_module

    async def noop_archive(*args, **kwargs):
        return None

    async def always_persisted(*args, **kwargs):
        return True

    monkeypatch.setattr(flow_module, "add_assistant_message", noop_archive)
    monkeypatch.setattr(flow_module, "verify_message_persisted", always_persisted)

    scheduled = []
    plugin = SimpleNamespace(
        _terminating=False,
        data_lock=asyncio.Lock(),
        last_message_times={UMO: time.time()},
        session_data={UMO: {"unanswered_count": 1}},
        session_service=FakeSessionService(),
        config_service=SimpleNamespace(
            get_session_config=lambda umo: {"enable": True}
        ),
        context=SimpleNamespace(conversation_manager=SimpleNamespace()),
        storage_service=SimpleNamespace(save_data=lambda: asyncio.sleep(0)),
        scheduler_service=SimpleNamespace(
            add_chat_job=lambda *args, **kwargs: scheduled.append(args)
        ),
    )
    orchestrator = FlowOrchestrator(plugin)

    asyncio.run(
        orchestrator.finalize_and_reschedule(
            UMO,
            "conv1",
            "主动消息正文",
            0,
            started_at=time.time() - 5,
        )
    )

    assert plugin.session_data[UMO]["unanswered_count"] == 1
    assert scheduled == []


def test_sender_stops_remaining_bubbles_after_user_reply():
    state = {"sent": 0, "bubbles": 0}

    class ConfigService:
        def get_session_config(self, umo):
            return {
                "enable": True,
                "tts_settings": {"enable_tts": False},
                "segmented_reply_settings": {"enable": False},
            }

    plugin = SimpleNamespace(
        config_service=ConfigService(),
        session_service=FakeSessionService(),
        telemetry=None,
    )
    service = SenderService(plugin)

    async def decorate(event, components):
        return components, True

    async def send(session_id, event, components):
        state["bubbles"] += 1
        state["sent"] += 1
        return True

    async def no_persist(session_id, chain):
        return None

    service.run_decorating_hooks = decorate  # type: ignore[method-assign]
    service.send_chain = send  # type: ignore[method-assign]
    service.persist_proactive_message_to_platform_history = no_persist  # type: ignore[method-assign]

    plan = {
        "bubbles": [
            {"text": "第一句", "delay_seconds": 0},
            {"text": "第二句", "delay_seconds": 0},
            {"text": "第三句", "delay_seconds": 0},
        ],
        "use_chain": False,
    }

    result = asyncio.run(
        service.send_proactive_message(
            UMO,
            "第一句第二句第三句",
            event=SimpleNamespace(),
            delivery_plan=plan,
            abort_check=lambda: state["sent"] >= 1,
        )
    )

    assert result is True
    assert state["bubbles"] == 1


def test_session_override_can_opt_in_without_session_list():
    class OverrideManager:
        def get_override(self, session_id):
            return {"enable": True} if session_id == UMO else {}

        def get_effective(self, session_id, base):
            effective = dict(base)
            effective.update(self.get_override(session_id))
            return effective

    plugin = SimpleNamespace(
        config={
            "private_settings": {
                "enable": True,
                "session_list": [],
            }
        },
        session_service=FakeSessionService(),
        session_override_manager=OverrideManager(),
    )
    service = ConfigService(plugin)

    config = service.get_session_config(UMO)
    assert config is not None
    assert config["_from_session_list"] is False
    assert config["enable"] is True


def test_session_commands_enable_disable_and_status():
    class OverrideManager:
        def __init__(self):
            self.overrides = {}

        def get_override(self, session_id):
            return dict(self.overrides.get(session_id, {}))

        async def set_override(self, session_id, patch):
            self.overrides[session_id] = dict(patch)

    class SchedulerService:
        def __init__(self):
            self.scheduled = []
            self.resumed = []
            self.paused = False

        def is_session_paused(self, session_id):
            return self.paused

        def is_in_cooldown(self, session_id):
            return False

        def cooldown_remaining(self, session_id):
            return 0

        def resume_session(self, session_id, clear_cooldown=False):
            self.resumed.append((session_id, clear_cooldown))
            return True

        async def cancel_all_related_auto_triggers(self, session_id):
            return True

        async def schedule_next_chat_and_save(self, session_id, reset_counter=False):
            self.scheduled.append((session_id, reset_counter))
            plugin.session_data[session_id]["next_trigger_time"] = time.time() + 600

        def clear_session_schedule_state(self, session_id):
            plugin.session_data.get(session_id, {}).pop("next_trigger_time", None)
            return True

    overrides = OverrideManager()
    scheduler_service = SchedulerService()

    class ConfigServiceStub:
        def get_private_settings(self):
            return {"enable": True, "session_list": []}

        def get_session_config(self, session_id):
            if overrides.get_override(session_id).get("enable"):
                return {"enable": True}
            return None

    plugin = SimpleNamespace(
        config_service=ConfigServiceStub(),
        session_override_manager=overrides,
        scheduler_service=scheduler_service,
        session_service=FakeSessionService(),
        session_data={UMO: {}},
        data_lock=asyncio.Lock(),
        storage_service=SimpleNamespace(save_data=lambda: asyncio.sleep(0)),
        scheduler=SimpleNamespace(remove_job=lambda session_id: None),
        timezone=timezone.utc,
    )
    service = SessionControlService(plugin)
    event = SimpleNamespace(unified_msg_origin=UMO)

    enabled_text = asyncio.run(service.enable(event))
    assert "已开启" in enabled_text
    assert overrides.overrides[UMO]["enable"] is True
    assert scheduler_service.resumed == [(UMO, True)]
    assert scheduler_service.scheduled == [(UMO, True)]

    status_text = asyncio.run(service.status(event))
    assert "已开启" in status_text

    disabled_text = asyncio.run(service.disable(event))
    assert "已关闭" in disabled_text
    assert overrides.overrides[UMO]["enable"] is False
    assert "next_trigger_time" not in plugin.session_data[UMO]

    disabled_status = asyncio.run(service.status(event))
    assert "已关闭" in disabled_status


def test_intensity_seed_merge_preserves_newer_runtime_events():
    seed = {
        UMO: {
            "events": [{"t": 100.0, "n": 3}],
            "last_user_ts": 100.0,
            "total_user_messages": 1,
        }
    }
    session_data = {UMO: {"unanswered_count": 2}}
    merged = StorageService.apply_intensity_seed(session_data, seed)
    assert merged == 1
    assert session_data[UMO]["intensity"]["events"] == [{"t": 100.0, "n": 3}]
    assert session_data[UMO]["unanswered_count"] == 2

    # 运行时已经有更新的用户消息：不能被旧种子覆盖。
    session_data[UMO]["intensity"] = {
        "events": [{"t": 200.0, "n": 5}],
        "last_user_ts": 200.0,
        "total_user_messages": 2,
    }
    merged = StorageService.apply_intensity_seed(session_data, seed)
    assert merged == 0
    assert session_data[UMO]["intensity"]["last_user_ts"] == 200.0

    # 新种子比旧种子更晚：允许更新。
    newer_seed = {
        UMO: {
            "events": [{"t": 300.0, "n": 4}],
            "last_user_ts": 300.0,
            "total_user_messages": 3,
        }
    }
    merged = StorageService.apply_intensity_seed(session_data, newer_seed)
    assert merged == 1
    assert session_data[UMO]["intensity"]["last_user_ts"] == 300.0


def test_plugin_set_mismatch_is_detected():
    plugin = SimpleNamespace(
        context=SimpleNamespace(
            get_config=lambda: {"plugin_set": ["some_other_plugin"]}
        )
    )
    warning = LifecycleService(plugin).detect_plugin_set_warning()
    assert "astrbot_plugin_proactive_chat" in warning
    assert "plugin_set" in warning

    plugin.context.get_config = lambda: {"plugin_set": ["*"]}
    assert LifecycleService(plugin).detect_plugin_set_warning() == ""

    plugin.context.get_config = lambda: {
        "plugin_set": ["astrbot_plugin_proactive_chat"]
    }
    assert LifecycleService(plugin).detect_plugin_set_warning() == ""


def test_page_api_unregister_removes_only_own_routes():
    registered = [
        ("/astrbot_plugin_proactive_chat/status", object(), ["GET"], "status"),
        ("/other_plugin/status", object(), ["GET"], "status"),
    ]
    plugin = SimpleNamespace(
        context=SimpleNamespace(registered_web_apis=registered),
        version="v2.4.0",
    )
    routes = PageApiRoutes(plugin)
    routes.registered = True

    routes.unregister()

    assert [item[0] for item in plugin.context.registered_web_apis] == [
        "/other_plugin/status"
    ]
    assert routes.registered is False


def test_private_user_message_is_backfilled_to_platform_history():
    calls: list[dict] = []

    class HistoryManager:
        async def insert_message_chain(self, **kwargs):
            calls.append(kwargs)

    plugin = SimpleNamespace(
        session_service=FakeSessionService(),
        context=SimpleNamespace(
            message_history_manager=HistoryManager()
        ),
    )
    handler = EventHandlers(plugin)
    event = SimpleNamespace(
        get_messages=lambda: ["hello"],
        get_sender_id=lambda: "user-1",
        get_sender_name=lambda: "小明",
    )

    asyncio.run(
        handler._persist_user_message_to_platform_history(event, UMO)
    )

    assert len(calls) == 1
    assert calls[0]["platform_id"] == "qq_official"
    assert calls[0]["user_id"] == "REG1"
    assert calls[0]["role"] == "user"
    assert calls[0]["sender_id"] == "user-1"
    assert calls[0]["max_messages"] == 700


def test_status_payload_exposes_plugin_set_warning_and_timezone():
    plugin = SimpleNamespace(
        plugin_start_time=time.time(),
        scheduler=None,
        auto_trigger_timers={},
        manual_trigger_sessions=set(),
        session_data={},
        session_service=FakeSessionService(),
        config_service=SimpleNamespace(
            get_session_config=lambda umo: None,
            get_private_settings=lambda: {},
        ),
        timezone=timezone.utc,
        plugin_set_warning="plugin_set 缺少本插件",
        version="v2.4.0",
        session_override_manager=SimpleNamespace(list_sessions=lambda: []),
        intensity_service=SimpleNamespace(summary=lambda sessions: {}),
    )
    builder = StateBuilder(plugin, "v2.4.0")

    payload = builder.build_status_payload()
    assert payload["plugin_set_warning"] == "plugin_set 缺少本插件"
    assert payload["timezone"] == "UTC"
