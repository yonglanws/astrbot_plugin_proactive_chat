"""私聊互动强度服务。

职责
----
- 在每个私聊会话的持久化数据里维护一份轻量的「互动强度状态」；
- 从真实消息时间线推导频率、节奏、活跃天数、回复率、表达欲等信号；
- 为调度器提供「触发概率」与「修正后的间隔」；
- 为管理端提供可直接渲染的快照。

存储位置
--------
``plugin.session_data[umo]["intensity"]``。结构保持 JSON 友好，并做上限裁剪：

- ``events``：最近 200 条用户消息（时间、长度、疑问/感叹/表情标记）；
- ``daily``：最近 45 天的每日消息数；
- ``proactive``：最近 30 条主动消息（发送时间、是否被回复、回复时间）；
- ``last_decision``：最近一次概率判定的可解释记录。
"""

from __future__ import annotations

import copy
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

from astrbot.api import logger

from ..domain.intensity import (
    IntensityResult,
    IntensitySettings,
    IntensitySignals,
    compute_score,
    effective_interval_range,
    median_gap_minutes,
    sample_interval_seconds,
    skip_retry_seconds,
)
from .scheduler_service import (
    PAUSE_AT,
    PAUSE_FLAG,
    PAUSE_REASON,
    PAUSE_THRESHOLD,
    PAUSE_UNANSWERED,
)

INTENSITY_KEY = "intensity"
STATE_VERSION = 1

MAX_EVENTS = 200
MAX_PROACTIVE = 30
DAILY_KEEP_DAYS = 45
PROACTIVE_KEEP_DAYS = 30
EVENT_SCAN_DAYS = 7.0
TEMPO_MAX_GAP_SECONDS = 6 * 3600

_FLAG_QUESTION = "q"
_FLAG_EXCLAIM = "e"
_FLAG_EMOJI = "m"

# 覆盖常见 emoji 区段与 QQ/微信表情占位符；不需要识别具体是哪一个表情。
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]"
)
_STICKER_TOKENS = ("[表情", "<sticker", "[动画表情", "[图片表情")
_QUESTION_TOKENS = ("?", "？", "吗", "呢", "怎么", "为什么", "什么", "哪儿", "哪")
_EXCLAIM_TOKENS = ("!", "！", "啊", "呀", "哇", "欸", "诶")


class IntensityService:
    """互动强度状态与决策服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------
    def default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "events": [],
            "daily": {},
            "proactive": [],
            "total_user_messages": 0,
            "first_seen": 0.0,
            "last_user_ts": 0.0,
            "last_decision": {},
            "seed": {},
        }

    def get_state(self, session_id: str, *, create: bool = False) -> dict[str, Any]:
        """读取会话强度状态；``create=True`` 时初始化缺失字段。"""
        session_data = getattr(self.plugin, "session_data", None)
        if not isinstance(session_data, dict):
            return self.default_state()

        payload = session_data.get(session_id)
        if not isinstance(payload, dict):
            if not create:
                return self.default_state()
            payload = {}
            session_data[session_id] = payload

        state = payload.get(INTENSITY_KEY)
        if not isinstance(state, dict):
            if not create:
                return self.default_state()
            state = self.default_state()
            payload[INTENSITY_KEY] = state

        # 容忍旧版本或被手工编辑过的数据。
        if not isinstance(state.get("events"), list):
            state["events"] = []
        if not isinstance(state.get("daily"), dict):
            state["daily"] = {}
        if not isinstance(state.get("proactive"), list):
            state["proactive"] = []
        if not isinstance(state.get("last_decision"), dict):
            state["last_decision"] = {}
        if not isinstance(state.get("seed"), dict):
            state["seed"] = {}
        return state

    def clear_state(self, session_id: str) -> bool:
        payload = getattr(self.plugin, "session_data", {}).get(session_id)
        if isinstance(payload, dict) and INTENSITY_KEY in payload:
            del payload[INTENSITY_KEY]
            return True
        return False

    # ------------------------------------------------------------------
    # 时间与文本工具
    # ------------------------------------------------------------------
    def _date_key(self, timestamp: float) -> str:
        try:
            return datetime.fromtimestamp(
                timestamp, tz=getattr(self.plugin, "timezone", None)
            ).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")

    def _recent_date_keys(self, now: float, days: int) -> list[str]:
        keys: list[str] = []
        tz = getattr(self.plugin, "timezone", None)
        try:
            current = datetime.fromtimestamp(now, tz=tz)
        except (OverflowError, OSError, ValueError):
            current = datetime.now()
        for offset in range(max(1, days)):
            keys.append((current - timedelta(days=offset)).strftime("%Y-%m-%d"))
        return keys

    @staticmethod
    def analyze_text(text: str) -> dict[str, Any]:
        """提取一条用户消息的可用于评分的特征。"""
        normalized = str(text or "").strip()
        has_emoji = bool(_EMOJI_RE.search(normalized)) or any(
            token in normalized for token in _STICKER_TOKENS
        )
        return {
            "n": len(normalized),
            "q": 1 if any(token in normalized for token in _QUESTION_TOKENS) else 0,
            "e": 1 if any(token in normalized for token in _EXCLAIM_TOKENS) else 0,
            "m": 1 if has_emoji else 0,
        }

    # ------------------------------------------------------------------
    # 记录事件
    # ------------------------------------------------------------------
    def record_user_message(
        self,
        session_id: str,
        text: str,
        *,
        now: float | None = None,
        settings: IntensitySettings | None = None,
        counts_as_reply: bool = True,
    ) -> bool:
        """记录一条用户私聊消息，并结算此前的主动消息回复。

        ``counts_as_reply=False`` 用于敷衍/负反馈回复：消息本身仍计入
        频率与表达特征，但不会把上一条主动消息标记为“已回复”，避免
        用“嗯/哦”抬高主动回复率。
        """
        timestamp = float(now if now is not None else time.time())
        settings = settings or self.get_settings()
        state = self.get_state(session_id, create=True)

        feature = self.analyze_text(text)
        event = {
            "t": round(timestamp, 3),
            "n": int(feature["n"]),
            "q": int(feature["q"]),
            "e": int(feature["e"]),
            "m": int(feature["m"]),
        }

        events = state["events"]
        events.append(event)
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]

        date_key = self._date_key(timestamp)
        daily = state["daily"]
        daily[date_key] = int(daily.get(date_key, 0) or 0) + 1
        self._prune_daily(state, timestamp)
        state["total_user_messages"] = int(state.get("total_user_messages", 0) or 0) + 1
        if not state.get("first_seen"):
            state["first_seen"] = timestamp
        state["last_user_ts"] = timestamp

        # 结算最近一条尚未回复的主动消息。
        if counts_as_reply:
            proactive = state["proactive"]
            window_seconds = max(1.0, settings.response_window_hours) * 3600.0
            for item in reversed(proactive):
                if not isinstance(item, dict) or item.get("replied_at") is not None:
                    continue
                sent_at = float(item.get("ts", 0) or 0)
                if sent_at <= 0:
                    continue
                if timestamp - sent_at <= window_seconds:
                    item["replied_at"] = round(timestamp, 3)
                else:
                    item["missed"] = True
                break

        self._prune_proactive(state, timestamp)
        return True

    def record_proactive_sent(
        self,
        session_id: str,
        *,
        now: float | None = None,
        settings: IntensitySettings | None = None,
        score: float | None = None,
    ) -> bool:
        """记录一次成功送达的主动消息，等待用户回复结算。"""
        timestamp = float(now if now is not None else time.time())
        settings = settings or self.get_settings()
        state = self.get_state(session_id, create=True)
        proactive = state["proactive"]

        # 先把已超过回复窗口的历史记录标记为未回复。
        window_seconds = max(1.0, settings.response_window_hours) * 3600.0
        for item in proactive:
            if not isinstance(item, dict) or item.get("replied_at") is not None:
                continue
            if item.get("missed"):
                continue
            sent_at = float(item.get("ts", 0) or 0)
            if sent_at > 0 and timestamp - sent_at > window_seconds:
                item["missed"] = True

        proactive.append(
            {
                "ts": round(timestamp, 3),
                "replied_at": None,
                "score": None if score is None else round(float(score), 2),
            }
        )
        if len(proactive) > MAX_PROACTIVE:
            del proactive[: len(proactive) - MAX_PROACTIVE]
        self._prune_proactive(state, timestamp)
        return True

    def _prune_daily(self, state: dict[str, Any], now: float) -> None:
        daily = state.get("daily", {})
        if not isinstance(daily, dict):
            state["daily"] = {}
            return
        keep = set(self._recent_date_keys(now, DAILY_KEEP_DAYS))
        for key in list(daily.keys()):
            if key not in keep:
                del daily[key]

    def _prune_proactive(self, state: dict[str, Any], now: float) -> None:
        proactive = state.get("proactive", [])
        if not isinstance(proactive, list):
            state["proactive"] = []
            return
        cutoff = now - PROACTIVE_KEEP_DAYS * 86400
        state["proactive"] = [
            item
            for item in proactive
            if isinstance(item, dict) and float(item.get("ts", 0) or 0) >= cutoff
        ][-MAX_PROACTIVE:]

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def get_private_settings(self) -> dict[str, Any]:
        """读取私聊全局配置（兼容旧版 friend_settings 键）。"""
        config = getattr(self.plugin, "config", {}) or {}
        for key in ("private_settings", "friend_settings"):
            value = config.get(key) if hasattr(config, "get") else None
            if isinstance(value, dict):
                return value
        return {}

    def get_settings(
        self, session_config: Mapping[str, Any] | None = None
    ) -> IntensitySettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            raw = session_config.get("intensity_settings")
        if not isinstance(raw, Mapping):
            raw = self.get_private_settings().get("intensity_settings")
        return IntensitySettings.from_dict(raw)

    # ------------------------------------------------------------------
    # 信号与快照
    # ------------------------------------------------------------------
    def build_signals(
        self,
        session_id: str,
        *,
        now: float | None = None,
        settings: IntensitySettings | None = None,
    ) -> IntensitySignals:
        timestamp = float(now if now is not None else time.time())
        settings = settings or self.get_settings()
        state = self.get_state(session_id)

        events = [
            item
            for item in state.get("events", [])
            if isinstance(item, dict) and float(item.get("t", 0) or 0) > 0
        ]
        events.sort(key=lambda item: float(item.get("t", 0) or 0))
        recent_events = [
            item
            for item in events
            if timestamp - float(item.get("t", 0) or 0) <= EVENT_SCAN_DAYS * 86400
        ]
        # 消息量很少时，用全部事件估计表达特征更稳定。
        feature_events = recent_events if len(recent_events) >= 5 else events[-20:]

        daily = state.get("daily", {})
        messages_24h = sum(
            1
            for item in events
            if timestamp - float(item.get("t", 0) or 0) <= 86400
        )
        if messages_24h == 0:
            messages_24h = int(daily.get(self._date_key(timestamp), 0) or 0)

        week_keys = self._recent_date_keys(timestamp, 7)
        messages_7d = sum(int(daily.get(key, 0) or 0) for key in week_keys)
        if messages_7d == 0:
            messages_7d = len(recent_events)

        recent_14 = set(self._recent_date_keys(timestamp, 14))
        active_days_14 = sum(
            1
            for key, value in daily.items()
            if key in recent_14 and int(value or 0) > 0
        )
        if active_days_14 == 0:
            active_days_14 = len(
                {
                    self._date_key(float(item.get("t", 0) or 0))
                    for item in events
                    if timestamp - float(item.get("t", 0) or 0) <= 14 * 86400
                }
            )

        timestamps = [float(item.get("t", 0) or 0) for item in events]
        gap = median_gap_minutes(timestamps, TEMPO_MAX_GAP_SECONDS)

        lengths = [int(item.get("n", 0) or 0) for item in feature_events]
        avg_length = (sum(lengths) / len(lengths)) if lengths else 0.0
        count = len(feature_events)
        question_ratio = (
            sum(int(item.get("q", 0) or 0) for item in feature_events) / count
            if count
            else 0.0
        )
        exclaim_ratio = (
            sum(int(item.get("e", 0) or 0) for item in feature_events) / count
            if count
            else 0.0
        )
        emoji_ratio = (
            sum(int(item.get("m", 0) or 0) for item in feature_events) / count
            if count
            else 0.0
        )

        last_user_ts = float(state.get("last_user_ts", 0) or 0)
        if last_user_ts <= 0 and timestamps:
            last_user_ts = max(timestamps)
        hours_since_last = (
            max(0.0, (timestamp - last_user_ts) / 3600.0)
            if last_user_ts > 0
            else None
        )

        window_seconds = max(1.0, settings.response_window_hours) * 3600.0
        cutoff = timestamp - PROACTIVE_KEEP_DAYS * 86400
        finalized: list[dict[str, Any]] = []
        for item in state.get("proactive", []):
            if not isinstance(item, dict):
                continue
            sent_at = float(item.get("ts", 0) or 0)
            if sent_at < cutoff:
                continue
            replied_at = item.get("replied_at")
            if replied_at is not None:
                finalized.append(item)
            elif item.get("missed") or (timestamp - sent_at > window_seconds):
                finalized.append(item)

        if finalized:
            replied_items = [
                item
                for item in finalized
                if item.get("replied_at") is not None
            ]
            reply_rate = len(replied_items) / len(finalized)
            if replied_items:
                latencies = [
                    (
                        float(item["replied_at"])
                        - float(item.get("ts", 0) or 0)
                    )
                    / 60.0
                    for item in replied_items
                ]
                avg_reply_minutes = sum(latencies) / len(latencies)
            else:
                avg_reply_minutes = None
        else:
            reply_rate = None
            avg_reply_minutes = None

        proactive_count_30d = sum(
            1
            for item in state.get("proactive", [])
            if isinstance(item, dict)
            and float(item.get("ts", 0) or 0) >= timestamp - 30 * 86400
        )

        return IntensitySignals(
            messages_24h=int(messages_24h),
            messages_7d=int(messages_7d),
            active_days_14=int(active_days_14),
            median_gap_minutes=gap,
            avg_length_7d=avg_length,
            question_ratio=question_ratio,
            exclaim_ratio=exclaim_ratio,
            emoji_ratio=emoji_ratio,
            hours_since_last_user=hours_since_last,
            reply_rate=reply_rate,
            avg_reply_minutes=avg_reply_minutes,
            total_user_messages=int(state.get("total_user_messages", 0) or 0),
            proactive_count_30d=int(proactive_count_30d),
        )

    def evaluate(
        self,
        session_id: str,
        session_config: Mapping[str, Any] | None = None,
        *,
        now: float | None = None,
        settings: IntensitySettings | None = None,
    ) -> dict[str, Any]:
        """返回一份完整的、可 JSON 序列化的强度快照。"""
        timestamp = float(now if now is not None else time.time())
        session_config = session_config or self._get_session_config(session_id)
        settings = settings or self.get_settings(session_config)
        signals = self.build_signals(session_id, now=timestamp, settings=settings)
        result = compute_score(signals, settings)

        schedule_conf = (
            session_config.get("schedule_settings", {})
            if isinstance(session_config, Mapping)
            else {}
        )
        base_min = self._as_number(schedule_conf.get("min_interval_minutes"), 30)
        base_max = max(base_min, self._as_number(schedule_conf.get("max_interval_minutes"), 600))
        if settings.enabled:
            effective_min, effective_max, scale = effective_interval_range(
                base_min, base_max, result.score, settings
            )
        else:
            # 强度调节关闭时，展示与调度都回到基础间隔。
            effective_min, effective_max, scale = (
                base_min * 60.0,
                base_max * 60.0,
                1.0,
            )

        state = self.get_state(session_id)
        session_data = (
            getattr(self.plugin, "session_data", {}).get(session_id, {})
            if isinstance(getattr(self.plugin, "session_data", {}), dict)
            else {}
        )
        last_decision = copy.deepcopy(state.get("last_decision", {}))
        seed_meta = copy.deepcopy(state.get("seed", {}))
        # 关闭强度调节时不再做概率拦截，展示为 100% 更符合实际行为。
        probability = result.probability if settings.enabled else 1.0

        paused_at = session_data.get(PAUSE_AT)
        try:
            paused_at_iso = (
                datetime.fromtimestamp(
                    float(paused_at), tz=getattr(self.plugin, "timezone", None)
                ).isoformat()
                if isinstance(paused_at, (int, float))
                else None
            )
        except (OverflowError, OSError, ValueError):
            paused_at_iso = None

        conversation_state: dict[str, Any] = {}
        rhythm_state: dict[str, Any] = {}
        persona_state: dict[str, Any] = {}
        conversation_memory: dict[str, Any] = {}
        try:
            state_service = getattr(self.plugin, "conversation_state_service", None)
            if state_service is not None:
                conversation_state = state_service.snapshot(
                    session_id, session_config=session_config, now=timestamp
                )
        except Exception:
            conversation_state = {}
        try:
            memory_service = getattr(
                self.plugin, "conversation_memory_service", None
            )
            if memory_service is not None:
                conversation_memory = memory_service.snapshot(
                    session_id, session_config=session_config, now=timestamp
                )
        except Exception:
            conversation_memory = {}
        try:
            rhythm_service = getattr(self.plugin, "rhythm_service", None)
            if rhythm_service is not None:
                rhythm_state = rhythm_service.snapshot(
                    session_id, now=timestamp, session_config=session_config
                )
        except Exception:
            rhythm_state = {}
        try:
            persona_service = getattr(self.plugin, "persona_state_service", None)
            if persona_service is not None:
                persona_state = persona_service.snapshot(
                    session_id, session_config=session_config, now=timestamp
                )
        except Exception:
            persona_state = {}

        return {
            "session_id": session_id,
            "score": round(result.score, 2),
            "tier": result.tier,
            "tier_label": result.tier_label,
            "components": {k: round(v, 4) for k, v in result.components.items()},
            "components_percent": {
                k: int(round(v * 100)) for k, v in result.components.items()
            },
            "probability": round(probability, 4),
            "probability_percent": int(round(probability * 100)),
            "interval_scale": round(scale, 4),
            "base_interval_minutes": [base_min, base_max],
            "effective_interval_minutes": [
                round(effective_min / 60.0, 1),
                round(effective_max / 60.0, 1),
            ],
            "metrics": {
                "messages_24h": signals.messages_24h,
                "messages_7d": signals.messages_7d,
                "active_days_14": signals.active_days_14,
                "total_user_messages": signals.total_user_messages,
                "median_gap_minutes": (
                    None
                    if signals.median_gap_minutes is None
                    else round(signals.median_gap_minutes, 2)
                ),
                "avg_length_7d": round(signals.avg_length_7d, 1),
                "question_ratio": round(signals.question_ratio, 3),
                "exclaim_ratio": round(signals.exclaim_ratio, 3),
                "emoji_ratio": round(signals.emoji_ratio, 3),
                "hours_since_last_user": (
                    None
                    if signals.hours_since_last_user is None
                    else round(signals.hours_since_last_user, 2)
                ),
                "last_user_at": (
                    datetime.fromtimestamp(
                        float(state.get("last_user_ts", 0) or 0),
                        tz=getattr(self.plugin, "timezone", None),
                    ).isoformat()
                    if float(state.get("last_user_ts", 0) or 0) > 0
                    else None
                ),
                "reply_rate": (
                    None
                    if signals.reply_rate is None
                    else round(signals.reply_rate, 3)
                ),
                "avg_reply_minutes": (
                    None
                    if signals.avg_reply_minutes is None
                    else round(signals.avg_reply_minutes, 1)
                ),
                "proactive_count_30d": signals.proactive_count_30d,
            },
            "enabled": settings.enabled,
            "has_state": bool(state.get("events")),
            "seed_source": seed_meta.get("source") or "",
            "last_decision": last_decision,
            "next_trigger_time": session_data.get("next_trigger_time"),
            "last_scheduled_at": session_data.get("last_scheduled_at"),
            "unanswered_count": session_data.get("unanswered_count", 0),
            "paused": bool(session_data.get(PAUSE_FLAG)),
            "paused_at": paused_at_iso,
            "pause_reason": str(session_data.get(PAUSE_REASON) or ""),
            "paused_unanswered_count": int(
                session_data.get(PAUSE_UNANSWERED, 0) or 0
            ),
            "paused_threshold": int(session_data.get(PAUSE_THRESHOLD, 0) or 0),
            "last_topic_judge": copy.deepcopy(
                session_data.get("last_topic_judge", {})
            ),
            "conversation_state": conversation_state,
            "conversation_memory": conversation_memory,
            "rhythm": rhythm_state,
            "persona": persona_state,
            "last_reply_quality": str(
                session_data.get("last_reply_quality") or ""
            ),
            "last_guard": copy.deepcopy(session_data.get("last_guard", {})),
            "last_critic": copy.deepcopy(session_data.get("last_critic", {})),
        }

    def decide(
        self,
        session_id: str,
        session_config: Mapping[str, Any] | None = None,
        *,
        trigger: str = "schedule",
        now: float | None = None,
        rng: random.Random | None = None,
        probability_multiplier: float = 1.0,
    ) -> tuple[bool, dict[str, Any]]:
        """执行一次触发概率判定，并把可解释记录写回状态。

        ``probability_multiplier`` 由人格状态（精力不足）等外部因素提供，
        只影响本次判定，不改写长期强度概率。
        """
        timestamp = float(now if now is not None else time.time())
        session_config = session_config or self._get_session_config(session_id)
        settings = self.get_settings(session_config)
        snapshot = self.evaluate(
            session_id, session_config, now=timestamp, settings=settings
        )

        scheduler_service = getattr(self.plugin, "scheduler_service", None)
        if scheduler_service is not None and scheduler_service.is_session_paused(
            session_id
        ):
            snapshot["roll"] = None
            snapshot["will_send"] = False
            snapshot["skip_reason"] = "proactive_paused"
            return False, snapshot

        if not settings.enabled:
            snapshot["roll"] = None
            snapshot["will_send"] = True
            snapshot["skip_reason"] = "intensity_disabled"
            return True, snapshot

        generator = rng or random
        multiplier = max(0.0, min(1.0, float(probability_multiplier)))
        effective_probability = snapshot["probability"] * multiplier
        snapshot["probability_multiplier"] = round(multiplier, 4)
        snapshot["effective_probability"] = round(effective_probability, 4)
        snapshot["effective_probability_percent"] = int(
            round(effective_probability * 100)
        )
        roll = generator.random()
        will_send = roll < effective_probability
        decision = {
            "at": round(timestamp, 3),
            "trigger": trigger,
            "score": snapshot["score"],
            "tier": snapshot["tier"],
            "probability": snapshot["probability"],
            "probability_multiplier": round(multiplier, 4),
            "effective_probability": round(effective_probability, 4),
            "roll": round(roll, 4),
            "will_send": bool(will_send),
        }
        state = self.get_state(session_id, create=True)
        state["last_decision"] = decision
        snapshot["roll"] = decision["roll"]
        snapshot["will_send"] = bool(will_send)
        snapshot["last_decision"] = decision

        if not will_send:
            effective_min = snapshot["effective_interval_minutes"][0] * 60.0
            snapshot["retry_after_seconds"] = skip_retry_seconds(
                effective_min, settings
            )
        else:
            snapshot["retry_after_seconds"] = 0
        return bool(will_send), snapshot

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def next_interval(
        self,
        session_id: str,
        schedule_conf: Mapping[str, Any] | None = None,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> dict[str, Any]:
        """计算强度修正后的下一次间隔（秒）。"""
        timestamp = float(now if now is not None else time.time())
        session_config = session_config or self._get_session_config(session_id)
        if not isinstance(schedule_conf, Mapping):
            schedule_conf = (
                session_config.get("schedule_settings", {})
                if isinstance(session_config, Mapping)
                else {}
            )

        base_min = self._as_number(schedule_conf.get("min_interval_minutes"), 30)
        base_max = max(
            base_min,
            self._as_number(schedule_conf.get("max_interval_minutes"), 600),
        )
        settings = self.get_settings(session_config)
        snapshot = self.evaluate(
            session_id, session_config, now=timestamp, settings=settings
        )
        if settings.enabled:
            effective_min, effective_max, scale = effective_interval_range(
                base_min, base_max, snapshot["score"], settings
            )
        else:
            effective_min, effective_max, scale = (
                base_min * 60.0,
                base_max * 60.0,
                1.0,
            )
        sampled = sample_interval_seconds(effective_min, effective_max, rng)
        return {
            "min_seconds": int(round(effective_min)),
            "max_seconds": int(round(effective_max)),
            "random_seconds": int(sampled),
            "score": snapshot["score"],
            "tier": snapshot["tier"],
            "probability": snapshot["probability"],
            "scale": scale,
            "applied": bool(settings.enabled),
        }

    # ------------------------------------------------------------------
    # 导入与汇总
    # ------------------------------------------------------------------
    def seed_state(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        replace: bool = True,
    ) -> bool:
        """写入离线导入的强度状态（供数据库导入工具使用）。"""
        if not isinstance(payload, Mapping):
            return False

        state = self.get_state(session_id, create=True)
        if replace:
            state.clear()
            state.update(self.default_state())
        else:
            state = self.get_state(session_id, create=True)

        events = payload.get("events", [])
        if isinstance(events, list):
            normalized_events = []
            for item in events[-MAX_EVENTS:]:
                if not isinstance(item, Mapping):
                    continue
                normalized_events.append(
                    {
                        "t": round(float(item.get("t", 0) or 0), 3),
                        "n": int(item.get("n", 0) or 0),
                        "q": int(item.get("q", 0) or 0),
                        "e": int(item.get("e", 0) or 0),
                        "m": int(item.get("m", 0) or 0),
                    }
                )
            state["events"] = normalized_events

        daily = payload.get("daily", {})
        if isinstance(daily, Mapping):
            state["daily"] = {
                str(key): int(value or 0)
                for key, value in daily.items()
                if int(value or 0) > 0
            }

        state["total_user_messages"] = int(
            payload.get("total_user_messages", len(state["events"])) or 0
        )
        state["first_seen"] = float(payload.get("first_seen", 0) or 0)
        state["last_user_ts"] = float(payload.get("last_user_ts", 0) or 0)
        state["seed"] = {
            "source": str(payload.get("source") or "astrbot_db"),
            "imported_at": time.time(),
            "platform": str(payload.get("platform") or ""),
        }
        return True

    def collect_snapshots(
        self,
        session_ids: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """批量生成快照，按分数从高到低排序。"""
        plugin = self.plugin
        if session_ids is None:
            session_ids = self._list_private_sessions()

        results: list[dict[str, Any]] = []
        for session_id in session_ids:
            try:
                snapshot = self.evaluate(session_id, now=now)
            except Exception as e:  # 单个会话异常不应拖垮整个面板
                logger.debug(f"[主动消息] 计算互动强度失败喵: {session_id} {e}")
                continue
            snapshot["session_name"] = self._session_name(session_id)
            snapshot["session_display_name"] = plugin.session_service.get_session_display_name(
                session_id
            )
            results.append(snapshot)

        results.sort(key=lambda item: (-item.get("score", 0), item.get("session_id", "")))
        return results

    def _list_private_sessions(self) -> list[str]:
        plugin = self.plugin
        sessions: set[str] = set()
        private_settings = self.get_private_settings()
        for value in private_settings.get("session_list", []) or []:
            if isinstance(value, str) and value.strip():
                try:
                    sessions.add(plugin.session_service.normalize_session_id(value.strip()))
                except Exception:
                    sessions.add(value.strip())

        for session_id in getattr(plugin, "session_data", {}) or {}:
            try:
                parsed = plugin.session_service.parse_session_id(session_id)
            except Exception:
                parsed = None
            if parsed and "friend" in str(parsed[1]).lower():
                sessions.add(session_id)

        return sorted(sessions)

    def _session_name(self, session_id: str) -> str:
        try:
            return self.plugin.session_service.get_session_name(session_id)
        except Exception:
            return ""

    def _get_session_config(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self.plugin.config_service.get_session_config(session_id)
        except Exception:
            return None

    @staticmethod
    def _as_number(value: Any, default: float) -> float:
        try:
            if isinstance(value, bool):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def summary(self, session_ids: list[str] | None = None) -> dict[str, Any]:
        """管理端顶部指标：平均分、各等级人数、平均概率。"""
        snapshots = self.collect_snapshots(session_ids)
        active = [item for item in snapshots if item.get("has_state")]
        tiers: dict[str, int] = {}
        for item in snapshots:
            tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
        avg_score = (
            sum(item["score"] for item in snapshots) / len(snapshots)
            if snapshots
            else 0.0
        )
        avg_probability = (
            sum(item["probability"] for item in snapshots) / len(snapshots)
            if snapshots
            else 0.0
        )
        hot = [item for item in snapshots if item["score"] >= 60]
        return {
            "total": len(snapshots),
            "tracked": len(active),
            "average_score": round(avg_score, 1),
            "average_probability": round(avg_probability, 4),
            "hot_count": len(hot),
            "tiers": tiers,
            "top_sessions": [
                {
                    "session_id": item["session_id"],
                    "session_display_name": item["session_display_name"],
                    "score": item["score"],
                    "tier": item["tier"],
                    "tier_label": item["tier_label"],
                    "probability_percent": item["probability_percent"],
                    "conversation_state_label": (
                        item.get("conversation_state") or {}
                    ).get("state_label"),
                    "persona_mood_label": (
                        item.get("persona") or {}
                    ).get("mood_label"),
                    "rhythm_active_hours_label": (
                        item.get("rhythm") or {}
                    ).get("active_hours_label"),
                }
                for item in snapshots[:5]
            ],
        }


def build_intensity_summary_from_result(result: IntensityResult) -> dict[str, Any]:
    """兼容辅助：把纯模型结果转为字典（测试与工具共用）。"""
    return result.as_dict()
