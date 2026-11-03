"""互动强度服务测试：事件记录、回复结算、概率决策与调度。"""

from __future__ import annotations

import random
from datetime import timezone

import pytest

from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.services.intensity_service import IntensityService

UMO = "qq_official:FriendMessage:ABC123"


class FakeSessionService:
    def parse_session_id(self, umo):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo):
        return umo

    def get_session_name(self, umo, config=None):
        return ""

    def get_session_display_name(self, umo, config=None):
        return umo

    def get_session_log_str(self, umo, config=None):
        return umo


class FakeConfigService:
    def __init__(self, settings):
        self.settings = settings

    def get_private_settings(self):
        return self.settings

    def get_session_config(self, umo):
        parsed = SessionKey.parse(umo)
        if not parsed or "friend" not in parsed.message_type.lower():
            return None
        return {
            **self.settings,
            "enable": True,
            "_session_type": "friend",
        }


class FakePlugin:
    def __init__(self, settings=None):
        self.timezone = timezone.utc
        self.session_data = {}
        self.settings = settings or {
            "enable": True,
            "schedule_settings": {
                "min_interval_minutes": 30,
                "max_interval_minutes": 600,
            },
            "intensity_settings": {
                "enable": True,
                "probability_min": 15,
                "probability_max": 95,
            },
        }
        self.config = {"private_settings": self.settings}
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService(self.settings)
        self.intensity_service = IntensityService(self)


def make_service(settings=None):
    plugin = FakePlugin(settings)
    return plugin, plugin.intensity_service


class TestRecording:
    def test_record_user_messages(self):
        plugin, service = make_service()
        now = 1_700_000_000.0
        for index in range(10):
            service.record_user_message(
                UMO, f"消息 {index}？", now=now + index * 60
            )

        state = service.get_state(UMO)
        assert state["total_user_messages"] == 10
        assert len(state["events"]) == 10
        assert state["daily"]

        snapshot = service.evaluate(UMO, now=now + 600)
        assert snapshot["metrics"]["messages_24h"] == 10
        assert snapshot["metrics"]["total_user_messages"] == 10
        assert snapshot["has_state"] is True
        assert snapshot["score"] > 0
        assert 0 <= snapshot["probability"] <= 1

    def test_events_are_pruned(self):
        plugin, service = make_service()
        now = 1_700_000_000.0
        for index in range(260):
            service.record_user_message(UMO, "hi", now=now + index)
        assert len(service.get_state(UMO)["events"]) == 200

    def test_own_proactive_message_reply_is_counted(self):
        plugin, service = make_service()
        sent_at = 1_700_000_000.0
        service.record_proactive_sent(UMO, now=sent_at)
        service.record_user_message(UMO, "我在", now=sent_at + 3600)

        snapshot = service.evaluate(UMO, now=sent_at + 7200)
        assert snapshot["metrics"]["reply_rate"] == 1.0
        assert snapshot["metrics"]["avg_reply_minutes"] == pytest.approx(60.0)
        assert snapshot["metrics"]["proactive_count_30d"] == 1

    def test_late_reply_is_missed(self):
        plugin, service = make_service()
        sent_at = 1_700_000_000.0
        service.record_proactive_sent(UMO, now=sent_at)
        service.record_user_message(UMO, "才看到", now=sent_at + 7 * 3600)

        snapshot = service.evaluate(UMO, now=sent_at + 8 * 3600)
        assert snapshot["metrics"]["reply_rate"] == 0.0
        assert snapshot["metrics"]["avg_reply_minutes"] is None

    def test_pending_proactive_is_not_counted_until_window_passes(self):
        plugin, service = make_service()
        sent_at = 1_700_000_000.0
        service.record_proactive_sent(UMO, now=sent_at)

        during = service.evaluate(UMO, now=sent_at + 3600)
        assert during["metrics"]["reply_rate"] is None

        after = service.evaluate(UMO, now=sent_at + 7 * 3600)
        assert after["metrics"]["reply_rate"] == 0.0

    def test_perfunctory_reply_does_not_settle_proactive(self):
        plugin, service = make_service()
        sent_at = 1_700_000_000.0
        service.record_proactive_sent(UMO, now=sent_at)
        service.record_user_message(
            UMO, "嗯", now=sent_at + 60, counts_as_reply=False
        )

        state = service.get_state(UMO)
        assert state["proactive"][-1]["replied_at"] is None

        after = service.evaluate(UMO, now=sent_at + 7 * 3600)
        assert after["metrics"]["reply_rate"] == 0.0

    def test_substantive_reply_settles_proactive(self):
        plugin, service = make_service()
        sent_at = 1_700_000_000.0
        service.record_proactive_sent(UMO, now=sent_at)
        service.record_user_message(
            UMO, "刚看到，面试过了", now=sent_at + 60
        )

        state = service.get_state(UMO)
        assert state["proactive"][-1]["replied_at"] is not None


class TestDecisions:
    def test_probability_zero_skips(self):
        settings = {
            "enable": True,
            "schedule_settings": {"min_interval_minutes": 30, "max_interval_minutes": 600},
            "intensity_settings": {
                "enable": True,
                "probability_min": 0,
                "probability_max": 0,
            },
        }
        plugin, service = make_service(settings)
        now = 1_700_000_000.0
        service.record_user_message(UMO, "你好", now=now)
        will_send, snapshot = service.decide(UMO, now=now + 60, rng=random.Random(1))
        assert will_send is False
        assert snapshot["retry_after_seconds"] >= 60
        assert service.get_state(UMO)["last_decision"]["will_send"] is False

    def test_probability_hundred_sends(self):
        settings = {
            "enable": True,
            "schedule_settings": {"min_interval_minutes": 30, "max_interval_minutes": 600},
            "intensity_settings": {
                "enable": True,
                "probability_min": 100,
                "probability_max": 100,
            },
        }
        plugin, service = make_service(settings)
        now = 1_700_000_000.0
        will_send, snapshot = service.decide(UMO, now=now, rng=random.Random(1))
        assert will_send is True
        assert snapshot["retry_after_seconds"] == 0

    def test_disabled_intensity_always_sends(self):
        settings = {
            "enable": True,
            "schedule_settings": {"min_interval_minutes": 30, "max_interval_minutes": 600},
            "intensity_settings": {"enable": False},
        }
        plugin, service = make_service(settings)
        will_send, snapshot = service.decide(UMO, now=1_700_000_000.0)
        assert will_send is True
        assert snapshot["skip_reason"] == "intensity_disabled"
        assert snapshot["probability_percent"] == 100

        info = service.next_interval(UMO, now=1_700_000_000.0)
        assert info["applied"] is False
        assert info["min_seconds"] == 30 * 60
        assert info["max_seconds"] == 600 * 60


class TestScheduling:
    def test_next_interval_is_within_effective_range(self):
        plugin, service = make_service()
        now = 1_700_000_000.0
        for index in range(12):
            service.record_user_message(UMO, "在吗？", now=now + index * 30)

        info = service.next_interval(UMO, now=now + 600, rng=random.Random(7))
        assert info["min_seconds"] <= info["random_seconds"] <= info["max_seconds"]
        assert info["min_seconds"] >= 8 * 60

    def test_hot_user_gets_shorter_interval_than_cold(self):
        settings = {
            "enable": True,
            "schedule_settings": {"min_interval_minutes": 30, "max_interval_minutes": 600},
            "intensity_settings": {
                "enable": True,
                "probability_min": 15,
                "probability_max": 95,
                "interval_scale_hot": 35,
                "interval_scale_cold": 115,
                "min_interval_floor_minutes": 1,
            },
        }
        plugin, service = make_service(settings)
        now = 1_700_000_000.0
        hot_umo = "qq_official:FriendMessage:HOT"
        for index in range(80):
            service.record_user_message(hot_umo, "哈哈哈哈这个好有意思！", now=now + index * 20)

        cold_umo = "qq_official:FriendMessage:COLD"
        service.record_user_message(cold_umo, "嗯", now=now)

        hot_info = service.next_interval(hot_umo, now=now + 2000)
        cold_info = service.next_interval(cold_umo, now=now + 2000)
        assert hot_info["score"] > cold_info["score"]
        assert hot_info["min_seconds"] < cold_info["min_seconds"]


class TestSeeding:
    def test_seed_state_roundtrip(self):
        plugin, service = make_service()
        payload = {
            "events": [{"t": 1_700_000_000.0, "n": 10, "q": 1, "e": 0, "m": 0}],
            "daily": {"2023-11-14": 5},
            "total_user_messages": 42,
            "first_seen": 1_690_000_000.0,
            "last_user_ts": 1_700_000_000.0,
            "source": "astrbot_db",
            "platform": "qq_official",
        }
        assert service.seed_state(UMO, payload) is True
        state = service.get_state(UMO)
        assert state["total_user_messages"] == 42
        assert state["seed"]["source"] == "astrbot_db"
        assert len(state["events"]) == 1

        snapshot = service.evaluate(UMO, now=1_700_000_100.0)
        assert snapshot["seed_source"] == "astrbot_db"
        assert snapshot["metrics"]["total_user_messages"] == 42

    def test_collect_snapshots_sorted(self):
        plugin, service = make_service()
        now = 1_700_000_000.0
        for index in range(30):
            service.record_user_message(UMO, "热聊中", now=now + index * 10)
        other = "qq_official:FriendMessage:OTHER"
        service.record_user_message(other, "嗯", now=now + 5000)

        snapshots = service.collect_snapshots([UMO, other], now=now + 6000)
        assert [item["session_id"] for item in snapshots] == [UMO, other]
        assert snapshots[0]["score"] > snapshots[1]["score"]
