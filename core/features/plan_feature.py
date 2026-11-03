"""PLAN 阶段：可选的 planner 多阶段生成。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage


class PlanStage(BaseStage):
    """在写作前生成一轮简短计划。"""

    name = "plan_stage"
    hook = HookPoint.PLAN
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_config = plugin.config_service.get_session_config(ctx.session_id) or {}
        planner = getattr(plugin, "planner_service", None)
        if planner is None:
            return StageResult.CONTINUE

        context_text = build_generation_context(ctx)
        try:
            plan = await planner.plan(
                ctx.session_id,
                context_text=context_text,
                session_config=session_config,
            )
        except Exception as e:
            logger.warning(f"[主动消息] planner 阶段异常，已降级喵: {e}")
            plan = None

        if plan:
            ctx.extra["plan"] = plan
            humanization = ctx.extra.get("humanization")
            if not isinstance(humanization, dict):
                humanization = {}
            humanization["plan"] = plan
            ctx.extra["humanization"] = humanization
            logger.info(
                f"[主动消息] 已注入 planner 计划：{plan.get('topic') or plan.get('goal') or ''}喵。"
            )
        return StageResult.CONTINUE


class PlanFeature:
    """PLAN 阶段功能。"""

    name = "plan_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.PLAN, stage=PlanStage(self.orchestrator), priority=10
        )


def build_generation_context(ctx: Any) -> str:
    """把关系状态、意图、记忆、话题账本与最近历史压成规划上下文。"""
    extra = ctx.extra or {}
    humanization = extra.get("humanization") or {}
    lines: list[str] = []
    intent_label = humanization.get("intent_label")
    if intent_label:
        lines.append(f"本轮意图：{intent_label}")
    brief = humanization.get("intent_brief")
    if brief:
        lines.append(f"意图说明：{brief}")
    state = humanization.get("conversation_state") or {}
    if state.get("state_label"):
        lines.append(f"关系状态：{state['state_label']}")
    persona = humanization.get("persona") or {}
    if persona.get("enabled"):
        lines.append(
            f"当前状态：{persona.get('mood_label', '平静')}，"
            f"精力 {persona.get('energy_percent', 60)}%，"
            f"{persona.get('activity', '日常')}"
        )
    rhythm = humanization.get("rhythm") or {}
    if rhythm.get("active_hours_label"):
        lines.append(f"对方活跃时段：{rhythm['active_hours_label']}")
    memory_context = str(extra.get("memory_context") or "").strip()
    if memory_context:
        lines.append(f"长期记忆：\n{memory_context[:1200]}")
    topic_context = str(extra.get("topic_context") or "").strip()
    if topic_context:
        lines.append(f"话题账本：\n{topic_context[:1200]}")
    history = extra.get("history") or []
    recent = []
    for item in list(history)[-6:]:
        if isinstance(item, dict):
            role = item.get("role", "unknown")
            content = str(item.get("content") or "").strip()
        else:
            role = getattr(item, "role", "unknown")
            content = str(getattr(item, "content", "") or "").strip()
        if content:
            recent.append(f"{role}: {content[:160]}")
    if recent:
        lines.append("最近对话：\n" + "\n".join(recent))
    return "\n".join(lines)
