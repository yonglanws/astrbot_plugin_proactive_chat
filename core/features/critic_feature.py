"""GUARD 阶段第二道审稿：可选的 critic 多阶段生成。"""

from __future__ import annotations

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage
from .guard_feature import _sync_rewritten_llm_response
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
        session_service = getattr(plugin, "session_service", None)
        state_session_id = (
            session_service.normalize_session_id(ctx.session_id)
            if session_service is not None
            and callable(getattr(session_service, "normalize_session_id", None))
            else ctx.session_id
        )
        try:
            critic_record = {
                "at": ctx.started_at,
                "allowed": bool(result.get("allowed")),
                "reason": str(result.get("reason") or "")[:200],
                "rewritten": bool(result.get("rewritten")),
            }
            lock = getattr(plugin, "data_lock", None)
            if lock is not None:
                async with lock:
                    payload = plugin.session_data.setdefault(state_session_id, {})
                    payload["last_critic"] = critic_record
            else:
                payload = plugin.session_data.setdefault(state_session_id, {})
                payload["last_critic"] = critic_record
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
            guard_service = getattr(plugin, "guard_service", None)
            if guard_service is not None and rewritten:
                try:
                    guard_settings = guard_service.get_settings(session_config)
                    if guard_settings.enabled:
                        guard_reasons = guard_service.check_text(
                            state_session_id, rewritten, settings=guard_settings
                        )
                        if guard_reasons:
                            logger.info(
                                f"[主动消息] critic 改写结果未通过自然度守卫（{', '.join(guard_reasons)}），"
                                "已放弃并重新调度喵。"
                            )
                            extra["retry_after_seconds"] = 900
                            ctx.abort("critic_rewrite_guard_blocked")
                            return StageResult.RESCHEDULE
                except Exception as e:
                    logger.debug(f"[主动消息] 复核 critic 改写文本失败喵: {e}")
            non_plain = [
                comp for comp in chain if type(comp).__name__ != "Plain"
            ]
            extra["response_text"] = rewritten
            extra["result_chain"] = non_plain
            _sync_rewritten_llm_response(extra.get("llm_response"), rewritten)
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
