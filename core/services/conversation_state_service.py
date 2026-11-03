"""会话关系状态、回复反馈与主动意图选择。

状态保存在 ``session_data[umo]["conversation_state"]``：
- 当前关系状态（空闲 / 话题未结束 / 等待回应 / 降温中 / 暂停）；
- 最近一次用户回复的质量；
- 最近一次主动消息使用的意图；
- 按小时桶与意图统计的反馈结果，用于轻量策略学习。
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any, Mapping

from ..domain.humanization import (
    CONVERSATION_STATE_LABELS,
    INTENT_BRIEFS,
    INTENT_LABELS,
    ConversationState,
    FeedbackSettings,
    Intent,
    PersonaSettings,
    ReplyQuality,
    StateSettings,
)
from .scheduler_service import PAUSE_FLAG

CONVERSATION_STATE_KEY = "conversation_state"
STATE_VERSION = 1
FEEDBACK_KEEP_DAYS = 60
HOUR_BUCKETS = (
    (0, 6, "night"),
    (6, 9, "morning"),
    (9, 12, "forenoon"),
    (12, 14, "noon"),
    (14, 18, "afternoon"),
    (18, 22, "evening"),
    (22, 24, "late_night"),
)


def hour_bucket(hour: int) -> str:
    hour = int(hour) % 24
    for start, end, label in HOUR_BUCKETS:
        if start <= hour < end:
            return label
    return "unknown"


class ConversationStateService:
    """关系状态机与意图选择服务。"""

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
    ) -> StateSettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            humanization = session_config.get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("state_settings")
        if not isinstance(raw, Mapping):
            humanization = self.get_private_settings().get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("state_settings")
        return StateSettings.from_dict(raw)

    def get_feedback_settings(
        self, session_config: Mapping[str, Any] | None = None
    ) -> FeedbackSettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            humanization = session_config.get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("feedback_settings")
        if not isinstance(raw, Mapping):
            humanization = self.get_private_settings().get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("feedback_settings")
        return FeedbackSettings.from_dict(raw)

    def get_persona_settings(
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
    # 状态读写
    # ------------------------------------------------------------------
    def default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "state": ConversationState.IDLE.value,
            "since": 0.0,
            "reason": "",
            "last_user_at": 0.0,
            "last_user_quality": "",
            "last_proactive_at": 0.0,
            "last_intent": "",
            "silent_streak": 0,
            "feedback_stats": {},
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
        state = payload.get(CONVERSATION_STATE_KEY)
        if not isinstance(state, dict):
            if not create:
                return self.default_state()
            state = self.default_state()
            payload[CONVERSATION_STATE_KEY] = state
        if not isinstance(state.get("feedback_stats"), dict):
            state["feedback_stats"] = {}
        return state

    def _session_payload(self, session_id: str) -> dict[str, Any]:
        session_data = getattr(self.plugin, "session_data", {})
        payload = session_data.get(session_id) if isinstance(session_data, dict) else None
        return payload if isinstance(payload, dict) else {}

    def _transition(
        self,
        state: dict[str, Any],
        new_state: ConversationState,
        *,
        now: float,
        reason: str,
    ) -> bool:
        current = str(state.get("state") or "")
        if current == new_state.value:
            state["reason"] = reason or state.get("reason", "")
            return False
        state["state"] = new_state.value
        state["since"] = now
        state["reason"] = reason
        return True

    def _local_hour(self, timestamp: float) -> int:
        try:
            return datetime.fromtimestamp(
                timestamp, tz=getattr(self.plugin, "timezone", None)
            ).hour
        except (OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(timestamp).hour

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def refresh(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """根据时间与未回复计数重新计算关系状态。"""
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        state = self.get_state(session_id, create=True)
        payload = self._session_payload(session_id)

        if payload.get(PAUSE_FLAG):
            self._transition(
                state, ConversationState.PAUSED, now=timestamp, reason="paused"
            )
            return state

        last_user_at = float(state.get("last_user_at", 0) or 0)
        last_proactive_at = float(state.get("last_proactive_at", 0) or 0)
        quality = str(state.get("last_user_quality") or "")
        unanswered = int(payload.get("unanswered_count", 0) or 0)
        awaiting_window = max(1.0, settings.awaiting_reply_hours) * 3600
        open_window = max(1.0, settings.open_thread_hours) * 3600
        cooling_window = max(1.0, settings.cooling_hours) * 3600

        if last_proactive_at > last_user_at and unanswered > 0:
            if timestamp - last_proactive_at <= awaiting_window:
                self._transition(
                    state,
                    ConversationState.AWAITING_REPLY,
                    now=timestamp,
                    reason="waiting_for_reply",
                )
            else:
                self._transition(
                    state,
                    ConversationState.COOLING,
                    now=timestamp,
                    reason="reply_window_passed",
                )
            return state

        if last_user_at > 0:
            age = timestamp - last_user_at
            if quality in {
                ReplyQuality.PERFUNCTORY.value,
                ReplyQuality.NEGATIVE.value,
            } and age <= cooling_window:
                self._transition(
                    state,
                    ConversationState.COOLING,
                    now=timestamp,
                    reason=f"user_{quality}",
                )
                return state
            if age <= open_window:
                self._transition(
                    state,
                    ConversationState.OPEN_THREAD,
                    now=timestamp,
                    reason="recent_message",
                )
                return state
            if age <= cooling_window:
                self._transition(
                    state,
                    ConversationState.COOLING,
                    now=timestamp,
                    reason="conversation_cooling",
                )
                return state

        self._transition(
            state, ConversationState.IDLE, now=timestamp, reason="no_recent_activity"
        )
        return state

    # ------------------------------------------------------------------
    # 记录用户回复
    # ------------------------------------------------------------------
    def record_user_message(
        self,
        session_id: str,
        quality: ReplyQuality,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录用户回复质量，并结算上一条主动消息的策略反馈。"""
        timestamp = float(now if now is not None else time.time())
        state = self.get_state(session_id, create=True)
        payload = self._session_payload(session_id)

        self._record_feedback(state, quality, timestamp)
        state["last_user_at"] = timestamp
        state["last_user_quality"] = quality.value
        state["silent_streak"] = 0

        if quality is ReplyQuality.NEGATIVE:
            new_state = ConversationState.COOLING
            reason = "negative_feedback"
        elif quality is ReplyQuality.PERFUNCTORY:
            new_state = ConversationState.COOLING
            reason = "perfunctory_reply"
        else:
            new_state = ConversationState.OPEN_THREAD
            reason = "user_reengaged"
        self._transition(state, new_state, now=timestamp, reason=reason)

        return {
            "quality": quality.value,
            "quality_label": {
                ReplyQuality.SUBSTANTIVE: "实质回复",
                ReplyQuality.QUESTION: "提问/接话",
                ReplyQuality.PERFUNCTORY: "敷衍回应",
                ReplyQuality.NEGATIVE: "负反馈",
                ReplyQuality.MEDIA: "媒体消息",
            }.get(quality, quality.value),
            "state": state.get("state"),
            "state_label": CONVERSATION_STATE_LABELS.get(
                ConversationState(state.get("state")), "未知"
            )
            if state.get("state") in {item.value for item in ConversationState}
            else "未知",
            "unanswered_count": int(payload.get("unanswered_count", 0) or 0),
        }

    def _record_feedback(
        self,
        state: dict[str, Any],
        quality: ReplyQuality,
        now: float,
    ) -> None:
        last_proactive_at = float(state.get("last_proactive_at", 0) or 0)
        intent = str(state.get("last_intent") or "")
        # 只结算最近 7 天内的主动消息，避免很久之后的回复污染策略统计。
        if last_proactive_at <= 0 or now - last_proactive_at > 7 * 86400:
            return
        if not intent:
            return
        if quality is ReplyQuality.NEGATIVE:
            outcome = "negative"
        elif quality is ReplyQuality.PERFUNCTORY:
            outcome = "neutral"
        else:
            outcome = "positive"

        bucket = hour_bucket(self._local_hour(last_proactive_at))
        stats = state.setdefault("feedback_stats", {})
        bucket_stats = stats.setdefault(bucket, {})
        intent_stats = bucket_stats.setdefault(
            intent, {"positive": 0, "neutral": 0, "negative": 0}
        )
        intent_stats[outcome] = int(intent_stats.get(outcome, 0) or 0) + 1

        # 限制历史规模：每个桶最多保留 12 个意图。
        if len(bucket_stats) > 12:
            for key in list(bucket_stats.keys())[:-12]:
                bucket_stats.pop(key, None)

    # ------------------------------------------------------------------
    # 记录主动消息
    # ------------------------------------------------------------------
    def record_proactive_sent(
        self,
        session_id: str,
        intent: Intent | str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        state = self.get_state(session_id, create=True)
        intent_value = intent.value if isinstance(intent, Intent) else str(intent)
        state["last_proactive_at"] = timestamp
        state["last_intent"] = intent_value
        state["silent_streak"] = 0
        self._transition(
            state,
            ConversationState.AWAITING_REPLY,
            now=timestamp,
            reason="proactive_sent",
        )
        return {
            "intent": intent_value,
            "intent_label": INTENT_LABELS.get(Intent(intent_value), intent_value)
            if intent_value in {item.value for item in Intent}
            else intent_value,
            "state": state.get("state"),
        }

    def record_silent_intent(
        self, session_id: str, *, now: float | None = None
    ) -> int:
        timestamp = float(now if now is not None else time.time())
        state = self.get_state(session_id, create=True)
        state["silent_streak"] = int(state.get("silent_streak", 0) or 0) + 1
        state["last_decision_at"] = timestamp
        state["last_intent"] = Intent.SILENT.value
        return state["silent_streak"]

    # ------------------------------------------------------------------
    # 意图选择
    # ------------------------------------------------------------------
    def choose_intent(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        persona_energy: float = 0.6,
        open_loop_count: int = 0,
        now: float | None = None,
        rng: random.Random | None = None,
        manual: bool = False,
    ) -> dict[str, Any]:
        """按关系状态、精力与历史反馈选择本轮意图。"""
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        state = self.refresh(
            session_id, session_config=session_config, now=timestamp
        )
        state_value = str(state.get("state") or ConversationState.IDLE.value)
        payload = self._session_payload(session_id)
        unanswered = int(payload.get("unanswered_count", 0) or 0)

        weights: dict[Intent, float] = {
            Intent.CONTINUE_TOPIC: 1.0,
            Intent.NEW_TOPIC: 1.0,
            Intent.CARE: 1.0,
            Intent.SHARE_SELF: 1.0,
            Intent.ASK: 1.0,
        }

        if state_value == ConversationState.OPEN_THREAD.value:
            weights.update(
                {
                    Intent.CONTINUE_TOPIC: 4.0,
                    Intent.CARE: 1.5,
                    Intent.SHARE_SELF: 1.2,
                    Intent.ASK: 1.0,
                    Intent.NEW_TOPIC: 0.5,
                }
            )
        elif state_value == ConversationState.AWAITING_REPLY.value:
            weights.update(
                {
                    Intent.CARE: 1.8,
                    Intent.CONTINUE_TOPIC: 1.0,
                    Intent.SHARE_SELF: 0.6,
                    Intent.ASK: 0.4,
                    Intent.NEW_TOPIC: 0.6,
                }
            )
        elif state_value == ConversationState.COOLING.value:
            weights.update(
                {
                    Intent.CARE: 2.2,
                    Intent.NEW_TOPIC: 0.8,
                    Intent.CONTINUE_TOPIC: 0.6,
                    Intent.SHARE_SELF: 0.4,
                    Intent.ASK: 0.3,
                }
            )
        else:  # idle
            weights.update(
                {
                    Intent.NEW_TOPIC: 3.0,
                    Intent.CARE: 1.5,
                    Intent.SHARE_SELF: 1.5,
                    Intent.CONTINUE_TOPIC: 0.6,
                    Intent.ASK: 0.6,
                }
            )

        # 精力影响：低精力少分享、少提问；高精力更愿意主动分享。
        if persona_energy < 0.35:
            weights[Intent.SHARE_SELF] *= 0.35
            weights[Intent.ASK] *= 0.6
            weights[Intent.CARE] *= 0.8
        elif persona_energy > 0.75:
            weights[Intent.SHARE_SELF] *= 1.5
            weights[Intent.ASK] *= 1.1

        # 有未闭环话题时，优先自然跟进，而不是重新开一个无关话题。
        if open_loop_count > 0:
            followup_weight = 1.5
            memory_service = getattr(
                self.plugin, "conversation_memory_service", None
            )
            if memory_service is not None:
                try:
                    followup_weight = float(
                        memory_service.get_settings(session_config).followup_weight
                        or 1.5
                    )
                except Exception:
                    followup_weight = 1.5
            boost = min(
                3.0, followup_weight + max(0, open_loop_count - 1) * 0.15
            )
            weights[Intent.CONTINUE_TOPIC] *= boost
            weights[Intent.CARE] *= boost

        # 历史反馈：用 Beta 平滑后的正向率给意图加权，避免小样本极端值。
        for intent, weight in list(weights.items()):
            success = self._intent_success_rate(
                state, intent, self._local_hour(timestamp)
            )
            weights[intent] = max(0.05, weight * (0.5 + success))

        silent_allowed = (
            settings.silent_intent_enabled
            and not manual
            and int(state.get("silent_streak", 0) or 0) < settings.silent_max_streak
        )
        if silent_allowed:
            silent_weight = 0.5
            if state_value == ConversationState.AWAITING_REPLY.value:
                silent_weight += 0.8 + unanswered * 0.9
            elif state_value == ConversationState.COOLING.value:
                silent_weight += 1.2
            if persona_energy < 0.35:
                silent_weight += 0.5
            weights[Intent.SILENT] = silent_weight

        intents = list(weights.keys())
        values = [max(0.01, float(weights[intent])) for intent in intents]
        generator = rng or random
        chosen = generator.choices(intents, weights=values, k=1)[0]

        reason = f"state={state_value}, energy={persona_energy:.2f}"
        if chosen is Intent.SILENT and not settings.enabled:
            chosen = Intent.CONTINUE_TOPIC
        if chosen is Intent.SILENT:
            reason += ", silent_candidate"
            self.record_silent_intent(session_id, now=timestamp)
        else:
            state["last_decision_at"] = timestamp

        return {
            "intent": chosen.value,
            "intent_label": INTENT_LABELS.get(chosen, chosen.value),
            "brief": INTENT_BRIEFS.get(chosen, ""),
            "state": state_value,
            "state_label": CONVERSATION_STATE_LABELS.get(
                ConversationState(state_value), state_value
            )
            if state_value in {item.value for item in ConversationState}
            else state_value,
            "reason": reason,
            "silent": chosen is Intent.SILENT,
            "unanswered_count": unanswered,
            "weights": {intent.value: round(weight, 3) for intent, weight in weights.items()},
        }

    def _intent_success_rate(
        self, state: dict[str, Any], intent: Intent, hour: int
    ) -> float:
        """返回平滑后的正向率；同时参考当前桶与全部桶。"""
        stats = state.get("feedback_stats", {})
        if not isinstance(stats, dict):
            return 0.5

        def _rate_for(container: Mapping[str, Any]) -> tuple[float, float]:
            item = container.get(intent.value)
            if not isinstance(item, Mapping):
                return 1.0, 2.0
            positive = float(item.get("positive", 0) or 0)
            neutral = float(item.get("neutral", 0) or 0)
            negative = float(item.get("negative", 0) or 0)
            return 1.0 + positive + neutral * 0.5, 2.0 + positive + neutral + negative

        bucket = stats.get(hour_bucket(hour), {})
        pos_b, total_b = _rate_for(bucket if isinstance(bucket, Mapping) else {})
        pos_all = 1.0
        total_all = 2.0
        for container in stats.values():
            if not isinstance(container, Mapping):
                continue
            pos, total = _rate_for(container)
            pos_all += pos - 1.0
            total_all += total - 2.0
        return (pos_b / total_b) * 0.6 + (pos_all / total_all) * 0.4

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        state = self.refresh(
            session_id, session_config=session_config, now=timestamp
        )
        state_value = str(state.get("state") or ConversationState.IDLE.value)
        return {
            "state": state_value,
            "state_label": CONVERSATION_STATE_LABELS.get(
                ConversationState(state_value), state_value
            )
            if state_value in {item.value for item in ConversationState}
            else state_value,
            "since": state.get("since"),
            "reason": state.get("reason"),
            "last_user_at": state.get("last_user_at") or None,
            "last_user_quality": state.get("last_user_quality") or "",
            "last_proactive_at": state.get("last_proactive_at") or None,
            "last_intent": state.get("last_intent") or "",
            "last_intent_label": INTENT_LABELS.get(
                Intent(state.get("last_intent")), state.get("last_intent")
            )
            if state.get("last_intent") in {item.value for item in Intent}
            else (state.get("last_intent") or ""),
            "silent_streak": int(state.get("silent_streak", 0) or 0),
            "feedback": self.feedback_summary(session_id, now=timestamp),
        }

    def feedback_summary(
        self, session_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        state = self.get_state(session_id)
        stats = state.get("feedback_stats", {})
        totals = {"positive": 0, "neutral": 0, "negative": 0}
        by_intent: dict[str, dict[str, int]] = {}
        if isinstance(stats, dict):
            for bucket in stats.values():
                if not isinstance(bucket, Mapping):
                    continue
                for intent, values in bucket.items():
                    if not isinstance(values, Mapping):
                        continue
                    target = by_intent.setdefault(
                        str(intent), {"positive": 0, "neutral": 0, "negative": 0}
                    )
                    for key in totals:
                        count = int(values.get(key, 0) or 0)
                        totals[key] += count
                        target[key] += count
        total = sum(totals.values())
        positive_rate = (
            (totals["positive"] + totals["neutral"] * 0.5) / total
            if total
            else None
        )
        return {
            "totals": totals,
            "total": total,
            "positive_rate": (
                None if positive_rate is None else round(positive_rate, 3)
            ),
            "by_intent": by_intent,
            "last_user_quality": str(state.get("last_user_quality") or ""),
            "hour_bucket": hour_bucket(self._local_hour(timestamp)),
        }


__all__ = [
    "CONVERSATION_STATE_KEY",
    "ConversationStateService",
    "hour_bucket",
]
