"""调度器强度修正与私聊守卫测试。"""

from __future__ import annotations

import asyncio
import time
from datetime import timezone
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.domain.domain_enums import TriggerSource
from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.services.config_service import ConfigService
from astrbot_plugin_proactive_chat.core.services.scheduler_service import SchedulerService

PRIVATE_UMO = "qq_official:FriendMessage:SCHED1"
GROUP_UMO = "qq_official:GroupMessage:GROUP1"


class FakeSessionService:
    def parse_session_id(self, umo):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo):
        return umo

    def resolve_full_umo(self, target_id, msg_type, preferred_platform=None):
        return f"qq_official:{msg_type}:{target_id}"

    def get_session_log_str(self, umo, config=None):
        return umo

    def get_session_name(self, umo, config=None):
        return ""


class FakeIntensityService:
    def __init__(self):
        self.calls = []

    def get_settings(self, session_config=None):
        return SimpleNamespace(enabled=True, apply_to_auto_trigger=True)

    def next_interval(self, session_id, schedule_conf):
        self.calls.append(session_id)
        return {
            "min_seconds": 300,
            "max_seconds": 900,
            "random_seconds": 600,
            "score": 88.0,
            "tier": "blazing",
            "probability": 0.9,
            "scale": 0.35,
        }


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger, **kwargs):
        self.jobs[kwargs["id"]] = kwargs

    def get_jobs(self):
        return []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)


class FakeStorage:
    async def save_data(self):
        return None


class FakePlugin:
    def __init__(self):
        self.timezone = timezone.utc
        self.session_data = {}
        self.scheduler = FakeScheduler()
        self.session_service = FakeSessionService()
        self.intensity_service = FakeIntensityService()
        self.config_service = ConfigService(self)
        self.data_lock = asyncio.Lock()
        self.storage_service = FakeStorage()
        self.auto_trigger_timers = {}
        self.last_message_times = {}
        self.manual_trigger_sessions = set()
        self.plugin_start_time = time.time()
        self._terminating = False
        self.config = {
            "private_settings": {
                "enable": True,
                "session_list": [PRIVATE_UMO],
                "schedule_settings": {
                    "min_interval_minutes": 30,
                    "max_interval_minutes": 600,
                },
                "intensity_settings": {"enable": True},
            }
        }

    async def check_and_chat(self, session_id, trigger=None):
        return None


def make_plugin():
    return FakePlugin()


class TestComputeNextTrigger:
    def test_uses_intensity_scaled_range(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        result = scheduler.compute_next_trigger(
            {"min_interval_minutes": 30, "max_interval_minutes": 600},
            session_id=PRIVATE_UMO,
        )
        assert len(result) == 7
        run_date, scheduled_at, min_interval, max_interval, random_interval, next_trigger, score = result
        assert min_interval == 300
        assert max_interval == 900
        assert random_interval == 600
        assert score == 88.0
        assert plugin.intensity_service.calls == [PRIVATE_UMO]

    def test_forced_interval_overrides_intensity(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        result = scheduler.compute_next_trigger(
            {"min_interval_minutes": 30, "max_interval_minutes": 600},
            session_id=PRIVATE_UMO,
            forced_interval_seconds=120,
        )
        assert result[4] == 120
        assert result[6] is None
        assert plugin.intensity_service.calls == []

    def test_without_session_uses_base_range(self):
        plugin = make_plugin()
        plugin.intensity_service = None
        scheduler = SchedulerService(plugin)
        result = scheduler.compute_next_trigger(
            {"min_interval_minutes": 1, "max_interval_minutes": 2},
        )
        assert 60 <= result[4] <= 120


class TestPrivateGuard:
    def test_group_umo_has_no_config(self):
        plugin = make_plugin()
        assert plugin.config_service.get_session_config(GROUP_UMO) is None

    def test_group_job_is_rejected(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        run_date = scheduler.compute_next_trigger({})[0]
        scheduler.add_chat_job(GROUP_UMO, run_date)
        assert GROUP_UMO not in plugin.scheduler.jobs

    def test_private_job_is_added(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        run_date = scheduler.compute_next_trigger({})[0]
        scheduler.add_chat_job(PRIVATE_UMO, run_date)
        assert PRIVATE_UMO in plugin.scheduler.jobs
        assert plugin.scheduler.jobs[PRIVATE_UMO]["args"] == [
            PRIVATE_UMO,
            TriggerSource.SCHEDULE,
        ]

    def test_auto_trigger_flag_propagates(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        run_date = scheduler.compute_next_trigger({})[0]
        scheduler.add_chat_job(
            PRIVATE_UMO, run_date, trigger=TriggerSource.AUTO_TRIGGER
        )
        assert plugin.scheduler.jobs[PRIVATE_UMO]["args"] == [
            PRIVATE_UMO,
            TriggerSource.AUTO_TRIGGER,
        ]


class TestConfigMigration:
    def test_legacy_friend_settings_migrated(self):
        plugin = make_plugin()
        plugin.config = {
            "friend_settings": {
                "enable": False,
                "session_list": [PRIVATE_UMO],
            }
        }
        service = ConfigService(plugin)
        assert service.ensure_private_settings() is True
        assert plugin.config["private_settings"]["session_list"] == [PRIVATE_UMO]
        assert service.get_private_settings()["enable"] is False

    def test_group_config_returns_none(self):
        plugin = make_plugin()
        assert plugin.config_service.get_base_session_config(GROUP_UMO) is None
        assert plugin.config_service.get_base_session_config(PRIVATE_UMO) is not None


class TestUnansweredPause:
    def test_pause_sets_state_and_clears_schedule(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        plugin.session_data[PRIVATE_UMO] = {
            "unanswered_count": 4,
            "next_trigger_time": time.time() + 600,
        }
        asyncio.run(
            scheduler.pause_session(
                PRIVATE_UMO, threshold=4, unanswered_count=4
            )
        )
        info = plugin.session_data[PRIVATE_UMO]
        assert scheduler.is_session_paused(PRIVATE_UMO) is True
        assert info["paused_threshold"] == 4
        assert info["paused_unanswered_count"] == 4
        assert "next_trigger_time" not in info

    def test_pause_blocks_new_schedule(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        asyncio.run(
            scheduler.pause_session(
                PRIVATE_UMO, threshold=4, unanswered_count=4
            )
        )
        asyncio.run(
            scheduler.schedule_next_chat_and_save(PRIVATE_UMO, reset_counter=True)
        )
        assert PRIVATE_UMO not in plugin.scheduler.jobs

    def test_resume_clears_pause_and_allows_schedule(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        asyncio.run(
            scheduler.pause_session(
                PRIVATE_UMO, threshold=4, unanswered_count=4
            )
        )
        assert scheduler.resume_session(PRIVATE_UMO) is True
        assert scheduler.is_session_paused(PRIVATE_UMO) is False
        assert plugin.session_data[PRIVATE_UMO].get("proactive_paused") is None

        asyncio.run(
            scheduler.schedule_next_chat_and_save(PRIVATE_UMO, reset_counter=True)
        )
        assert PRIVATE_UMO in plugin.scheduler.jobs
        assert plugin.session_data[PRIVATE_UMO]["unanswered_count"] == 0


class TestNegativeCooldown:
    def test_cooldown_clamps_next_trigger(self):
        plugin = make_plugin()
        cooldown_until = time.time() + 7200
        plugin.session_data[PRIVATE_UMO] = {"cooldown_until": cooldown_until}
        scheduler = SchedulerService(plugin)
        result = scheduler.compute_next_trigger(
            {"min_interval_minutes": 1, "max_interval_minutes": 2},
            session_id=PRIVATE_UMO,
        )
        assert result[5] >= cooldown_until

    def test_pause_sets_cooldown_and_resume_keeps_it(self):
        plugin = make_plugin()
        scheduler = SchedulerService(plugin)
        asyncio.run(
            scheduler.pause_session(
                PRIVATE_UMO,
                reason="negative_feedback",
                cooldown_seconds=3600,
            )
        )
        assert scheduler.is_session_paused(PRIVATE_UMO) is True
        assert scheduler.is_in_cooldown(PRIVATE_UMO) is True
        assert scheduler.cooldown_remaining(PRIVATE_UMO) > 3000

        assert scheduler.resume_session(PRIVATE_UMO) is True
        assert scheduler.is_session_paused(PRIVATE_UMO) is False
        assert scheduler.is_in_cooldown(PRIVATE_UMO) is True

        plugin.session_data[PRIVATE_UMO]["proactive_paused"] = True
        assert (
            scheduler.resume_session(PRIVATE_UMO, clear_cooldown=True) is True
        )
        assert scheduler.is_in_cooldown(PRIVATE_UMO) is False

    def test_clear_cooldown_without_pause(self):
        plugin = make_plugin()
        plugin.session_data[PRIVATE_UMO] = {"cooldown_until": time.time() + 600}
        scheduler = SchedulerService(plugin)
        assert scheduler.resume_session(PRIVATE_UMO, clear_cooldown=True) is True
        assert scheduler.is_in_cooldown(PRIVATE_UMO) is False
