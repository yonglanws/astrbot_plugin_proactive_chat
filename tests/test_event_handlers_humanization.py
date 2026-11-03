"""私聊事件处理：回复质量、负反馈暂停与敷衍计数测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.adapters.event_handlers import EventHandlers
from astrbot_plugin_proactive_chat.core.domain.humanization import FeedbackSettings

UMO = "qq_official:FriendMessage:EVENT2"
SETTINGS = {
    "enable": True,
    "schedule_settings": {"max_unanswered_times": 4, "min_interval_minutes": 30},
    "humanization_settings": {
        "feedback_settings": {
            "enable": True,
            "negative_cooldown_hours": 24,
            "perfunctory_no_reset": True,
            "min_substantive_length": 6,
        }
    },
}


class FakeSessionService:
    def normalize_session_id(self, umo):
        return umo

    def get_session_log_str(self, umo, config=None):
        return umo


class FakeConfigService:
    def get_session_config(self, umo):
        return SETTINGS


class FakeSchedulerService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.scheduled = []
        self.resume_calls = 0
        self.pause_calls = []

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

    async def pause_session(self, session_id, *, reason="max_unanswered", threshold=0, unanswered_count=0, cooldown_seconds=0):
        info = self.plugin.session_data.setdefault(session_id, {})
        info["proactive_paused"] = True
        info["pause_reason"] = reason
        info["paused_threshold"] = threshold
        info["paused_unanswered_count"] = unanswered_count
        if cooldown_seconds:
            info["cooldown_until"] = 1_800_000_000.0
        self.pause_calls.append((session_id, reason))
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
        self.reply_flags = []

    def get_settings(self, session_config=None):
        return SimpleNamespace()

    def record_user_message(self, session_id, text, **kwargs):
        self.messages.append((session_id, text))
        self.reply_flags.append(kwargs.get("counts_as_reply", True))
        return True


class FakeConversationStateService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.qualities = []

    def get_feedback_settings(self, session_config=None):
        return FeedbackSettings.from_dict(
            (session_config or {}).get("humanization_settings", {}).get(
                "feedback_settings"
            )
        )

    def record_user_message(self, session_id, quality, **kwargs):
        self.qualities.append((session_id, quality))
        self.plugin.session_data.setdefault(session_id, {})[
            "last_reply_quality"
        ] = quality.value
        return {"quality": quality.value}


class FakeRhythmService:
    def __init__(self):
        self.activities = []

    def record_user_activity(self, session_id, now=None):
        self.activities.append(session_id)
        return True


class FakePersonaStateService:
    def __init__(self):
        self.qualities = []

    def record_user_message(self, session_id, quality, **kwargs):
        self.qualities.append((session_id, quality))


class FakeStorage:
    def __init__(self):
        self.saves = 0

    async def save_data(self):
        self.saves += 1


class FakeTopicJudge:
    def __init__(self, decision=(True, {"new_topic": True})):
        self.decision = decision
        self.calls = []

    async def should_resume(self, session_id, text, session_config=None):
        self.calls.append((session_id, text))
        return self.decision


class FakePlugin:
    def __init__(self, *, paused=False, unanswered=2, decision=(True, {"new_topic": True})):
        self.session_data = {
            UMO: {
                "unanswered_count": unanswered,
                **(
                    {
                        "proactive_paused": True,
                        "paused_threshold": 4,
                        "paused_unanswered_count": 4,
                    }
                    if paused
                    else {}
                ),
            }
        }
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService()
        self.scheduler_service = FakeSchedulerService(self)
        self.scheduler = FakeScheduler()
        self.intensity_service = FakeIntensityService()
        self.conversation_state_service = FakeConversationStateService(self)
        self.rhythm_service = FakeRhythmService()
        self.persona_state_service = FakePersonaStateService()
        self.storage_service = FakeStorage()
        self.topic_judge_service = FakeTopicJudge(decision)
        self.data_lock = asyncio.Lock()
        self.first_message_logged = set()
        self.last_message_times = {}
        self.plugin_start_time = 0
        self._terminating = False


class FakeEvent:
    unified_msg_origin = UMO

    def __init__(self, text="刚看完那部电影，结尾有点意外"):
        self.message_str = text

    def get_messages(self):
        return [object()]

    def get_self_id(self):
        return "bot"


class TestReplyQualityFlow:
    def test_substantive_resets_counter(self):
        plugin = FakePlugin(unanswered=2)
        asyncio.run(EventHandlers(plugin).on_friend_message(FakeEvent()))
        assert plugin.scheduler_service.scheduled == [(UMO, True)]
        assert plugin.scheduler_service.pause_calls == []
        assert plugin.intensity_service.reply_flags == [True]

    def test_perfunctory_keeps_counter(self):
        plugin = FakePlugin(unanswered=2)
        asyncio.run(EventHandlers(plugin).on_friend_message(FakeEvent("嗯")))
        assert plugin.scheduler_service.scheduled == [(UMO, False)]
        assert plugin.session_data[UMO]["unanswered_count"] == 2
        assert plugin.session_data[UMO]["last_reply_quality"] == "perfunctory"
        assert plugin.intensity_service.reply_flags == [False]

    def test_negative_pauses_and_does_not_schedule(self):
        plugin = FakePlugin(unanswered=1)
        asyncio.run(EventHandlers(plugin).on_friend_message(FakeEvent("别发了，在忙")))
        assert plugin.scheduler_service.pause_calls == [(UMO, "negative_feedback")]
        assert plugin.scheduler_service.scheduled == []
        assert plugin.session_data[UMO]["proactive_paused"] is True
        assert plugin.conversation_state_service.qualities[-1][1].value == "negative"
        assert plugin.intensity_service.reply_flags == [False]


class TestPausedQualityGate:
    def test_paused_perfunctory_skips_llm_and_stays_paused(self):
        plugin = FakePlugin(paused=True, unanswered=4)
        asyncio.run(EventHandlers(plugin).on_friend_message(FakeEvent("嗯")))
        assert plugin.topic_judge_service.calls == []
        assert plugin.scheduler_service.scheduled == []
        assert plugin.session_data[UMO]["proactive_paused"] is True

    def test_paused_substantive_calls_judge_and_resumes(self):
        plugin = FakePlugin(
            paused=True,
            unanswered=4,
            decision=(True, {"new_topic": True, "reason": "new"}),
        )
        asyncio.run(
            EventHandlers(plugin).on_friend_message(
                FakeEvent("我们聊聊新出的那个游戏吧")
            )
        )
        assert len(plugin.topic_judge_service.calls) == 1
        assert plugin.scheduler_service.resume_calls == 1
        assert plugin.scheduler_service.scheduled == [(UMO, True)]
