"""多轮端到端及回归测试：连续未回复暂停、重启恢复、竞态、UMO 漂移与生命周期。"""

from __future__ import annotations

import asyncio
import time
from datetime import timezone
from types import SimpleNamespace

from astrbot.core.platform.platform import PlatformStatus
from astrbot_plugin_proactive_chat.core.adapters.event_handlers import EventHandlers
from astrbot_plugin_proactive_chat.core.domain.domain_enums import StageResult, TriggerSource
from astrbot_plugin_proactive_chat.core.features.gating_feature import GatingStage
from astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator import FlowOrchestrator
from astrbot_plugin_proactive_chat.core.services.config_service import ConfigService
from astrbot_plugin_proactive_chat.core.services.conversation_memory_service import (
    ConversationMemoryService,
)
from astrbot_plugin_proactive_chat.core.services.conversation_state_service import (
    ConversationStateService,
)
from astrbot_plugin_proactive_chat.core.services.guard_service import GuardService
from astrbot_plugin_proactive_chat.core.services.intensity_service import IntensityService
from astrbot_plugin_proactive_chat.core.services.lifecycle_service import LifecycleService
from astrbot_plugin_proactive_chat.core.services.llm_service import LlmService
from astrbot_plugin_proactive_chat.core.services.persona_state_service import (
    PersonaStateService,
)
from astrbot_plugin_proactive_chat.core.services.rhythm_service import RhythmService
from astrbot_plugin_proactive_chat.core.services.scheduler_service import SchedulerService
from astrbot_plugin_proactive_chat.core.services.session_service import SessionService
from astrbot_plugin_proactive_chat.core.services.storage_service import StorageService
from astrbot_plugin_proactive_chat.core.services.topic_judge import TopicJudgeService
from astrbot_plugin_proactive_chat.core.store.override_store import OverrideStore

UMO = "qq_official:FriendMessage:E2E_USER"


class FakeScheduler:
    def __init__(self):
        self.jobs = {}
        self.running = True

    def add_job(self, func, trigger, **kwargs):
        self.jobs[kwargs["id"]] = kwargs

    def get_jobs(self):
        return [SimpleNamespace(id=jid) for jid in self.jobs]

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        self.jobs.pop(job_id, None)

    def remove_all_jobs(self):
        self.jobs.clear()

    def shutdown(self, wait=False):
        self.running = False


class FakeConversationManager:
    def __init__(self):
        self.conversations = {}

    async def get_conversation_by_id(self, cid):
        return self.conversations.get(cid)

    async def update_conversation(self, cid, content):
        self.conversations[cid] = SimpleNamespace(content=content)


class FakeContextService:
    async def prepare_llm_request(self, session_id, **kwargs):
        return {
            "conv_id": "conv_e2e",
            "history": [{"role": "user", "content": "你好"}],
            "system_prompt": "你是一个自然聊天的朋友。",
            "platform_context": "",
            "session_id": session_id,
            "conversation": SimpleNamespace(persona_id="p1"),
        }

    @staticmethod
    def sanitize_history_content(history):
        return history

    @staticmethod
    def make_temp_text_part(text):
        return text


class FakeSender:
    def __init__(self):
        self.sent = []

    def build_proactive_event(self, session_id):
        return SimpleNamespace(session_id=session_id)

    async def send_proactive_message(
        self,
        session_id,
        text,
        event=None,
        initial_chain=None,
        delivery_plan=None,
        abort_check=None,
        report_out=None,
    ):
        self.sent.append(text)
        if report_out is not None:
            report_out.sent = True
            report_out.delivered_bubbles.append(text)
        return True


class FakeProvider:
    def __init__(self, texts):
        self._texts = list(texts)
        self._idx = 0

    async def text_chat(self, **kwargs):
        text = self._texts[min(self._idx, len(self._texts) - 1)]
        self._idx += 1
        return SimpleNamespace(completion_text=text, result_chain=None)


class E2EPlugin:
    def __init__(self, tmp_path, *, max_unanswered=3, resume_mode="llm_topic"):
        self.data_dir = tmp_path
        self.session_data_file = tmp_path / "session_data.json"
        self.timezone = timezone.utc
        self.data_lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._startup_finalized = False
        self._startup_attempted_without_platforms = False
        self._terminating = False
        self.plugin_start_time = time.time() - 10
        self.session_data = {}
        self.last_message_times = {}
        self.auto_trigger_timers = {}
        self.manual_trigger_sessions = set()
        self.active_trigger_sessions = set()
        self.first_message_logged = set()
        self.web_admin_server = None
        self.notification_center = None
        self.page_api = None
        self.telemetry = None
        self._telemetry_tasks = set()
        self._heartbeat_task = None
        self._startup_task = None
        self._start_time = 0.0
        self.plugin_set_warning = ""

        self.config = {
            "private_settings": {
                "enable": True,
                "session_list": [UMO],
                "proactive_prompt": "随意说一句。",
                "schedule_settings": {
                    "min_interval_minutes": 30,
                    "max_interval_minutes": 60,
                    "quiet_hours": "",
                    "max_unanswered_times": max_unanswered,
                },
                "intensity_settings": {
                    "enable": True,
                    "probability_min": 100,
                    "probability_max": 100,
                },
                "pause_settings": {
                    "resume_mode": resume_mode,
                    "fallback_resume_on_error": False,
                },
                "tts_settings": {"enable_tts": False},
                "segmented_reply_settings": {"enable": False},
                "humanization_settings": {
                    "state_settings": {"enable": True, "silent_intent_enabled": False},
                    "feedback_settings": {"enable": True, "perfunctory_no_reset": True},
                    "guard_settings": {"enable": True, "rewrite_on_fail": False, "repeat_similarity": 0.95},
                    "delivery_plan_settings": {"enable": True, "first_delay_min_seconds": 0, "first_delay_max_seconds": 0},
                    "persona_settings": {"enable": True, "low_energy_probability_reduction": 0},
                    "rhythm_settings": {"enable": True, "learn_active_hours": False, "min_gap_minutes": 0, "daily_proactive_limit": 20},
                    "memory_integration_settings": {"enable": False},
                },
            }
        }

        self.context = SimpleNamespace(
            conversation_manager=FakeConversationManager(),
            platform_manager=SimpleNamespace(platform_insts=[]),
        )
        self.scheduler = FakeScheduler()
        self.session_override_manager = OverrideStore(tmp_path)
        self.session_service = SessionService(self)
        self.session_override_manager.set_normalizer(
            self.session_service.normalize_session_id
        )
        self.storage_service = StorageService(self)
        self.config_service = ConfigService(self)
        self.intensity_service = IntensityService(self)
        self.rhythm_service = RhythmService(self)
        self.conversation_state_service = ConversationStateService(self)
        self.conversation_memory_service = ConversationMemoryService(self)
        self.persona_state_service = PersonaStateService(self)
        self.guard_service = GuardService(self)
        self.llm_service = LlmService(self)
        self.topic_judge_service = TopicJudgeService(self)
        self.scheduler_service = SchedulerService(self)
        self.context_service = FakeContextService()
        self.sender_service = FakeSender()
        self.event_handlers = EventHandlers(self)
        self.orchestrator = FlowOrchestrator(self)

    def _track_task(self, task):
        if task is not None:
            self._telemetry_tasks.add(task)
            task.add_done_callback(self._telemetry_tasks.discard)
        return task

    async def _cleanup_telemetry_tasks(self):
        if not self._telemetry_tasks:
            return
        pending = list(self._telemetry_tasks)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._telemetry_tasks.clear()

    async def check_and_chat(self, session_id, trigger=TriggerSource.SCHEDULE):
        await self.orchestrator.run(session_id, trigger)


def test_e2e_unanswered_x_times_pauses_immediately_and_blocks_restart(tmp_path, monkeypatch):
    provider = FakeProvider(
        [
            "刚路过楼下看到一只橘猫。",
            "今天晚饭煮了番茄面，味道意外不错。",
            "刚收拾完桌子，准备歇一会儿。",
            "第4条绝对不应该被发出来。",
        ]
    )

    async def fake_resolve(self, session_id):
        return provider

    async def fake_invoke(prov, req):
        return await prov.text_chat()

    async def noop_archive(*args, **kwargs):
        return None

    async def always_persisted(*args, **kwargs):
        return True

    monkeypatch.setattr(LlmService, "resolve_chat_provider", fake_resolve)
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.llm_service.invoke_provider",
        fake_invoke,
    )
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator.add_assistant_message",
        noop_archive,
    )
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator.verify_message_persisted",
        always_persisted,
    )

    async def scenario():
        plugin = E2EPlugin(tmp_path, max_unanswered=3)

        # 第 1、2 次：未达到上限，应正常安排下一次任务。
        await plugin.orchestrator.run(UMO, TriggerSource.SCHEDULE)
        assert plugin.session_data[UMO]["unanswered_count"] == 1
        assert plugin.scheduler_service.is_session_paused(UMO) is False
        assert UMO in plugin.scheduler.jobs

        await plugin.orchestrator.run(UMO, TriggerSource.SCHEDULE)
        assert plugin.session_data[UMO]["unanswered_count"] == 2
        assert plugin.scheduler_service.is_session_paused(UMO) is False
        assert UMO in plugin.scheduler.jobs

        # 第 3 次（X-th send）：发送完成后立即暂停，绝不安排第 4 个定时任务。
        await plugin.orchestrator.run(UMO, TriggerSource.SCHEDULE)
        assert len(plugin.sender_service.sent) == 3
        assert plugin.session_data[UMO]["unanswered_count"] == 3
        assert plugin.scheduler_service.is_session_paused(UMO) is True
        assert plugin.session_data[UMO]["paused_threshold"] == 3
        assert plugin.session_data[UMO]["paused_unanswered_count"] == 3
        assert "next_trigger_time" not in plugin.session_data[UMO]
        assert UMO not in plugin.scheduler.jobs

        # 即使手动触发也不应再发送第 4 条。
        await plugin.orchestrator.run(UMO, TriggerSource.MANUAL)
        assert len(plugin.sender_service.sent) == 3

        # 模拟重启：从磁盘重新加载并恢复调度，已暂停会话绝不复活。
        restarted = E2EPlugin(tmp_path, max_unanswered=3)
        await restarted.storage_service.load_data()
        await restarted.scheduler_service.init_jobs_from_data()
        assert restarted.scheduler_service.is_session_paused(UMO) is True
        assert UMO not in restarted.scheduler.jobs

        # 暂停态收到纯非文本/表情消息（llm_topic 模式）：保持暂停且不清零计数。
        sticker_event = SimpleNamespace(
            unified_msg_origin=UMO,
            message_str="",
            get_messages=lambda: [object()],
            get_self_id=lambda: "bot",
            get_sender_id=lambda: "E2E_USER",
            get_sender_name=lambda: "用户",
            get_extra=lambda *a, **kw: None,
        )
        # get_extra 需要对 missing 默认返回 missing，模拟无 parsed_params。
        sticker_event.get_extra = lambda key, default=None: default
        await restarted.event_handlers.on_friend_message(sticker_event)
        assert restarted.scheduler_service.is_session_paused(UMO) is True
        assert restarted.session_data[UMO]["unanswered_count"] == 3
        assert UMO not in restarted.scheduler.jobs

        # 收到开启新话题的实质文本：调用话题判定 -> 恢复、清零、重新排期。
        async def fake_judge_invoke(prov, req):
            return SimpleNamespace(
                completion_text='{"new_topic": true, "reason": "开启新话题"}'
            )

        monkeypatch.setattr(
            "astrbot_plugin_proactive_chat.core.services.topic_judge.invoke_provider",
            fake_judge_invoke,
        )
        restarted.llm_service.resolve_chat_provider = lambda sid: _async_val(object())
        text_event = SimpleNamespace(
            unified_msg_origin=UMO,
            message_str="对了，我周末要去爬山，你有去过吗？",
            get_messages=lambda: [object()],
            get_self_id=lambda: "bot",
            get_sender_id=lambda: "E2E_USER",
            get_sender_name=lambda: "用户",
            get_extra=lambda key, default=None: default,
        )
        await restarted.event_handlers.on_friend_message(text_event)
        assert restarted.scheduler_service.is_session_paused(UMO) is False
        assert restarted.session_data[UMO]["unanswered_count"] == 0
        assert UMO in restarted.scheduler.jobs

    asyncio.run(scenario())


def test_init_jobs_blocks_unpaused_at_limit_and_cancelled_schedule(tmp_path):
    async def scenario():
        plugin = E2EPlugin(tmp_path, max_unanswered=3)
        umo_limit = "qq_official:FriendMessage:AT_LIMIT"
        umo_cancelled = "qq_official:FriendMessage:CANCELLED"
        plugin.config["private_settings"]["session_list"] = [umo_limit, umo_cancelled]
        plugin.session_data = {
            umo_limit: {
                "unanswered_count": 3,
                "next_trigger_time": time.time() + 600,
            },
            umo_cancelled: {
                "unanswered_count": 0,
                "schedule_cancelled": True,
            },
        }
        await plugin.scheduler_service.init_jobs_from_data()
        assert plugin.scheduler_service.is_session_paused(umo_limit) is True
        assert umo_limit not in plugin.scheduler.jobs
        assert umo_cancelled not in plugin.scheduler.jobs

        # cancel_session_schedule 写入 schedule_cancelled 后，重启也不复活。
        umo_manual_cancel = "qq_official:FriendMessage:MANUAL_CANCEL"
        plugin.config["private_settings"]["session_list"].append(umo_manual_cancel)
        await plugin.scheduler_service.schedule_next_chat_and_save(umo_manual_cancel)
        assert umo_manual_cancel in plugin.scheduler.jobs
        assert await plugin.scheduler_service.cancel_session_schedule(umo_manual_cancel) is True
        assert plugin.session_data[umo_manual_cancel]["schedule_cancelled"] is True
        assert umo_manual_cancel not in plugin.scheduler.jobs

        await plugin.scheduler_service.init_jobs_from_data()
        assert umo_manual_cancel not in plugin.scheduler.jobs

    asyncio.run(scenario())


def test_event_handler_pause_race_does_not_unpause(tmp_path):
    async def scenario():
        plugin = E2EPlugin(tmp_path, max_unanswered=3)
        plugin.session_data[UMO] = {
            "unanswered_count": 3,
            "proactive_paused": True,
            "paused_at": 100.0,
        }

        entered_judge = asyncio.Event()
        release_judge = asyncio.Event()

        async def slow_judge(session_id, text, session_config=None):
            entered_judge.set()
            await release_judge.wait()
            return True, {"new_topic": True, "reason": "ok"}

        plugin.topic_judge_service.should_resume = slow_judge
        event = SimpleNamespace(
            unified_msg_origin=UMO,
            message_str="我们来聊一个全新话题吧",
            get_messages=lambda: [object()],
            get_self_id=lambda: "bot",
            get_sender_id=lambda: "E2E_USER",
            get_sender_name=lambda: "用户",
            get_extra=lambda key, default=None: default,
        )

        task = asyncio.create_task(plugin.event_handlers.on_friend_message(event))
        await entered_judge.wait()
        # 在话题判定 await 期间发生一次更新时间戳的暂停。
        async with plugin.data_lock:
            plugin.session_data[UMO]["paused_at"] = 200.0
            plugin.session_data[UMO]["pause_reason"] = "negative_feedback"
        release_judge.set()
        await task

        assert plugin.scheduler_service.is_session_paused(UMO) is True
        assert plugin.session_data[UMO]["pause_reason"] == "negative_feedback"
        assert UMO not in plugin.scheduler.jobs

    asyncio.run(scenario())


def test_disabled_session_does_not_pollute_session_data_or_last_message_times(tmp_path):
    async def scenario():
        plugin = E2EPlugin(tmp_path)
        disabled_umo = "qq_official:FriendMessage:NOT_IN_LIST"
        event = SimpleNamespace(
            unified_msg_origin=disabled_umo,
            message_str="你好呀，在干嘛呢？",
            get_messages=lambda: [object()],
            get_self_id=lambda: "bot",
            get_sender_id=lambda: "NOT_IN_LIST",
            get_sender_name=lambda: "路人",
            get_extra=lambda key, default=None: default,
        )
        await plugin.event_handlers.on_friend_message(event)
        assert disabled_umo not in plugin.session_data
        assert disabled_umo not in plugin.last_message_times

    asyncio.run(scenario())


def test_umo_drift_preserves_configured_platform(tmp_path):
    plugin = E2EPlugin(tmp_path)
    # qq_official 暂时处于 STOPPED，aiocqhttp 处于 RUNNING；因 qq_official 在 session_list 中明确配置，不得漂移。
    plat_qq = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq_official"),
        status="stopped",
    )
    plat_cq = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="aiocqhttp"),
        status=PlatformStatus.RUNNING,
    )
    plugin.context.platform_manager = SimpleNamespace(platform_insts=[plat_qq, plat_cq])
    resolved = plugin.session_service.resolve_full_umo(
        "E2E_USER", "FriendMessage", preferred_platform="qq_official"
    )
    assert resolved == UMO


def test_manual_mutex_and_abort_pause_and_quiet_hours_retry(tmp_path):
    async def scenario():
        plugin = E2EPlugin(tmp_path, max_unanswered=2)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_run_locked(normalized_session_id, trigger):
            started.set()
            await release.wait()

        plugin.orchestrator._run_locked = slow_run_locked
        plugin.manual_trigger_sessions.add(UMO)
        first = asyncio.create_task(
            plugin.orchestrator.run(UMO, TriggerSource.MANUAL)
        )
        await started.wait()

        # 第二个重复 MANUAL 触发被跳过时，绝不能清除正在运行者的占用标记。
        await plugin.orchestrator.run(UMO, TriggerSource.MANUAL)
        assert UMO in plugin.manual_trigger_sessions

        release.set()
        await first
        assert UMO not in plugin.manual_trigger_sessions

        # _on_abort 遇到已达上限的会话时，转为暂停而不是继续排期。
        plugin.session_data[UMO] = {"unanswered_count": 2}
        ctx = SimpleNamespace(session_id=UMO)
        await plugin.orchestrator._on_abort(ctx, RuntimeError("boom"))
        assert plugin.scheduler_service.is_session_paused(UMO) is True
        assert UMO not in plugin.scheduler.jobs

    asyncio.run(scenario())

    # GatingStage quiet_hours 阻断时应设置 retry_after_seconds。
    plugin2 = E2EPlugin(tmp_path)
    plugin2.config["private_settings"]["schedule_settings"]["quiet_hours"] = "0-23"
    plugin2.rhythm_service = None
    stage = GatingStage(plugin2.orchestrator)
    ctx = SimpleNamespace(
        session_id=UMO,
        trigger=TriggerSource.SCHEDULE,
        unanswered_count=0,
        extra={},
        abort_reason="",
        abort=lambda r: setattr(ctx, "abort_reason", r),
    )
    result = asyncio.run(stage.handle(ctx))
    assert result is StageResult.RESCHEDULE
    assert ctx.abort_reason == "quiet_hours"
    assert ctx.extra["retry_after_seconds"] >= 60


def test_lifecycle_double_init_guard_and_terminate_cleans_tasks_without_telemetry(tmp_path):
    async def scenario():
        plugin = E2EPlugin(tmp_path)
        calls = 0

        async def count_init():
            nonlocal calls
            calls += 1

        plugin.scheduler_service.init_jobs_from_data = count_init
        plugin.scheduler_service.setup_auto_triggers_for_enabled_sessions = (
            lambda: _async_val(None)
        )
        plugin.context.platform_manager = SimpleNamespace(
            platform_insts=[
                SimpleNamespace(
                    meta=lambda: SimpleNamespace(id="qq_official"),
                    status=PlatformStatus.RUNNING,
                )
            ]
        )
        lifecycle = LifecycleService(plugin)
        await lifecycle.finalize_startup(allow_retry=True)
        await lifecycle.finalize_startup()
        assert calls == 1

        # 即使 telemetry 为 None / disabled，terminate() 也应取消并清理 _telemetry_tasks 中的业务任务。
        cancelled = False

        async def pending_job():
            nonlocal cancelled
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled = True
                raise

        plugin._track_task(asyncio.create_task(pending_job()))
        await asyncio.sleep(0)
        await lifecycle.terminate()
        assert cancelled is True
        assert plugin._telemetry_tasks == set()

    asyncio.run(scenario())


async def _async_val(value):
    return value
