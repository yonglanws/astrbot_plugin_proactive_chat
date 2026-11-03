"""作息节律服务：让主动消息落在对方通常活跃的时间。

职责
----
- 从真实用户消息学习 24 小时活跃分布；
- 在调度时把候选时间调整到活跃窗口内，并避开免打扰时段；
- 维护每日主动消息预算与最小发送间隔；
- 为管理端提供可解释的节律快照。

状态保存在 ``session_data[umo]["rhythm"]``，字段保持 JSON 友好。
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

from ..domain.humanization import RhythmSettings

RHYTHM_KEY = "rhythm"
STATE_VERSION = 1
HOUR_DECAY_PER_DAY = 0.9
DAILY_KEEP_DAYS = 14
MAX_HOUR_SCAN_DAYS = 14


class RhythmService:
    """私聊作息节律与发送预算服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 配置与状态
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
    ) -> RhythmSettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            humanization = session_config.get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("rhythm_settings")
        if not isinstance(raw, Mapping):
            humanization = self.get_private_settings().get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("rhythm_settings")
        return RhythmSettings.from_dict(raw)

    def default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "hour_counts": [0] * 24,
            "total_activity": 0,
            "daily_proactive": {},
            "last_user_ts": 0.0,
            "last_proactive_ts": 0.0,
            "last_update_ts": 0.0,
            "seed": {},
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
        state = payload.get(RHYTHM_KEY)
        if not isinstance(state, dict):
            if not create:
                return self.default_state()
            state = self.default_state()
            payload[RHYTHM_KEY] = state

        counts = state.get("hour_counts")
        if not isinstance(counts, list) or len(counts) != 24:
            state["hour_counts"] = [0] * 24
        else:
            state["hour_counts"] = [max(0, int(value or 0)) for value in counts]
        if not isinstance(state.get("daily_proactive"), dict):
            state["daily_proactive"] = {}
        if not isinstance(state.get("seed"), dict):
            state["seed"] = {}
        return state

    def _local_datetime(self, timestamp: float) -> datetime:
        try:
            return datetime.fromtimestamp(
                timestamp, tz=getattr(self.plugin, "timezone", None)
            )
        except (OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(timestamp)

    def _date_key(self, timestamp: float) -> str:
        return self._local_datetime(timestamp).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def record_user_activity(
        self, session_id: str, *, now: float | None = None
    ) -> bool:
        """记录一次用户活跃，用于学习作息分布。"""
        timestamp = float(now if now is not None else time.time())
        state = self.get_state(session_id, create=True)
        self._decay(state, timestamp)
        local = self._local_datetime(timestamp)
        state["hour_counts"][local.hour] = int(
            state["hour_counts"][local.hour] or 0
        ) + 1
        state["total_activity"] = int(state.get("total_activity", 0) or 0) + 1
        state["last_user_ts"] = timestamp
        state["last_update_ts"] = timestamp
        self._prune_daily(state, timestamp)
        return True

    def record_proactive_sent(
        self, session_id: str, *, now: float | None = None
    ) -> bool:
        """记录一次已发送的主动消息，推进每日预算。"""
        timestamp = float(now if now is not None else time.time())
        state = self.get_state(session_id, create=True)
        self._decay(state, timestamp)
        key = self._date_key(timestamp)
        daily = state["daily_proactive"]
        daily[key] = int(daily.get(key, 0) or 0) + 1
        state["last_proactive_ts"] = timestamp
        state["last_update_ts"] = timestamp
        self._prune_daily(state, timestamp)
        return True

    def _decay(self, state: dict[str, Any], now: float) -> None:
        last_update = float(state.get("last_update_ts", 0) or 0)
        if last_update <= 0:
            state["last_update_ts"] = now
            return
        days = max(0.0, (now - last_update) / 86400.0)
        if days < 0.5:
            return
        factor = HOUR_DECAY_PER_DAY ** min(days, 60.0)
        state["hour_counts"] = [
            max(0.0, float(value or 0) * factor)
            for value in state.get("hour_counts", [])
        ]
        state["last_update_ts"] = now

    def _prune_daily(self, state: dict[str, Any], now: float) -> None:
        keep = {
            self._date_key(now - offset * 86400)
            for offset in range(DAILY_KEEP_DAYS)
        }
        daily = state.get("daily_proactive", {})
        for key in list(daily.keys()):
            if key not in keep:
                del daily[key]

    # ------------------------------------------------------------------
    # 作息学习
    # ------------------------------------------------------------------
    def _seed_from_intensity(self, session_id: str, state: dict[str, Any]) -> None:
        """首次启用时用互动强度事件里的时间戳补一份作息分布。"""
        if int(state.get("total_activity", 0) or 0) >= 3:
            return
        if state.get("seed"):
            return
        intensity_service = getattr(self.plugin, "intensity_service", None)
        if intensity_service is None:
            return
        try:
            intensity_state = intensity_service.get_state(session_id)
            events = intensity_state.get("events", [])
        except Exception:
            return
        timestamps = [
            float(item.get("t", 0) or 0)
            for item in events
            if isinstance(item, dict) and float(item.get("t", 0) or 0) > 0
        ]
        if not timestamps:
            return
        counts = [0.0] * 24
        for timestamp in timestamps[-200:]:
            counts[self._local_datetime(timestamp).hour] += 1
        state["hour_counts"] = counts
        state["total_activity"] = len(timestamps[-200:])
        state["seed"] = {
            "source": "intensity_events",
            "seeded_at": time.time(),
        }

    def learned_active_hours(
        self,
        session_id: str,
        *,
        now: float | None = None,
        settings: RhythmSettings | None = None,
    ) -> list[int]:
        """返回学习到的活跃小时列表；样本不足时返回空列表。"""
        settings = settings or self.get_settings()
        if not settings.enabled or not settings.learn_active_hours:
            return []
        state = self.get_state(session_id)
        self._seed_from_intensity(session_id, state)
        total = float(sum(state.get("hour_counts", [])))
        if total < settings.min_messages_to_learn:
            return []

        counts = [float(value or 0) for value in state.get("hour_counts", [])]
        average = total / 24.0
        threshold = max(1.0, average * 0.55)
        hours = [hour for hour, value in enumerate(counts) if value >= threshold]
        if not hours:
            ranked = sorted(range(24), key=lambda h: counts[h], reverse=True)
            hours = ranked[:8]
        return sorted(hours)

    def _allowed_minutes(
        self,
        session_id: str,
        *,
        settings: RhythmSettings,
        quiet_hours: str = "",
        weekend: bool = False,
    ) -> set[int]:
        hours = self.learned_active_hours(session_id, settings=settings)
        if not hours:
            return set()
        margin = settings.active_hours_margin_minutes
        if weekend:
            margin += settings.weekend_margin_minutes
        allowed: set[int] = set()
        for hour in hours:
            start = hour * 60 - margin
            end = hour * 60 + 60 + margin
            for minute in range(start, end + 1):
                allowed.add(minute % 1440)

        if quiet_hours:
            quiet = self._parse_quiet_minutes(quiet_hours)
            allowed = {minute for minute in allowed if minute not in quiet}
        return allowed

    @staticmethod
    def _parse_quiet_minutes(quiet_hours: str) -> set[int]:
        try:
            start_str, end_str = str(quiet_hours).split("-")
            start_hour, end_hour = int(start_str), int(end_str)
        except (ValueError, TypeError):
            return set()
        start = (start_hour % 24) * 60
        end = (end_hour % 24) * 60
        if start == end:
            return set()
        if start < end:
            return set(range(start, end))
        return set(range(start, 1440)) | set(range(0, end))

    def _next_allowed_timestamp(
        self,
        candidate: float,
        allowed_minutes: set[int],
        *,
        max_days: int = MAX_HOUR_SCAN_DAYS,
    ) -> float | None:
        if not allowed_minutes:
            return None
        local = self._local_datetime(candidate)
        current_minute = local.hour * 60 + local.minute
        for day_offset in range(max_days):
            base = (local + timedelta(days=day_offset)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for minute in sorted(allowed_minutes):
                if day_offset == 0 and minute < current_minute:
                    continue
                if day_offset == 0 and minute == current_minute:
                    return candidate
                target = base + timedelta(minutes=minute)
                if target.timestamp() >= candidate - 1:
                    return target.timestamp()
        return None

    # ------------------------------------------------------------------
    # 调度修正
    # ------------------------------------------------------------------
    def adjust_trigger(
        self,
        session_id: str,
        candidate_ts: float,
        *,
        now: float | None = None,
        schedule_conf: Mapping[str, Any] | None = None,
        session_config: Mapping[str, Any] | None = None,
        rng: random.Random | None = None,
    ) -> tuple[float, dict[str, Any]]:
        """把候选触发时间调整到作息窗口、每日预算与最小间隔内。"""
        timestamp = float(candidate_ts)
        current = float(now if now is not None else time.time())
        timestamp = max(timestamp, current + 60.0)
        settings = self.get_settings(session_config)
        schedule_conf = schedule_conf if isinstance(schedule_conf, Mapping) else {}
        meta: dict[str, Any] = {
            "adjusted": False,
            "reason": "",
            "active_hours": self.learned_active_hours(
                session_id, now=current, settings=settings
            ),
            "daily_count": 0,
            "daily_limit": settings.daily_proactive_limit if settings.enabled else 0,
        }
        if not settings.enabled:
            return timestamp, meta

        state = self.get_state(session_id)
        generator = rng or random

        # 1) 最小间隔：避免连续主动消息太密。
        last_proactive = float(state.get("last_proactive_ts", 0) or 0)
        min_gap = max(0, int(settings.min_gap_minutes)) * 60
        if last_proactive > 0 and min_gap > 0:
            earliest = last_proactive + min_gap
            if timestamp < earliest:
                timestamp = earliest
                meta["adjusted"] = True
                meta["reason"] = "min_gap"

        # 2) 每日预算：用完则顺延到下一个活跃窗口/次日。
        date_key = self._date_key(timestamp)
        daily_count = int(state.get("daily_proactive", {}).get(date_key, 0) or 0)
        meta["daily_count"] = daily_count
        if (
            settings.daily_proactive_limit > 0
            and daily_count >= settings.daily_proactive_limit
        ):
            next_day = self._local_datetime(timestamp) + timedelta(days=1)
            timestamp = (
                next_day.replace(hour=0, minute=0, second=0, microsecond=0)
            ).timestamp() + generator.uniform(0, 30) * 60
            meta["adjusted"] = True
            meta["reason"] = "daily_budget"
            meta["daily_count"] = 0

        # 3) 活跃窗口：优先落在学习到的作息内。
        quiet_hours = str(schedule_conf.get("quiet_hours") or "")
        allowed = self._allowed_minutes(
            session_id,
            settings=settings,
            quiet_hours=quiet_hours,
            weekend=self._local_datetime(timestamp).weekday() >= 5,
        )
        if allowed:
            adjusted = self._next_allowed_timestamp(timestamp, allowed)
            if adjusted is not None and adjusted > timestamp + 1:
                timestamp = adjusted + generator.uniform(0, 20) * 60
                meta["adjusted"] = True
                meta["reason"] = meta["reason"] or "active_hours"
        elif quiet_hours:
            # 没有足够样本学习作息时，至少避开免打扰时段。
            quiet = self._parse_quiet_minutes(quiet_hours)
            local = self._local_datetime(timestamp)
            if (local.hour * 60 + local.minute) in quiet:
                allowed_fallback = set(range(1440)) - quiet
                adjusted = self._next_allowed_timestamp(
                    timestamp, allowed_fallback
                )
                if adjusted is not None:
                    timestamp = adjusted
                    meta["adjusted"] = True
                    meta["reason"] = "quiet_hours"

        return timestamp, meta

    def can_send_now(
        self,
        session_id: str,
        *,
        now: float | None = None,
        manual: bool = False,
        schedule_conf: Mapping[str, Any] | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str, float]:
        """检查当前是否适合发送；返回 (allowed, reason, next_delay_seconds)。"""
        if manual:
            return True, "manual", 0.0
        current = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        if not settings.enabled:
            return True, "disabled", 0.0

        state = self.get_state(session_id)
        last_proactive = float(state.get("last_proactive_ts", 0) or 0)
        min_gap = max(0, int(settings.min_gap_minutes)) * 60
        if last_proactive > 0 and min_gap > 0:
            earliest = last_proactive + min_gap
            if current < earliest:
                return False, "min_gap", max(60.0, earliest - current)

        date_key = self._date_key(current)
        daily_count = int(state.get("daily_proactive", {}).get(date_key, 0) or 0)
        if (
            settings.daily_proactive_limit > 0
            and daily_count >= settings.daily_proactive_limit
        ):
            next_day = (self._local_datetime(current) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return False, "daily_budget", max(60.0, next_day.timestamp() - current)

        schedule_conf = schedule_conf if isinstance(schedule_conf, Mapping) else {}
        quiet_hours = str(schedule_conf.get("quiet_hours") or "")
        allowed = self._allowed_minutes(
            session_id,
            settings=settings,
            quiet_hours=quiet_hours,
            weekend=self._local_datetime(current).weekday() >= 5,
        )
        if allowed:
            local = self._local_datetime(current)
            if (local.hour * 60 + local.minute) not in allowed:
                adjusted = self._next_allowed_timestamp(current, allowed)
                delay = (
                    max(60.0, adjusted - current)
                    if adjusted is not None
                    else 3600.0
                )
                return False, "active_hours", delay
        elif quiet_hours:
            quiet = self._parse_quiet_minutes(quiet_hours)
            local = self._local_datetime(current)
            if (local.hour * 60 + local.minute) in quiet:
                adjusted = self._next_allowed_timestamp(
                    current, set(range(1440)) - quiet
                )
                delay = (
                    max(60.0, adjusted - current)
                    if adjusted is not None
                    else 3600.0
                )
                return False, "quiet_hours", delay

        return True, "allowed", 0.0

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def circadian_probability_multiplier(
        self,
        session_id: str,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> float:
        """按当前小时与活跃时段的距离，给出一个温和的概率系数。"""
        settings = self.get_settings(session_config)
        if not settings.enabled or settings.circadian_strength <= 0:
            return 1.0
        timestamp = float(now if now is not None else time.time())
        active = self.learned_active_hours(
            session_id, now=timestamp, settings=settings
        )
        if not active:
            return 1.0
        hour = self._local_datetime(timestamp).hour
        distance = active_hours_distance(active, hour)
        raw = max(0.35, 1.0 - distance * 0.12)
        return max(
            0.2,
            1.0 - settings.circadian_strength * (1.0 - raw),
        )

    def snapshot(
        self,
        session_id: str,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        state = self.get_state(session_id)
        self._seed_from_intensity(session_id, state)
        active_hours = self.learned_active_hours(
            session_id, now=timestamp, settings=settings
        )
        date_key = self._date_key(timestamp)
        daily_count = int(state.get("daily_proactive", {}).get(date_key, 0) or 0)
        allowed, reason, delay = self.can_send_now(
            session_id,
            now=timestamp,
            session_config=session_config,
        )
        return {
            "enabled": settings.enabled,
            "learned": bool(active_hours),
            "active_hours": active_hours,
            "active_hours_label": self._format_hours(active_hours),
            "total_activity": int(state.get("total_activity", 0) or 0),
            "daily_count": daily_count,
            "daily_limit": settings.daily_proactive_limit,
            "min_gap_minutes": settings.min_gap_minutes,
            "last_proactive_at": state.get("last_proactive_ts") or None,
            "last_user_at": state.get("last_user_ts") or None,
            "can_send_now": allowed,
            "block_reason": reason,
            "next_delay_seconds": int(max(0, delay)),
            "seed_source": str((state.get("seed") or {}).get("source") or ""),
        }

    @staticmethod
    def _format_hours(hours: list[int]) -> str:
        if not hours:
            return "样本不足"
        # 压缩连续小时为 9-12、20-23 这样的区间。
        ranges: list[str] = []
        start = prev = hours[0]
        for hour in hours[1:]:
            if hour == prev + 1:
                prev = hour
                continue
            ranges.append(f"{start}-{prev + 1}" if start != prev else f"{start}")
            start = prev = hour
        ranges.append(f"{start}-{prev + 1}" if start != prev else f"{start}")
        return "、".join(ranges)

    def seed_from_timestamps(
        self, session_id: str, timestamps: list[float]
    ) -> bool:
        """用一组历史时间戳初始化作息分布（供导入工具/测试使用）。"""
        valid = [float(value) for value in timestamps if float(value or 0) > 0]
        if not valid:
            return False
        state = self.get_state(session_id, create=True)
        counts = [0.0] * 24
        for timestamp in valid[-500:]:
            counts[self._local_datetime(timestamp).hour] += 1
        state["hour_counts"] = counts
        state["total_activity"] = len(valid[-500:])
        state["seed"] = {
            "source": "timestamps",
            "seeded_at": time.time(),
        }
        return True


def active_hours_distance(hours: list[int], hour: int) -> int:
    """返回目标小时到最近活跃小时的环形距离（供测试与解释使用）。"""
    if not hours:
        return 24
    return min(min(abs(hour - item), 24 - abs(hour - item)) for item in hours)


def circadian_multiplier(hour: int, active_hours: list[int]) -> float:
    """按活跃小时给触发概率一个温和的昼夜系数。"""
    if not active_hours:
        return 1.0
    distance = active_hours_distance(active_hours, hour)
    return max(0.35, 1.0 - distance * 0.12)


__all__ = [
    "RHYTHM_KEY",
    "RhythmService",
    "active_hours_distance",
    "circadian_multiplier",
]
