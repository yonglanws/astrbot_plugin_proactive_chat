"""调度器与作息节律的集成测试。"""

from __future__ import annotations

from datetime import timezone

from astrbot_plugin_proactive_chat.core.services.scheduler_service import SchedulerService

UMO = "qq_official:FriendMessage:SCHED1"
SETTINGS = {
    "enable": True,
    "schedule_settings": {
        "min_interval_minutes": 30,
        "max_interval_minutes": 60,
        "quiet_hours": "1-7",
    },
}


class FakeIntensityService:
    def next_interval(self, session_id, schedule_conf):
        return {
            "min_seconds": 1800,
            "max_seconds": 3600,
            "random_seconds": 1800,
            "score": 50,
            "tier": "warm",
            "probability": 0.5,
            "scale": 1.0,
            "applied": True,
        }


class FakeRhythmService:
    def __init__(self):
        self.calls = []

    def adjust_trigger(self, session_id, candidate_ts, **kwargs):
        self.calls.append((session_id, candidate_ts, kwargs))
        return candidate_ts + 3600, {
            "adjusted": True,
            "reason": "active_hours",
        }


class FakeConfigService:
    def get_session_config(self, session_id):
        return SETTINGS


class FakePlugin:
    timezone = timezone.utc

    def __init__(self, with_rhythm=True):
        self.intensity_service = FakeIntensityService()
        self.rhythm_service = FakeRhythmService() if with_rhythm else None
        self.config_service = FakeConfigService()
        self.session_data = {}


class TestComputeNextTrigger:
    def test_rhythm_adjustment_applied(self):
        plugin = FakePlugin()
        service = SchedulerService(plugin)
        (
            run_date,
            scheduled_at,
            min_interval,
            max_interval,
            random_interval,
            next_trigger_time,
            score,
        ) = service.compute_next_trigger(
            SETTINGS["schedule_settings"], session_id=UMO
        )
        assert plugin.rhythm_service.calls
        assert next_trigger_time >= scheduled_at + 3600
        assert random_interval >= 3600
        assert score == 50

    def test_no_rhythm_service(self):
        plugin = FakePlugin(with_rhythm=False)
        service = SchedulerService(plugin)
        (
            _run_date,
            scheduled_at,
            _min_interval,
            _max_interval,
            random_interval,
            next_trigger_time,
            _score,
        ) = service.compute_next_trigger(
            SETTINGS["schedule_settings"], session_id=UMO
        )
        assert 1800 <= random_interval <= 3600
        assert next_trigger_time == scheduled_at + random_interval
