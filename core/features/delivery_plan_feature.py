"""发送计划阶段：把一段回复拆成 1–N 条有节奏的气泡。"""

from __future__ import annotations

import random

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..domain.humanization import DeliveryPlanSettings, build_delivery_plan
from ..pipeline.stage_protocol import BaseStage


class DeliveryPlanStage(BaseStage):
    """生成气泡级发送计划。"""

    name = "delivery_plan_stage"
    hook = HookPoint.DELIVER
    priority = 5

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_config = plugin.config_service.get_session_config(ctx.session_id) or {}
        humanization = session_config.get("humanization_settings", {})
        raw_settings = (
            humanization.get("delivery_plan_settings", {})
            if isinstance(humanization, dict)
            else {}
        )
        settings = DeliveryPlanSettings.from_dict(raw_settings)
        tts_conf = session_config.get("tts_settings", {})
        voice_allowed = bool(tts_conf.get("enable_tts", True))

        text = str((ctx.extra or {}).get("response_text") or "")
        chain = (ctx.extra or {}).get("result_chain") or []
        has_rich_chain = any(type(comp).__name__ != "Plain" for comp in chain)
        plan = build_delivery_plan(
            text,
            settings,
            has_rich_chain=has_rich_chain,
            voice_allowed=voice_allowed,
            rng=random,
        )
        ctx.extra["delivery_plan"] = plan.as_dict()
        logger.info(
            f"[主动消息] 发送计划：{len(plan.bubbles)} 条气泡"
            f"{'，语音' if plan.voice else ''}"
            f"（{plan.reason}）喵。"
        )
        return StageResult.CONTINUE


class DeliveryPlanFeature:
    """发送计划阶段功能。"""

    name = "delivery_plan_feature"

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIVER,
            stage=DeliveryPlanStage(self.orchestrator),
            priority=5,
        )
