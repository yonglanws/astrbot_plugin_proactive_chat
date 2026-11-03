"""INTENT 阶段：根据关系状态、精力与历史反馈选择本轮主动意图。"""

from __future__ import annotations

import random

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult, TriggerSource
from ..pipeline.stage_protocol import BaseStage


class IntentStage(BaseStage):
    """意图选择阶段。"""

    name = "intent_stage"
    hook = HookPoint.INTENT
    priority = 10

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id
        session_config = plugin.config_service.get_session_config(session_id) or {}

        manual = ctx.trigger is TriggerSource.MANUAL
        try:
            persona = plugin.persona_state_service.snapshot(
                session_id, session_config=session_config
            )
            energy = float(persona.get("energy", 0.6) or 0.6)
        except Exception:
            energy = 0.6

        try:
            open_loops = plugin.conversation_memory_service.open_loop_count(
                session_id, session_config=session_config
            )
        except Exception:
            open_loops = 0

        async with plugin.data_lock:
            decision = plugin.conversation_state_service.choose_intent(
                session_id,
                session_config=session_config,
                persona_energy=energy,
                open_loop_count=open_loops,
                now=ctx.started_at,
                rng=random,
                manual=manual,
            )

        ctx.extra["intent_decision"] = decision
        ctx.extra["intent"] = decision.get("intent", "continue_topic")
        ctx.extra["intent_brief"] = decision.get("brief", "")
        ctx.extra["conversation_state"] = decision.get("state", "idle")

        if decision.get("silent"):
            # 有时候什么都不说，比硬找一句话更自然。沉默不是失败，
            # 只是把下一次机会往后放。
            retry_after = self._silent_retry_seconds(session_config)
            ctx.extra["retry_after_seconds"] = retry_after
            ctx.abort("intent_silent")
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                f"本轮意图为「暂不发言」，约 {max(1, retry_after // 60)} 分钟后重新评估喵。"
            )
            return StageResult.RESCHEDULE

        logger.info(
            f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
            f"本轮意图：{decision.get('intent_label')}（{decision.get('reason')}）喵。"
        )
        return StageResult.CONTINUE

    @staticmethod
    def _silent_retry_seconds(session_config: dict) -> int:
        schedule_conf = session_config.get("schedule_settings", {})
        try:
            min_minutes = max(1, int(schedule_conf.get("min_interval_minutes", 30)))
        except (TypeError, ValueError):
            min_minutes = 30
        return max(600, int(min_minutes * 60 * 0.25))


class IntentFeature:
    """INTENT 阶段功能。"""

    name = "intent_feature"

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.INTENT, stage=IntentStage(self.orchestrator), priority=10
        )
