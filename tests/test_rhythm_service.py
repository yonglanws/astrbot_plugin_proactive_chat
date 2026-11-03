"""作息节律服务测试：活跃时段学习、每日预算、最小间隔与免打扰。"""

from __future__ import annotations

from datetime import datetime, timezone

from astrbot_plugin_proactive_chat.core.services.rhythm_service import RhythmService

UMO = "qq_official:FriendMessage:RHYTHM1"
SETTINGS = {
    "humanization_settings": {
        "rhythm_settings": {
            "enable": True,
            "learn_active_hours": True,
            "min_messages_to_learn": 3,
            "active_hours_margin_minutes": 0,
            "daily_proactive_limit": 6,
            "min_gap_minutes": 30,
        }
    }
}


def ts(hour, minute=0, day=5):
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc).timestamp()


class FakeConfigService:
    def get_session_config(self, session_id):
        return {**SETTINGS, "enable": True}


class FakePlugin:
    timezone = timezone.utc

    def __init__(self, settings=None):
        self.session_data = {}
        self.config = {"private_settings": settings or SETTINGS}
        self.config_service = FakeConfigService()
        self.intensity_service = None


def make_service(settings=None):
    plugin = FakePlugin(settings)
    return plugin, RhythmService(plugin)


def seed_active_hours(service):
    service.seed_from_timestamps(
        UMO,
        [
            ts(9),
            ts(9, 30),
            ts(10),
            ts(10, 20),
            ts(21),
            ts(21, 40),
        ],
    )


class TestLearning:
    def test_learns_active_hours(self):
        _, service = make_service()
        seed_active_hours(service)
        hours = service.learned_active_hours(UMO)
        assert 9 in hours
        assert 10 in hours
        assert 21 in hours

    def test_not_enough_samples(self):
        settings = {
            "humanization_settings": {
                "rhythm_settings": {
                    "min_messages_to_learn": 20,
                    "learn_active_hours": True,
                }
            }
        }
        _, service = make_service(settings)
        seed_active_hours(service)
        assert service.learned_active_hours(UMO) == []

    def test_snapshot_contains_hours(self):
        _, service = make_service()
        seed_active_hours(service)
        snapshot = service.snapshot(UMO, now=ts(12))
        assert snapshot["learned"] is True
        assert snapshot["active_hours_label"]


class TestAdjustTrigger:
    def test_moves_outside_active_hours(self):
        _, service = make_service()
        seed_active_hours(service)
        candidate = ts(3)
        adjusted, meta = service.adjust_trigger(UMO, candidate, now=ts(2))
        assert meta["adjusted"] is True
        assert datetime.fromtimestamp(adjusted, timezone.utc).hour == 9

    def test_min_gap(self):
        _, service = make_service()
        state = service.get_state(UMO, create=True)
        state["last_proactive_ts"] = ts(10)
        candidate = ts(10, 10)
        adjusted, meta = service.adjust_trigger(UMO, candidate, now=ts(10, 5))
        assert adjusted >= ts(10, 30)
        assert "min_gap" in meta["reason"]

    def test_daily_budget_moves_to_next_day(self):
        settings = {
            "humanization_settings": {
                "rhythm_settings": {
                    "enable": True,
                    "learn_active_hours": False,
                    "daily_proactive_limit": 2,
                    "min_gap_minutes": 0,
                }
            }
        }
        _, service = make_service(settings)
        state = service.get_state(UMO, create=True)
        state["daily_proactive"] = {"2026-01-05": 2}
        adjusted, meta = service.adjust_trigger(
            UMO, ts(12), now=ts(11)
        )
        assert meta["reason"] == "daily_budget"
        assert datetime.fromtimestamp(adjusted, timezone.utc).day == 6

    def test_quiet_hours_when_no_samples(self):
        settings = {
            "humanization_settings": {
                "rhythm_settings": {
                    "enable": True,
                    "learn_active_hours": False,
                    "min_gap_minutes": 0,
                }
            }
        }
        _, service = make_service(settings)
        adjusted, meta = service.adjust_trigger(
            UMO,
            ts(3),
            now=ts(2),
            schedule_conf={"quiet_hours": "1-7"},
        )
        assert datetime.fromtimestamp(adjusted, timezone.utc).hour == 7
        assert meta["reason"] == "quiet_hours"


class TestCanSendNow:
    def test_outside_active_hours_blocked(self):
        _, service = make_service()
        seed_active_hours(service)
        allowed, reason, delay = service.can_send_now(UMO, now=ts(3))
        assert allowed is False
        assert reason == "active_hours"
        assert delay > 0

    def test_manual_bypasses(self):
        _, service = make_service()
        seed_active_hours(service)
        allowed, reason, delay = service.can_send_now(UMO, now=ts(3), manual=True)
        assert allowed is True
        assert reason == "manual"
        assert delay == 0

    def test_daily_budget_blocked(self):
        settings = {
            "humanization_settings": {
                "rhythm_settings": {
                    "enable": True,
                    "learn_active_hours": False,
                    "daily_proactive_limit": 1,
                    "min_gap_minutes": 0,
                }
            }
        }
        _, service = make_service(settings)
        service.record_proactive_sent(UMO, now=ts(10))
        allowed, reason, _ = service.can_send_now(UMO, now=ts(11))
        assert allowed is False
        assert reason == "daily_budget"
