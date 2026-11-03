"""私聊事件处理：暂停后话题判定与恢复行为测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.adapters.event_handlers import EventHandlers

UMO = "qq_official:FriendMessage:EVENT1"


class FakeSessionService:
    def normalize_session_id(self, umo):
        return umo

    def get_session_log_str(self, umo, config=None):
        return umo


class FakeConfigService:
    def get_session_config(self, umo):
        return {
            "enable": True,
            "pause_settings": {"resume_mode": "llm_topic"},
        }


class FakeSchedulerService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.scheduled = []
        self.resume_calls = 0

    def is_session_paused(self, session_id):
        return bool(self.plugin.session_data.get(session_id, {}).get("proactive_paused"))

    def resume_session(self, session_id):
        info = self.plugin.session_data.get(session_id, {})
        if not info.get("proactive_paused"):
            return False
        for key in (
            "proactive_paused",
            "paused_at",
            "pause_reason",
            "paused_unanswered_count",
            "paused_threshold",
        ):
            info.pop(key, None)
        self.resume_calls += 1
        return True

    async def cancel_all_related_auto_triggers(self, session_id):
        return False

    async def schedule_next_chat_and_save(self, session_id, reset_counter=False):
        self.scheduled.append((session_id, reset_counter))
        if reset_counter:
            self.plugin.session_data.setdefault(session_id, {})["unanswered_count"] = 0

    def purge_related_jobs(self, session_id):
        return None

    def is_persisted_task_still_valid(self, *args, **kwargs):
        return False


class FakeScheduler:
    def remove_job(self, session_id):
        return None


class FakeIntensityService:
    def __init__(self):
        self.messages = []

    def get_settings(self, session_config=None):
        return SimpleNamespace()

    def record_user_message(self, session_id, text, **kwargs):
        self.messages.append((session_id, text))
        return True


class FakeStorage:
    def __init__(self):
        self.saves = 0

    async def save_data(self):
        self.saves += 1


class FakeTopicJudge:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def should_resume(self, session_id, text, session_config=None):
        self.calls.append((session_id, text))
        return self.decision


class FakePlugin:
    def __init__(self, decision):
        self.session_data = {
            UMO: {
                "unanswered_count": 4,
                "proactive_paused": True,
                "paused_threshold": 4,
                "paused_unanswered_count": 4,
            }
        }
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService()
        self.scheduler_service = FakeSchedulerService(self)
        self.scheduler = FakeScheduler()
        self.intensity_service = FakeIntensityService()
        self.storage_service = FakeStorage()
        self.topic_judge_service = FakeTopicJudge(decision)
        self.data_lock = asyncio.Lock()
        self.first_message_logged = set()
        self.last_message_times = {}
        self.plugin_start_time = 0
        self._terminating = False


class FakeEvent:
    unified_msg_origin = UMO

    def __init__(self, text="我们换个话题吧，聊聊新游戏"):
        self.message_str = text

    def get_messages(self):
        return [object()]

    def get_self_id(self):
        return "bot"


def test_paused_new_topic_resumes_and_schedules():
    plugin = FakePlugin((True, {"new_topic": True, "reason": "new"}))
    handler = EventHandlers(plugin)
    asyncio.run(handler.on_friend_message(FakeEvent()))

    info = plugin.session_data[UMO]
    assert "proactive_paused" not in info
    assert plugin.scheduler_service.resume_calls == 1
    assert plugin.scheduler_service.scheduled == [(UMO, True)]
    assert info["unanswered_count"] == 0
    assert info["last_topic_judge"]["new_topic"] is True


def test_paused_old_topic_stays_paused():
    plugin = FakePlugin((False, {"new_topic": False, "reason": "旧话题"}))
    handler = EventHandlers(plugin)
    asyncio.run(handler.on_friend_message(FakeEvent("你怎么还不回我")))

    info = plugin.session_data[UMO]
    assert info["proactive_paused"] is True
    assert plugin.scheduler_service.resume_calls == 0
    assert plugin.scheduler_service.scheduled == []
    assert info["unanswered_count"] == 4
    assert info["last_topic_judge"]["new_topic"] is False
    assert plugin.storage_service.saves >= 1


def test_not_paused_skips_topic_judge():
    plugin = FakePlugin((False, {"new_topic": False}))
    plugin.session_data[UMO].pop("proactive_paused", None)
    handler = EventHandlers(plugin)
    asyncio.run(handler.on_friend_message(FakeEvent()))

    assert plugin.topic_judge_service.calls == []
    assert plugin.scheduler_service.scheduled == [(UMO, True)]
