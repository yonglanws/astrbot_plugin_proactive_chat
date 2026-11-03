"""功能装配清单。

默认功能顺序即为流水线骨架对应的默认行为。未来新增功能只需在此处
追加一行（或一个 Feature），主干无需改动。
"""

from __future__ import annotations

from typing import Any

from .context.conversation_source import ConversationSourceFeature
from .critic_feature import CriticFeature
from .deliver.split_delivery import SplitDeliveryFeature
from .delivery_plan_feature import DeliveryPlanFeature
from .gating_feature import GatingFeature
from .guard_feature import GuardFeature
from .intent_feature import IntentFeature
from .plan_feature import PlanFeature
from .prompt.single_prompt import SinglePromptFeature
from .record.history_writer import HistoryWriterFeature
from .review_feature import ReviewFeature


def build_default_features(orchestrator: Any) -> list:
    """构建默认功能清单。

    Args:
        orchestrator: 编排器实例，Feature 通过它访问插件能力。

    Returns:
        按默认顺序排列的 Feature 列表。
    """
    return [
        GatingFeature(orchestrator),
        IntentFeature(orchestrator),
        ConversationSourceFeature(orchestrator),
        PlanFeature(orchestrator),
        SinglePromptFeature(orchestrator),
        ReviewFeature(orchestrator),
        GuardFeature(orchestrator),
        CriticFeature(orchestrator),
        DeliveryPlanFeature(orchestrator),
        SplitDeliveryFeature(orchestrator),
        HistoryWriterFeature(orchestrator),
        # ↓ 未来新功能在此追加一行即可（如 ToolLoopFeature）
    ]
