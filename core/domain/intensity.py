"""私聊互动强度模型（纯函数）。

本模块只依赖标准库，不触碰 AstrBot 运行时，便于单元测试与离线校准。

设计目标
--------
1. 把「聊得有多热」量化成一个 0~100 的分数；
2. 分数同时驱动两件事：
   - 主动消息的触发概率（越热概率越高）；
   - 下一次主动消息的间隔（越热间隔越短）；
3. 所有参数都可通过 ``intensity_settings`` 配置覆盖。

评分信号
--------
- ``frequency``      最近 24 小时 / 7 天的消息密度；
- ``tempo``          最近来回消息的中位间隔（越快越热）；
- ``streak``         最近 14 天的活跃天数（持续聊天）；
- ``engagement``     对历史主动消息的回复率与回复速度；
- ``expressiveness`` 消息长度、疑问、感叹、表情占比；
- ``recency``        距离用户上一条消息的时间（指数衰减）。

最终分数会再乘上「新鲜度因子」，避免一个曾经很热但已沉默很久的用户
仍然保持高热度。
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 分数区间与等级。区间上界不包含，最后一档包含 100。
#: 阈值参考真实私聊数据校准：绝大多数一次性会话落在 <40，
#: 持续互动用户集中在 40~70，长期高频用户可达 70+。
TIER_TABLE: tuple[tuple[float, str, str], ...] = (
    (20.0, "dormant", "冷淡"),
    (40.0, "steady", "平稳"),
    (55.0, "warm", "热络"),
    (70.0, "close", "亲密"),
    (101.0, "blazing", "炽热"),
)

#: 频率饱和点：达到该消息量时子分数约为 1。
FREQUENCY_24H_SATURATION = 40.0
FREQUENCY_7D_DAILY_SATURATION = 25.0
STREAK_ACTIVE_DAYS_SATURATION = 10.0
EXPRESSIVENESS_LENGTH_SATURATION = 120.0
TEMPO_SCALE_MINUTES = 120.0

#: 各维度权重（合计 1.0）。
WEIGHT_FREQUENCY = 0.30
WEIGHT_TEMPO = 0.22
WEIGHT_STREAK = 0.16
WEIGHT_ENGAGEMENT = 0.18
WEIGHT_EXPRESSIVENESS = 0.14

#: 新鲜度：0 小时为 1，半个半衰期后为 0.5。
DEFAULT_RECENCY_HALF_LIFE_HOURS = 36.0


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "启用"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "禁用", ""}:
            return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_ratio(
    value: Any,
    default: float,
    *,
    percent_above: float = 1.0,
    integer_is_percent: bool = False,
) -> float:
    """解析比例配置。

    兼容两种写法：
    - 0~1 的小数（如 0.15、1.15）；
    - 0~300 的百分数（如 15、115），大于阈值时自动除以 100。

    ``integer_is_percent=True`` 用于概率字段：Schema 使用 0~100 的整数，
    因此整数 1 表示 1%，而不是 100%。
    """
    if (
        integer_is_percent
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        return float(value) / 100.0

    numeric = _as_float(value, default)
    if abs(numeric) > percent_above:
        numeric = numeric / 100.0
    return numeric


@dataclass(frozen=True)
class IntensitySettings:
    """互动强度相关配置（已做边界收敛）。"""

    enabled: bool = True
    probability_min: float = 0.15
    probability_max: float = 0.95
    probability_gamma: float = 1.15
    interval_scale_hot: float = 0.35
    interval_scale_cold: float = 1.15
    interval_gamma: float = 1.0
    min_interval_floor_minutes: int = 8
    response_window_hours: float = 6.0
    recency_half_life_hours: float = DEFAULT_RECENCY_HALF_LIFE_HOURS
    apply_to_auto_trigger: bool = True
    prompt_context: bool = True
    skip_retry_ratio: float = 0.2
    min_skip_retry_minutes: int = 5

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> IntensitySettings:
        """从配置字典构建，非法值回退默认值。"""
        if not isinstance(raw, Mapping):
            raw = {}

        probability_min = _clamp(
            _as_ratio(
                raw.get("probability_min"), 0.15, integer_is_percent=True
            ),
            0.0,
            1.0,
        )
        probability_max = _clamp(
            _as_ratio(
                raw.get("probability_max"), 0.95, integer_is_percent=True
            ),
            0.0,
            1.0,
        )
        if probability_max < probability_min:
            probability_max = probability_min

        hot_scale = _clamp(
            _as_ratio(raw.get("interval_scale_hot"), 0.35, percent_above=3.0),
            0.05,
            1.0,
        )
        cold_scale = _clamp(
            _as_ratio(raw.get("interval_scale_cold"), 1.15, percent_above=3.0),
            0.05,
            3.0,
        )
        if cold_scale < hot_scale:
            cold_scale = hot_scale

        return cls(
            enabled=_as_bool(raw.get("enable", raw.get("enabled")), True),
            probability_min=probability_min,
            probability_max=probability_max,
            probability_gamma=_clamp(
                _as_ratio(raw.get("probability_gamma"), 1.15, percent_above=5.0),
                0.2,
                3.0,
            ),
            interval_scale_hot=hot_scale,
            interval_scale_cold=cold_scale,
            interval_gamma=_clamp(
                _as_ratio(raw.get("interval_gamma"), 1.0, percent_above=5.0),
                0.2,
                3.0,
            ),
            min_interval_floor_minutes=max(
                1, _as_int(raw.get("min_interval_floor_minutes"), 8)
            ),
            response_window_hours=max(
                0.25, _as_float(raw.get("response_window_hours"), 6.0)
            ),
            recency_half_life_hours=max(
                1.0, _as_float(raw.get("recency_half_life_hours"), 36.0)
            ),
            apply_to_auto_trigger=_as_bool(
                raw.get("apply_to_auto_trigger"), True
            ),
            prompt_context=_as_bool(raw.get("prompt_context"), True),
            skip_retry_ratio=_clamp(
                _as_float(raw.get("skip_retry_ratio"), 0.2), 0.0, 1.0
            ),
            min_skip_retry_minutes=max(
                1, _as_int(raw.get("min_skip_retry_minutes"), 5)
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enable": self.enabled,
            "probability_min": self.probability_min,
            "probability_max": self.probability_max,
            "probability_gamma": self.probability_gamma,
            "interval_scale_hot": self.interval_scale_hot,
            "interval_scale_cold": self.interval_scale_cold,
            "interval_gamma": self.interval_gamma,
            "min_interval_floor_minutes": self.min_interval_floor_minutes,
            "response_window_hours": self.response_window_hours,
            "recency_half_life_hours": self.recency_half_life_hours,
            "apply_to_auto_trigger": self.apply_to_auto_trigger,
            "prompt_context": self.prompt_context,
            "skip_retry_ratio": self.skip_retry_ratio,
            "min_skip_retry_minutes": self.min_skip_retry_minutes,
        }


# ---------------------------------------------------------------------------
# 信号与评分
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntensitySignals:
    """某一时刻用于评分的原始信号。"""

    messages_24h: int = 0
    messages_7d: int = 0
    active_days_14: int = 0
    median_gap_minutes: float | None = None
    avg_length_7d: float = 0.0
    question_ratio: float = 0.0
    exclaim_ratio: float = 0.0
    emoji_ratio: float = 0.0
    hours_since_last_user: float | None = None
    reply_rate: float | None = None
    avg_reply_minutes: float | None = None
    total_user_messages: int = 0
    proactive_count_30d: int = 0


@dataclass(frozen=True)
class IntensityResult:
    """评分结果。"""

    score: float
    tier: str
    tier_label: str
    components: dict[str, float] = field(default_factory=dict)
    probability: float = 0.0
    interval_scale: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "tier": self.tier,
            "tier_label": self.tier_label,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "probability": round(self.probability, 4),
            "interval_scale": round(self.interval_scale, 4),
        }


def _saturate_log(value: float, saturation: float) -> float:
    """对数饱和：value 达到 saturation 时返回 1，超过后仍为 1。"""
    if saturation <= 1:
        return 1.0 if value > 0 else 0.0
    if value <= 0:
        return 0.0
    return _clamp(math.log1p(value) / math.log1p(saturation), 0.0, 1.0)


def _tier_for(score: float) -> tuple[str, str]:
    for upper, key, label in TIER_TABLE:
        if score < upper:
            return key, label
    return "blazing", "炽热"


def compute_score(
    signals: IntensitySignals,
    settings: IntensitySettings | None = None,
) -> IntensityResult:
    """把原始信号折算为 0~100 的互动强度分。

    缺席信号的处理原则：
    - 没有历史回复记录时，engagement 取中性值 0.5，而不是 0；
    - 只有一个消息点时 tempo 取中性值 0.3，避免把「刚开始聊」误判为冷；
    - 从未发过消息时按完全冷淡处理。
    """
    settings = settings or IntensitySettings()

    # 1. 频率：24h 与 7 天日均各占一部分。
    freq_24h = _saturate_log(
        float(max(0, signals.messages_24h)), FREQUENCY_24H_SATURATION
    )
    freq_7d = _saturate_log(
        max(0.0, float(signals.messages_7d) / 7.0),
        FREQUENCY_7D_DAILY_SATURATION,
    )
    frequency = 0.6 * freq_24h + 0.4 * freq_7d

    # 2. 节奏：中位间隔越短越热；无间隔数据时取偏低的中性值。
    if signals.median_gap_minutes is None:
        tempo = 0.25
    else:
        tempo = math.exp(-max(0.0, signals.median_gap_minutes) / TEMPO_SCALE_MINUTES)

    # 3. 连续性：14 天内活跃天数。
    streak = _clamp(
        float(max(0, signals.active_days_14)) / STREAK_ACTIVE_DAYS_SATURATION,
        0.0,
        1.0,
    )

    # 4. 主动消息回复意愿。
    if signals.reply_rate is None:
        engagement = 0.4
    else:
        if signals.avg_reply_minutes is None:
            latency_factor = 0.6
        else:
            latency_factor = _clamp(
                math.exp(-max(0.0, signals.avg_reply_minutes - 5.0) / 120.0),
                0.15,
                1.0,
            )
        engagement = _clamp(
            (0.65 * signals.reply_rate) + (0.35 * signals.reply_rate * latency_factor),
            0.0,
            1.0,
        )

    # 5. 表达欲：长度 + 疑问 + 感叹 + 表情。
    length_score = _saturate_log(
        max(0.0, signals.avg_length_7d), EXPRESSIVENESS_LENGTH_SATURATION
    )
    expressiveness = _clamp(
        (0.40 * length_score)
        + (0.25 * _clamp(signals.question_ratio, 0.0, 1.0))
        + (0.15 * _clamp(signals.exclaim_ratio, 0.0, 1.0))
        + (0.20 * _clamp(signals.emoji_ratio, 0.0, 1.0)),
        0.0,
        1.0,
    )

    raw = (
        (WEIGHT_FREQUENCY * frequency)
        + (WEIGHT_TEMPO * tempo)
        + (WEIGHT_STREAK * streak)
        + (WEIGHT_ENGAGEMENT * engagement)
        + (WEIGHT_EXPRESSIVENESS * expressiveness)
    )

    # 新鲜度衰减：沉默越久，分数越低；完全没出现过则直接为 0。
    if signals.hours_since_last_user is None:
        recency = 0.0
        freshness = 0.0
    else:
        recency = math.pow(
            0.5,
            max(0.0, signals.hours_since_last_user)
            / max(1.0, settings.recency_half_life_hours),
        )
        freshness = 0.15 + (0.85 * recency)

    # 样本置信度：只有一两条消息的会话不足以判断“聊得热”，
    # 用对数曲线压低低样本会话的分数，避免误判。
    volume = 0.55 + (0.45 * _saturate_log(
        float(max(0, signals.total_user_messages)), 30.0
    ))

    score = _clamp(100.0 * raw * freshness * volume, 0.0, 100.0)

    probability = probability_for_score(score, settings)
    interval_scale = interval_scale_for_score(score, settings)
    tier, tier_label = _tier_for(score)

    return IntensityResult(
        score=score,
        tier=tier,
        tier_label=tier_label,
        components={
            "frequency": frequency,
            "tempo": tempo,
            "streak": streak,
            "engagement": engagement,
            "expressiveness": expressiveness,
            "recency": recency,
            "volume": volume,
        },
        probability=probability,
        interval_scale=interval_scale,
    )


# ---------------------------------------------------------------------------
# 概率与间隔映射
# ---------------------------------------------------------------------------


def probability_for_score(
    score: float, settings: IntensitySettings | None = None
) -> float:
    """把强度分映射为主动消息触发概率。"""
    settings = settings or IntensitySettings()
    normalized = _clamp(score / 100.0, 0.0, 1.0)
    shaped = math.pow(normalized, max(0.2, settings.probability_gamma))
    return _clamp(
        settings.probability_min
        + ((settings.probability_max - settings.probability_min) * shaped),
        0.0,
        1.0,
    )


def interval_scale_for_score(
    score: float, settings: IntensitySettings | None = None
) -> float:
    """把强度分映射为间隔缩放系数（越热越小）。"""
    settings = settings or IntensitySettings()
    normalized = _clamp(score / 100.0, 0.0, 1.0)
    shaped = math.pow(normalized, max(0.2, settings.interval_gamma))
    return _clamp(
        settings.interval_scale_cold
        - ((settings.interval_scale_cold - settings.interval_scale_hot) * shaped),
        0.05,
        3.0,
    )


def effective_interval_range(
    base_min_minutes: float,
    base_max_minutes: float,
    score: float,
    settings: IntensitySettings | None = None,
) -> tuple[float, float, float]:
    """计算强度修正后的有效间隔区间。

    Returns:
        (effective_min_seconds, effective_max_seconds, interval_scale)
    """
    settings = settings or IntensitySettings()
    scale = interval_scale_for_score(score, settings)

    base_min_seconds = max(1.0, float(base_min_minutes) * 60.0)
    base_max_seconds = max(base_min_seconds, float(base_max_minutes) * 60.0)
    floor_seconds = max(1.0, float(settings.min_interval_floor_minutes) * 60.0)

    effective_min = max(floor_seconds, base_min_seconds * scale)
    effective_max = max(effective_min, base_max_seconds * scale)
    return effective_min, effective_max, scale


def sample_interval_seconds(
    effective_min_seconds: float,
    effective_max_seconds: float,
    rng: random.Random | None = None,
) -> int:
    """在有效区间内随机取一个整数秒。"""
    low = max(1, int(round(effective_min_seconds)))
    high = max(low, int(round(effective_max_seconds)))
    generator = rng or random
    return int(generator.randint(low, high))


def skip_retry_seconds(
    effective_min_seconds: float,
    settings: IntensitySettings | None = None,
) -> int:
    """概率未命中时的短重试间隔。

    不直接等待完整间隔，而是在一个较短的窗口后重新掷骰，
    让「热」用户有更多自然触发的机会，同时避免过于频繁。
    """
    settings = settings or IntensitySettings()
    floor_seconds = max(1, settings.min_skip_retry_minutes) * 60
    ratio_seconds = max(1.0, effective_min_seconds) * _clamp(
        settings.skip_retry_ratio, 0.0, 1.0
    )
    return int(max(floor_seconds, min(ratio_seconds, effective_min_seconds)))


# ---------------------------------------------------------------------------
# 文案辅助
# ---------------------------------------------------------------------------


def format_intensity_context(
    result: IntensityResult,
    *,
    probability: float | None = None,
) -> str:
    """生成可注入提示词的互动强度描述。"""
    probability_text = (
        f"{round((probability if probability is not None else result.probability) * 100)}%"
    )
    return (
        f"- 当前互动强度：{result.tier_label}（{round(result.score)}/100）\n"
        f"- 本轮主动消息触发概率：{probability_text}\n"
        f"- 参考含义：分数越高代表对方最近聊得越投入、回复越快，"
        f"可以更大胆、更主动地延续话题。"
    )


def summarize_tiers(results: list[IntensityResult]) -> dict[str, int]:
    """按等级汇总数量，供管理端展示。"""
    summary = {key: 0 for _, key, _ in TIER_TABLE}
    for result in results:
        summary[result.tier] = summary.get(result.tier, 0) + 1
    return summary


def median_gap_minutes(timestamps: list[float], max_gap_seconds: float = 6 * 3600) -> float | None:
    """计算相邻时间戳的中位间隔（分钟），过滤异常长间隔。"""
    if len(timestamps) < 2:
        return None
    ordered = sorted(timestamps)
    gaps = [
        (later - earlier) / 60.0
        for earlier, later in zip(ordered, ordered[1:])
        if 0 < (later - earlier) <= max_gap_seconds
    ]
    if not gaps:
        return None
    return float(statistics.median(gaps))
