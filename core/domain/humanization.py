"""拟人化领域模型与纯逻辑。

本模块不依赖 AstrBot，负责：
- 解析 ``humanization_settings`` 配置；
- 对用户回复做质量分类（敷衍 / 负反馈 / 提问 / 实质回复）；
- 定义会话关系状态与主动意图；
- 提供文本相似度、发送气泡计划等可单测的纯函数。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "是"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "否", ""}:
            return False
    return default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _section(raw: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    value = raw.get(key)
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class RhythmSettings:
    """作息与发送预算配置。"""

    enabled: bool = True
    learn_active_hours: bool = True
    min_messages_to_learn: int = 10
    active_hours_margin_minutes: int = 45
    weekend_margin_minutes: int = 60
    daily_proactive_limit: int = 6
    min_gap_minutes: int = 30
    circadian_strength: float = 0.5

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> RhythmSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            learn_active_hours=_as_bool(raw.get("learn_active_hours"), True),
            min_messages_to_learn=_as_int(
                raw.get("min_messages_to_learn"), 10, 3, 200
            ),
            active_hours_margin_minutes=_as_int(
                raw.get("active_hours_margin_minutes"), 45, 0, 180
            ),
            weekend_margin_minutes=_as_int(
                raw.get("weekend_margin_minutes"), 60, 0, 240
            ),
            daily_proactive_limit=_as_int(
                raw.get("daily_proactive_limit"), 6, 0, 48
            ),
            min_gap_minutes=_as_int(raw.get("min_gap_minutes"), 30, 0, 720),
            circadian_strength=_as_float(
                raw.get("circadian_strength"), 0.5, 0.0, 1.0
            ),
        )


@dataclass(frozen=True)
class StateSettings:
    """会话状态与意图选择配置。"""

    enabled: bool = True
    open_thread_hours: float = 24.0
    awaiting_reply_hours: float = 36.0
    cooling_hours: float = 72.0
    silent_intent_enabled: bool = True
    silent_max_streak: int = 2

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> StateSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            open_thread_hours=_as_float(
                raw.get("open_thread_hours"), 24.0, 1.0, 168.0
            ),
            awaiting_reply_hours=_as_float(
                raw.get("awaiting_reply_hours"), 36.0, 1.0, 240.0
            ),
            cooling_hours=_as_float(
                raw.get("cooling_hours"), 72.0, 1.0, 720.0
            ),
            silent_intent_enabled=_as_bool(
                raw.get("silent_intent_enabled"), True
            ),
            silent_max_streak=_as_int(raw.get("silent_max_streak"), 2, 0, 10),
        )


@dataclass(frozen=True)
class FeedbackSettings:
    """回复质量与负反馈冷却配置。"""

    enabled: bool = True
    negative_cooldown_hours: float = 24.0
    perfunctory_no_reset: bool = True
    min_substantive_length: int = 6

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> FeedbackSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            negative_cooldown_hours=_as_float(
                raw.get("negative_cooldown_hours"), 24.0, 1.0, 720.0
            ),
            perfunctory_no_reset=_as_bool(
                raw.get("perfunctory_no_reset"), True
            ),
            min_substantive_length=_as_int(
                raw.get("min_substantive_length"), 6, 1, 100
            ),
        )


@dataclass(frozen=True)
class GuardSettings:
    """发送前自然度守卫配置。"""

    enabled: bool = True
    repeat_similarity: float = 0.65
    recent_message_count: int = 5
    max_message_chars: int = 300
    max_consecutive_questions: int = 2
    blocked_patterns: str = ""
    rewrite_on_fail: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> GuardSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            repeat_similarity=_as_float(
                raw.get("repeat_similarity"), 0.65, 0.1, 1.0
            ),
            recent_message_count=_as_int(
                raw.get("recent_message_count"), 5, 1, 30
            ),
            max_message_chars=_as_int(
                raw.get("max_message_chars"), 300, 40, 2000
            ),
            max_consecutive_questions=_as_int(
                raw.get("max_consecutive_questions"), 2, 1, 10
            ),
            blocked_patterns=str(raw.get("blocked_patterns") or ""),
            rewrite_on_fail=_as_bool(raw.get("rewrite_on_fail"), True),
        )


@dataclass(frozen=True)
class DeliveryPlanSettings:
    """分段发送与语音概率配置。"""

    enabled: bool = True
    max_bubbles: int = 3
    bubble_length_threshold: int = 60
    first_delay_min_seconds: float = 1.0
    first_delay_max_seconds: float = 4.0
    inter_bubble_min_seconds: float = 1.0
    inter_bubble_max_seconds: float = 3.0
    voice_probability: float = 0.25
    max_total_delay_seconds: float = 30.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> DeliveryPlanSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            max_bubbles=_as_int(raw.get("max_bubbles"), 3, 1, 6),
            bubble_length_threshold=_as_int(
                raw.get("bubble_length_threshold"), 60, 10, 500
            ),
            first_delay_min_seconds=_as_float(
                raw.get("first_delay_min_seconds"), 1.0, 0.0, 30.0
            ),
            first_delay_max_seconds=_as_float(
                raw.get("first_delay_max_seconds"), 4.0, 0.0, 60.0
            ),
            inter_bubble_min_seconds=_as_float(
                raw.get("inter_bubble_min_seconds"), 1.0, 0.0, 30.0
            ),
            inter_bubble_max_seconds=_as_float(
                raw.get("inter_bubble_max_seconds"), 3.0, 0.0, 60.0
            ),
            voice_probability=_as_float(
                raw.get("voice_probability"), 0.25, 0.0, 1.0
            ),
            max_total_delay_seconds=_as_float(
                raw.get("max_total_delay_seconds"), 30.0, 0.0, 120.0
            ),
        )


@dataclass(frozen=True)
class PersonaSettings:
    """人格状态（心情/精力）配置。"""

    enabled: bool = True
    baseline_energy: float = 0.6
    low_energy_threshold: float = 0.35
    low_energy_probability_reduction: float = 0.3

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> PersonaSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            baseline_energy=_as_float(
                raw.get("baseline_energy"), 0.6, 0.0, 1.0
            ),
            low_energy_threshold=_as_float(
                raw.get("low_energy_threshold"), 0.35, 0.0, 1.0
            ),
            low_energy_probability_reduction=_as_float(
                raw.get("low_energy_probability_reduction"), 0.3, 0.0, 1.0
            ),
        )


@dataclass(frozen=True)
class MemoryIntegrationSettings:
    """与 LivingMemory 等外部长期记忆插件的集成配置。"""

    enabled: bool = True
    plugin_id: str = "astrbot_plugin_livingmemory"
    top_k: int = 5
    inject_into_prompt: bool = True
    write_proactive: bool = True
    timeout_seconds: float = 5.0

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any] | None
    ) -> MemoryIntegrationSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            plugin_id=str(
                raw.get("plugin_id") or "astrbot_plugin_livingmemory"
            ).strip(),
            top_k=_as_int(raw.get("top_k"), 5, 0, 20),
            inject_into_prompt=_as_bool(raw.get("inject_into_prompt"), True),
            write_proactive=_as_bool(raw.get("write_proactive"), True),
            timeout_seconds=_as_float(
                raw.get("timeout_seconds"), 5.0, 0.5, 30.0
            ),
        )


@dataclass(frozen=True)
class ConversationMemorySettings:
    """短期工作记忆：画像、开放话题与话题账本。"""

    enabled: bool = True
    max_open_loops: int = 8
    max_recent_topics: int = 8
    ttl_days: float = 7.0
    followup_weight: float = 1.5
    profile_enabled: bool = True

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any] | None
    ) -> ConversationMemorySettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(raw.get("enable"), True),
            max_open_loops=_as_int(raw.get("max_open_loops"), 8, 0, 40),
            max_recent_topics=_as_int(raw.get("max_recent_topics"), 8, 0, 40),
            ttl_days=_as_float(raw.get("ttl_days"), 7.0, 0.5, 90.0),
            followup_weight=_as_float(
                raw.get("followup_weight"), 1.5, 1.0, 5.0
            ),
            profile_enabled=_as_bool(raw.get("profile_enabled"), True),
        )


@dataclass(frozen=True)
class GenerationSettings:
    """多阶段生成：planner / writer / critic。"""

    planner_enabled: bool = False
    critic_enabled: bool = False
    planner_timeout_seconds: float = 8.0
    critic_timeout_seconds: float = 10.0
    planner_provider_id: str = ""
    critic_provider_id: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> GenerationSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            planner_enabled=_as_bool(raw.get("planner_enabled"), False),
            critic_enabled=_as_bool(raw.get("critic_enabled"), False),
            planner_timeout_seconds=_as_float(
                raw.get("planner_timeout_seconds"), 8.0, 1.0, 60.0
            ),
            critic_timeout_seconds=_as_float(
                raw.get("critic_timeout_seconds"), 10.0, 1.0, 60.0
            ),
            planner_provider_id=str(
                raw.get("planner_provider_id") or ""
            ).strip(),
            critic_provider_id=str(
                raw.get("critic_provider_id") or ""
            ).strip(),
        )


@dataclass(frozen=True)
class HumanizationSettings:
    """拟人化配置聚合。"""

    rhythm: RhythmSettings = field(default_factory=RhythmSettings)
    state: StateSettings = field(default_factory=StateSettings)
    feedback: FeedbackSettings = field(default_factory=FeedbackSettings)
    guard: GuardSettings = field(default_factory=GuardSettings)
    delivery: DeliveryPlanSettings = field(default_factory=DeliveryPlanSettings)
    persona: PersonaSettings = field(default_factory=PersonaSettings)
    memory: MemoryIntegrationSettings = field(
        default_factory=MemoryIntegrationSettings
    )
    conversation_memory: ConversationMemorySettings = field(
        default_factory=ConversationMemorySettings
    )
    generation: GenerationSettings = field(default_factory=GenerationSettings)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> HumanizationSettings:
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            rhythm=RhythmSettings.from_dict(_section(raw, "rhythm_settings")),
            state=StateSettings.from_dict(_section(raw, "state_settings")),
            feedback=FeedbackSettings.from_dict(
                _section(raw, "feedback_settings")
            ),
            guard=GuardSettings.from_dict(_section(raw, "guard_settings")),
            delivery=DeliveryPlanSettings.from_dict(
                _section(raw, "delivery_plan_settings")
            ),
            persona=PersonaSettings.from_dict(
                _section(raw, "persona_settings")
            ),
            memory=MemoryIntegrationSettings.from_dict(
                _section(raw, "memory_integration_settings")
            ),
            conversation_memory=ConversationMemorySettings.from_dict(
                _section(raw, "conversation_memory_settings")
            ),
            generation=GenerationSettings.from_dict(
                _section(raw, "generation_settings")
            ),
        )


# ---------------------------------------------------------------------------
# 回复质量
# ---------------------------------------------------------------------------
class ReplyQuality(str, Enum):
    SUBSTANTIVE = "substantive"
    QUESTION = "question"
    PERFUNCTORY = "perfunctory"
    NEGATIVE = "negative"
    MEDIA = "media"


REPLY_QUALITY_LABELS = {
    ReplyQuality.SUBSTANTIVE: "实质回复",
    ReplyQuality.QUESTION: "提问/接话",
    ReplyQuality.PERFUNCTORY: "敷衍回应",
    ReplyQuality.NEGATIVE: "负反馈",
    ReplyQuality.MEDIA: "媒体消息",
}

_NEGATIVE_RE = re.compile(
    r"(别发了?|不要发了?|不要再发|别再发|别找我|别烦|烦不烦|不想聊|"
    r"没空|在忙|不要打扰|别打扰|闭嘴|滚开|滚|拉黑|举报|"
    r"shut\s*up|leave\s+me\s+alone|do\s+not\s+message|don'?t\s+message|"
    r"stop\s+messaging|i'?m\s+busy)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"[?？]|吗|呢|怎么|为什么|什么|哪儿|哪|如何|是否")
_EMOJI_ONLY_RE = re.compile(
    r"^[\s\U0001F300-\U0001FAFF\U00002600-\U000027BF"
    r"\U0001F000-\U0001F0FF\U00002190-\U000021FF"
    r"\U00002B00-\U00002BFF。！？!?~…]+$"
)
_PERFUNCTORY_TOKENS = {
    "嗯",
    "嗯嗯",
    "哦",
    "哦哦",
    "啊",
    "哈",
    "哈哈",
    "额",
    "呃",
    "噢",
    "好",
    "好的",
    "行",
    "可以",
    "收到",
    "ok",
    "okay",
    "k",
    "。",
    "...",
    "……",
}


def classify_reply(
    text: str,
    *,
    has_media: bool = False,
    min_substantive_length: int = 6,
) -> ReplyQuality:
    """把一条用户回复分类为可驱动调度与反馈的质量等级。"""
    normalized = str(text or "").strip()
    if not normalized:
        return ReplyQuality.MEDIA if has_media else ReplyQuality.PERFUNCTORY

    if _NEGATIVE_RE.search(normalized):
        return ReplyQuality.NEGATIVE
    if has_media:
        return ReplyQuality.MEDIA

    compact = re.sub(r"\s+", "", normalized).lower()
    if (
        len(compact) <= 1
        or compact in _PERFUNCTORY_TOKENS
        or _EMOJI_ONLY_RE.match(normalized)
    ):
        return ReplyQuality.PERFUNCTORY

    if _QUESTION_RE.search(normalized):
        return ReplyQuality.QUESTION

    # 短回复不必然敷衍：“到了”“晚安”“在路上了”都是有效回应；
    # 只有整句都由语气词构成时才归为敷衍。
    if len(compact) <= max(1, min_substantive_length) and all(
        char in "嗯哦啊哈额呃噢好吧行可以收到没在" for char in compact
    ):
        return ReplyQuality.PERFUNCTORY
    return ReplyQuality.SUBSTANTIVE


# ---------------------------------------------------------------------------
# 会话状态与意图
# ---------------------------------------------------------------------------
class ConversationState(str, Enum):
    IDLE = "idle"
    OPEN_THREAD = "open_thread"
    AWAITING_REPLY = "awaiting_reply"
    COOLING = "cooling"
    PAUSED = "paused"


CONVERSATION_STATE_LABELS = {
    ConversationState.IDLE: "空闲",
    ConversationState.OPEN_THREAD: "话题未结束",
    ConversationState.AWAITING_REPLY: "等待回应",
    ConversationState.COOLING: "降温中",
    ConversationState.PAUSED: "已暂停",
}


class Intent(str, Enum):
    SILENT = "silent"
    CONTINUE_TOPIC = "continue_topic"
    NEW_TOPIC = "new_topic"
    CARE = "care"
    SHARE_SELF = "share_self"
    ASK = "ask"


INTENT_LABELS = {
    Intent.SILENT: "暂不发言",
    Intent.CONTINUE_TOPIC: "延续话题",
    Intent.NEW_TOPIC: "开启新话题",
    Intent.CARE: "关心近况",
    Intent.SHARE_SELF: "分享自己",
    Intent.ASK: "具体提问",
}

INTENT_BRIEFS = {
    Intent.CONTINUE_TOPIC: (
        "延续你们上次没有聊完的具体话题，接着当时的细节说，"
        "不要重新问“在干嘛”，也不要总结聊天记录。"
    ),
    Intent.NEW_TOPIC: (
        "开启一个轻量、具体的新话题。避免空泛问候，最好从你此刻的生活细节"
        "或一个自然的好奇点切入。"
    ),
    Intent.CARE: (
        "关心对方最近提到过的事或此刻的状态，语气轻一点，"
        "只问一个具体问题，不要连续追问。"
    ),
    Intent.SHARE_SELF: (
        "分享你此刻的一件真实小事（不要编造重大事件），"
        "再把话头自然交给对方，而不是自顾自说一大段。"
    ),
    Intent.ASK: (
        "只问一个具体、容易回答的问题，不要连续提问，"
        "也不要用审问式的语气。"
    ),
}


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(
    r"[\s，,。.!！?？~～…、；;：:\"'“”‘’（）()\[\]【】<>《》\-—_/\\|]+"
)


def normalize_for_similarity(text: str) -> str:
    normalized = str(text or "").lower()
    normalized = _EMOJI_ONLY_RE.sub("", normalized)
    return _PUNCT_RE.sub("", normalized)


def _char_bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[i : i + 2] for i in range(len(value) - 1)}


def text_similarity(left: str, right: str) -> float:
    """返回 0–1 的文本相似度，兼顾字符序列与二元组重合。"""
    a = normalize_for_similarity(left)
    b = normalize_for_similarity(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_bigrams = _char_bigrams(a)
    b_bigrams = _char_bigrams(b)
    union = a_bigrams | b_bigrams
    jaccard = len(a_bigrams & b_bigrams) / len(union) if union else 0.0
    return max(sequence, jaccard)


def has_question(text: str) -> bool:
    return bool(re.search(r"[?？]|吗|呢|怎么|为什么|什么|哪儿|哪|如何", text or ""))


# ---------------------------------------------------------------------------
# 发送计划
# ---------------------------------------------------------------------------
@dataclass
class PlannedBubble:
    """一条待发送气泡。"""

    text: str
    delay_seconds: float = 0.0


@dataclass
class DeliveryPlan:
    """一次主动消息的发送计划。"""

    bubbles: list[PlannedBubble] = field(default_factory=list)
    voice: bool = False
    ask_question: bool = False
    use_chain: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bubbles": [
                {"text": bubble.text, "delay_seconds": round(bubble.delay_seconds, 2)}
                for bubble in self.bubbles
            ],
            "voice": bool(self.voice),
            "ask_question": bool(self.ask_question),
            "use_chain": bool(self.use_chain),
            "reason": self.reason,
        }


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?…~])\s*|\n+", str(text or ""))
    return [part.strip() for part in parts if part and part.strip()]


def _group_sentences(sentences: list[str], max_bubbles: int) -> list[str]:
    if len(sentences) <= max_bubbles:
        return sentences
    # 按累计长度均分，保证每条气泡不会太短或太长。
    total = sum(len(sentence) for sentence in sentences)
    target = total / max_bubbles
    groups: list[str] = []
    current = ""
    for sentence in sentences:
        if (
            current
            and len(current) + len(sentence) > target * 1.25
            and len(groups) < max_bubbles - 1
        ):
            groups.append(current)
            current = sentence
        else:
            current = current + sentence if current else sentence
    if current:
        groups.append(current)
    return groups[:max_bubbles]


def build_delivery_plan(
    text: str,
    settings: DeliveryPlanSettings,
    *,
    has_rich_chain: bool = False,
    voice_allowed: bool = False,
    rng: random.Random | None = None,
) -> DeliveryPlan:
    """根据文本长度与配置生成气泡级发送计划。"""
    generator = rng or random
    normalized = str(text or "").strip()

    if not settings.enabled:
        return DeliveryPlan(
            bubbles=[PlannedBubble(text=normalized, delay_seconds=0.0)],
            voice=bool(voice_allowed),
            ask_question=has_question(normalized),
            use_chain=has_rich_chain and not normalized,
            reason="delivery_plan_disabled",
        )

    if has_rich_chain and not normalized:
        return DeliveryPlan(
            bubbles=[PlannedBubble(text="", delay_seconds=0.0)],
            voice=False,
            ask_question=False,
            use_chain=True,
            reason="rich_chain",
        )

    if not normalized:
        return DeliveryPlan(reason="empty")

    if len(normalized) <= settings.bubble_length_threshold or settings.max_bubbles <= 1:
        groups = [normalized]
    else:
        sentences = _split_sentences(normalized)
        groups = _group_sentences(sentences, settings.max_bubbles) or [normalized]

    first_delay = generator.uniform(
        min(settings.first_delay_min_seconds, settings.first_delay_max_seconds),
        max(settings.first_delay_min_seconds, settings.first_delay_max_seconds),
    )
    bubbles: list[PlannedBubble] = []
    total_delay = 0.0
    for index, group in enumerate(groups):
        if index == 0:
            delay = first_delay
        else:
            previous_length = len(groups[index - 1])
            base = generator.uniform(
                min(
                    settings.inter_bubble_min_seconds,
                    settings.inter_bubble_max_seconds,
                ),
                max(
                    settings.inter_bubble_min_seconds,
                    settings.inter_bubble_max_seconds,
                ),
            )
            delay = base + min(1.5, previous_length * 0.01)
        if total_delay + delay > settings.max_total_delay_seconds:
            delay = max(0.0, settings.max_total_delay_seconds - total_delay)
        total_delay += delay
        bubbles.append(PlannedBubble(text=group, delay_seconds=delay))

    return DeliveryPlan(
        bubbles=bubbles,
        voice=bool(voice_allowed)
        and generator.random() < settings.voice_probability,
        ask_question=has_question(normalized),
        reason="planned",
    )


def persona_energy_curve(hour: int) -> float:
    """一天中的精力曲线（0–1），仅用于制造合理的“作息感”。"""
    hour = int(hour) % 24
    if hour < 6:
        return 0.25
    if hour < 9:
        return 0.5
    if hour < 12:
        return 0.8
    if hour < 14:
        return 0.65
    if hour < 18:
        return 0.85
    if hour < 22:
        return 0.75
    return 0.45


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def saturate(value: float) -> float:
    """把任意非负值压到 0–1。"""
    return clamp(1.0 - math.exp(-max(0.0, float(value))))
