"""人格状态服务：用轻量的心情/精力/活动状态减少“永远在线”的机械感。

状态保存在 ``session_data[umo]["persona_state"]``。它不是复杂的情绪模拟，
只做三件事：
- 让精力随时段自然起伏；
- 让负反馈/连续未回复降低精力与主动意愿；
- 把状态写进提示词，让语气和长度随状态变化。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping

from ..domain.humanization import (
    PersonaSettings,
    ReplyQuality,
    clamp,
    persona_energy_curve,
)

PERSONA_STATE_KEY = "persona_state"
STATE_VERSION = 1

MOOD_LABELS = {
    "calm": "平静",
    "happy": "轻快",
    "tired": "有点累",
    "thoughtful": "若有所思",
    "quiet": "安静",
}


class PersonaStateService:
    """心情、精力与当前活动状态。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def get_private_settings(self) -> dict[str, Any]:
        config = getattr(self.plugin, "config", {}) or {}
        for key in ("private_settings", "friend_settings"):
            value = config.get(key) if hasattr(config, "get") else None
            if isinstance(value, dict):
                return value
        return {}

    def get_settings(
        self, session_config: Mapping[str, Any] | None = None
    ) -> PersonaSettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            humanization = session_config.get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("persona_settings")
        if not isinstance(raw, Mapping):
            humanization = self.get_private_settings().get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("persona_settings")
        return PersonaSettings.from_dict(raw)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "energy": 0.6,
            "mood": "calm",
            "activity": "日常",
            "updated_at": 0.0,
            "last_event_at": 0.0,
        }

    def get_state(self, session_id: str, *, create: bool = False) -> dict[str, Any]:
        session_data = getattr(self.plugin, "session_data", None)
        if not isinstance(session_data, dict):
            return self.default_state()
        payload = session_data.get(session_id)
        if not isinstance(payload, dict):
            if not create:
                return self.default_state()
            payload = {}
            session_data[session_id] = payload
        state = payload.get(PERSONA_STATE_KEY)
        if not isinstance(state, dict):
            if not create:
                return self.default_state()
            state = self.default_state()
            payload[PERSONA_STATE_KEY] = state
        return state

    def _local_datetime(self, timestamp: float) -> datetime:
        try:
            return datetime.fromtimestamp(
                timestamp, tz=getattr(self.plugin, "timezone", None)
            )
        except (OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(timestamp)

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def refresh(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
        read_only: bool = False,
    ) -> dict[str, Any]:
        """纯时间衰减与昼夜精力刷新（幂等，不重复扣除事件惩罚）。"""
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        if read_only:
            existing = self.get_state(session_id, create=False)
            state = dict(existing)
        else:
            state = self.get_state(session_id, create=True)
        baseline = settings.baseline_energy
        local = self._local_datetime(timestamp)
        curve = persona_energy_curve(local.hour)
        is_weekend = local.weekday() >= 5

        if not settings.enabled:
            state["energy"] = baseline
            state["mood"] = "calm"
            state["activity"] = "日常"
            state["updated_at"] = timestamp
            return state

        previous = clamp(float(state.get("energy", baseline) or baseline))
        last_update = float(state.get("updated_at", 0) or 0)
        hours = max(0.0, (timestamp - last_update) / 3600.0) if last_update else 0.0
        # 精力以 6 小时为半衰期向当前时段曲线与基线的平衡点回归，完全幂等。
        target = clamp(baseline * 0.4 + curve * 0.6)
        if last_update <= 0:
            energy = target
        else:
            decay_weight = 0.5 ** (hours / 6.0)
            energy = clamp(target + (previous - target) * decay_weight)

        payload = (
            getattr(self.plugin, "session_data", {}).get(session_id, {})
            if isinstance(getattr(self.plugin, "session_data", {}), dict)
            else {}
        )
        quality = ""
        conversation_state = payload.get("conversation_state")
        if isinstance(conversation_state, dict):
            quality = str(conversation_state.get("last_user_quality") or "")

        state["energy"] = round(energy, 4)
        state["updated_at"] = timestamp
        state["mood"] = self._derive_mood(
            energy, quality, settings.low_energy_threshold
        )
        state["activity"] = self._derive_activity(local.hour, is_weekend=is_weekend)
        return state

    @staticmethod
    def _derive_mood(energy: float, quality: str, low_threshold: float) -> str:
        if quality == ReplyQuality.NEGATIVE.value:
            return "quiet"
        if energy <= low_threshold:
            return "tired"
        if energy >= 0.78 and quality in {
            ReplyQuality.SUBSTANTIVE.value,
            ReplyQuality.QUESTION.value,
        }:
            return "happy"
        if quality == ReplyQuality.PERFUNCTORY.value:
            return "thoughtful"
        return "calm"

    @staticmethod
    def _derive_activity(hour: int, *, is_weekend: bool = False) -> str:
        """返回中性时段状态描述，不预设睡眠或进食等具体作息（由人格设定自行决定）。"""
        hour = int(hour) % 24
        if is_weekend:
            if hour < 7:
                return "周末深夜时段，四周比较安静"
            if hour < 10:
                return "周末上午，节奏比较松散"
            if hour < 13:
                return "周末午间，空余时间较多"
            if hour < 18:
                return "周末下午，在忙自己的事或随手刷手机"
            if hour < 22:
                return "周末傍晚到晚间，节奏轻松"
            return "周末夜深了，四周安静下来"
        if hour < 6:
            return "深夜时段，四周很安静"
        if hour < 9:
            return "清晨到早间，新的一天刚开始"
        if hour < 12:
            return "上午，在忙自己的事"
        if hour < 14:
            return "午间空档，节奏稍微放缓"
        if hour < 18:
            return "下午，在忙但偶尔会看手机"
        if hour < 22:
            return "晚间，节奏慢了下来"
        return "夜深了，四周比较安静"

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def record_user_message(
        self,
        session_id: str,
        quality: ReplyQuality,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> None:
        timestamp = float(now if now is not None else time.time())
        if not self.get_settings(session_config).enabled:
            return
        state = self.get_state(session_id, create=True)
        self.refresh(
            session_id, session_config=session_config, now=timestamp
        )
        energy = float(state.get("energy", 0.6) or 0.6)
        if quality is ReplyQuality.NEGATIVE:
            energy -= 0.15
        elif quality is ReplyQuality.PERFUNCTORY:
            energy -= 0.04
        else:
            energy += 0.06
        state["energy"] = round(clamp(energy), 4)
        state["mood"] = self._derive_mood(
            state["energy"],
            quality.value,
            self.get_settings(session_config).low_energy_threshold,
        )
        state["last_event_at"] = timestamp
        state["updated_at"] = timestamp

    def record_proactive_sent(
        self,
        session_id: str,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> None:
        timestamp = float(now if now is not None else time.time())
        if not self.get_settings(session_config).enabled:
            return
        state = self.get_state(session_id, create=True)
        self.refresh(
            session_id, session_config=session_config, now=timestamp
        )
        payload = (
            getattr(self.plugin, "session_data", {}).get(session_id, {})
            if isinstance(getattr(self.plugin, "session_data", {}), dict)
            else {}
        )
        unanswered = int(payload.get("unanswered_count", 0) or 0)
        fatigue = 0.03 + min(0.08, max(0, unanswered - 1) * 0.02)
        state["energy"] = round(
            clamp(float(state.get("energy", 0.6) or 0.6) - fatigue), 4
        )
        state["last_event_at"] = timestamp
        state["updated_at"] = timestamp

    # ------------------------------------------------------------------
    # 概率与快照
    # ------------------------------------------------------------------
    def probability_multiplier(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
        read_only: bool = False,
    ) -> float:
        settings = self.get_settings(session_config)
        if not settings.enabled:
            return 1.0
        state = self.refresh(
            session_id, session_config=session_config, now=now, read_only=read_only
        )
        energy = float(state.get("energy", settings.baseline_energy) or 0.0)
        if energy >= settings.low_energy_threshold:
            return 1.0
        deficit = (
            settings.low_energy_threshold - energy
        ) / max(1e-6, settings.low_energy_threshold)
        return clamp(1.0 - settings.low_energy_probability_reduction * deficit)

    def snapshot(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
        read_only: bool = False,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        if not settings.enabled:
            return {"enabled": False, "probability_multiplier": 1.0}
        state = self.refresh(
            session_id, session_config=session_config, now=timestamp, read_only=read_only
        )
        energy = clamp(float(state.get("energy", settings.baseline_energy) or 0.0))
        mood = str(state.get("mood") or "calm")
        multiplier = 1.0
        if energy < settings.low_energy_threshold:
            deficit = (
                settings.low_energy_threshold - energy
            ) / max(1e-6, settings.low_energy_threshold)
            multiplier = clamp(
                1.0 - settings.low_energy_probability_reduction * deficit
            )
        return {
            "enabled": settings.enabled,
            "energy": round(energy, 3),
            "energy_percent": int(round(energy * 100)),
            "mood": mood,
            "mood_label": MOOD_LABELS.get(mood, mood),
            "activity": str(state.get("activity") or ""),
            "probability_multiplier": round(multiplier, 3),
            "updated_at": state.get("updated_at"),
        }


__all__ = ["PERSONA_STATE_KEY", "PersonaStateService"]
