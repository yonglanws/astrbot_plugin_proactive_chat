"""GUARD 阶段第二道审稿：可选的 critic 多阶段生成。"""

from __future__ import annotations

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage
from .plan_feature import build_generation_context


class CriticStage(BaseStage):
    """在规则守卫之后做一次语义级审稿。"""

    name = "critic_stage"
    hook = HookPoint.GUARD
    priority = 20

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        critic = getattr(plugin, "critic_service", None)
        if critic is None:
            return StageResult.CONTINUE

        extra = ctx.extra or {}
        text = str(extra.get("response_text") or "")
        chain = extra.get("result_chain") or []
        if not text.strip() and any(
            type(comp).__name__ != "Plain" for comp in chain
        ):
            return StageResult.CONTINUE

        session_config = plugin.config_service.get_session_config(ctx.session_id) or {}
        try:
            result = await critic.review(
                ctx.session_id,
                text,
                context_text=build_generation_context(ctx),
                session_config=session_config,
            )
        except Exception as e:
            logger.warning(f"[主动消息] critic 阶段异常，按通过处理喵: {e}")
            return StageResult.CONTINUE

        extra["critic"] = result
        try:
            payload = plugin.session_data.setdefault(ctx.session_id, {})
            payload["last_critic"] = {
                "at": ctx.started_at,
                "allowed": bool(result.get("allowed")),
                "reason": str(result.get("reason") or "")[:200],
                "rewritten": bool(result.get("rewritten")),
            }
        except Exception:
            pass

        if not result.get("allowed"):
            logger.info(
                f"[主动消息] critic 拒绝本次消息（{result.get('reason') or 'unknown'}），"
                "已放弃并重新调度喵。"
            )
            extra["retry_after_seconds"] = 900
            ctx.abort("critic_rejected")
            return StageResult.RESCHEDULE

        if result.get("rewritten"):
            rewritten = str(result.get("text") or "").strip()
            non_plain = [
                comp for comp in chain if type(comp).__name__ != "Plain"
            ]
            extra["response_text"] = rewritten
            extra["result_chain"] = non_plain
            logger.info("[主动消息] critic 已替换为改写后的文本喵。")
        return StageResult.CONTINUE


class CriticFeature:
    """GUARD 阶段的 critic 功能。"""

    name = "critic_feature"

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.GUARD, stage=CriticStage(self.orchestrator), priority=20
        )
