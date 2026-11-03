"""GUARD 阶段：发送前的重复、长度、语气与自然度守卫。"""

from __future__ import annotations

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage


class GuardStage(BaseStage):
    """自然度守卫阶段。"""

    name = "guard_stage"
    hook = HookPoint.GUARD
    priority = 10

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id
        extra = ctx.extra or {}
        session_config = plugin.config_service.get_session_config(session_id) or {}
        text = str(extra.get("response_text") or "")
        chain = extra.get("result_chain") or []

        # 纯富媒体（图片/语音等）没有可校验的文本，直接放行。
        if not text.strip() and any(
            type(comp).__name__ != "Plain" for comp in chain
        ):
            extra["guard"] = {
                "enabled": True,
                "passed": True,
                "skipped": "rich_chain",
            }
            return StageResult.CONTINUE

        result = await plugin.guard_service.review(
            session_id,
            text,
            session_config=session_config,
            conversation=extra.get("conversation"),
        )
        extra["guard"] = result.get("guard", {})
        extra["guard_reasons"] = result.get("reasons", [])

        if not result.get("allowed"):
            reasons = ", ".join(result.get("reasons", [])) or "unknown"
            logger.info(
                f"[主动消息] 自然度守卫拦截本次主动消息（{reasons}），"
                f"已放弃并重新调度喵。"
            )
            try:
                payload = plugin.session_data.setdefault(session_id, {})
                payload["last_guard"] = {
                    "at": ctx.started_at,
                    "allowed": False,
                    "reasons": result.get("reasons", []),
                }
                await plugin.storage_service.save_data()
            except Exception as e:
                logger.debug(f"[主动消息] 保存自然度守卫结果失败喵: {e}")
            extra["retry_after_seconds"] = 900
            ctx.abort("guard_blocked")
            return StageResult.RESCHEDULE

        if result.get("rewritten"):
            rewritten = str(result.get("text") or "").strip()
            non_plain = [
                comp
                for comp in chain
                if type(comp).__name__ != "Plain"
            ]
            extra["response_text"] = rewritten
            # 保留图片/语音等富媒体组件，只替换文本部分。
            extra["result_chain"] = non_plain
            logger.info("[主动消息] 自然度守卫已替换为改写后的文本喵。")
        else:
            extra["response_text"] = str(result.get("text") or text)

        try:
            payload = plugin.session_data.setdefault(session_id, {})
            payload["last_guard"] = {
                "at": ctx.started_at,
                "allowed": True,
                **result.get("guard", {}),
            }
        except Exception:
            pass
        return StageResult.CONTINUE


class GuardFeature:
    """GUARD 阶段功能。"""

    name = "guard_feature"

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.GUARD, stage=GuardStage(self.orchestrator), priority=10
        )
