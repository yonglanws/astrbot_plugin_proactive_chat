"""人格心情/精力状态测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from astrbot_plugin_proactive_chat.core.domain.humanization import ReplyQuality
from astrbot_plugin_proactive_chat.core.services.persona_state_service import (
    PersonaStateService,
)

UMO = "qq_official:FriendMessage:PERSONA1"
SETTINGS = {
    "humanization_settings": {
        "persona_settings": {
            "enable": True,
            "baseline_energy": 0.6,
            "low_energy_threshold": 0.35,
            "low_energy_probability_reduction": 0.5,
        }
    }
}


def ts(hour, minute=0):
    return datetime(2026, 1, 5, hour, minute, tzinfo=timezone.utc).timestamp()


class FakePlugin:
    timezone = timezone.utc

    def __init__(self):
        self.session_data = {}
        self.config = {"private_settings": SETTINGS}


def make_service():
    plugin = FakePlugin()
    return plugin, PersonaStateService(plugin)


class TestPersona:
    def test_night_energy_lower_than_afternoon(self):
        _, service = make_service()
        night = service.snapshot(UMO, now=ts(3))
        afternoon = service.snapshot(UMO, now=ts(15))
        # 两个时刻分别刷新，但由于状态会衰减，直接比较精力曲线更可靠。
        assert night["energy"] < afternoon["energy"]

    def test_activity_and_mood(self):
        _, service = make_service()
        snapshot = service.refresh(UMO, now=ts(23))
        assert snapshot["activity"]
        assert snapshot["mood"]

    def test_negative_feedback_lowers_energy(self):
        plugin, service = make_service()
        before = service.snapshot(UMO, now=ts(15))["energy"]
        service.record_user_message(
            UMO, ReplyQuality.NEGATIVE, now=ts(15, 5)
        )
        after = service.snapshot(UMO, now=ts(15, 6))["energy"]
        assert after < before

    def test_low_energy_reduces_probability(self):
        plugin, service = make_service()
        state = service.get_state(UMO, create=True)
        state["energy"] = 0.1
        state["updated_at"] = ts(15)
        multiplier = service.probability_multiplier(UMO, now=ts(15))
        assert multiplier < 1.0

    def test_disabled_returns_one(self):
        settings = {
            "humanization_settings": {
                "persona_settings": {"enable": False}
            }
        }
        plugin = FakePlugin()
        plugin.config = {"private_settings": settings}
        service = PersonaStateService(plugin)
        assert service.probability_multiplier(UMO, now=ts(15)) == 1.0
