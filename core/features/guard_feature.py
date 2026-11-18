"""GUARD 阶段：发送前的重复、长度、语气与自然度守卫。"""

from __future__ import annotations

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage


def _sync_rewritten_llm_response(llm_response, rewritten: str) -> None:
    if llm_response is None:
        return
    try:
        llm_response.completion_text = rewritten
    except Exception:
        pass
    chain_obj = getattr(llm_response, "result_chain", None)
    chain_list = getattr(chain_obj, "chain", None) if chain_obj is not None else None
    if isinstance(chain_list, list):
        non_plain = [c for c in chain_list if type(c).__name__ != "Plain"]
        try:
            from astrbot.core.message.components import Plain

            chain_obj.chain = ([Plain(text=rewritten)] if rewritten else []) + non_plain
        except Exception:
            chain_obj.chain = non_plain


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
                lock = getattr(plugin, "data_lock", None)
                if lock is not None:
                    async with lock:
                        payload = plugin.session_data.setdefault(session_id, {})
                        payload["last_guard"] = {
                            "at": ctx.started_at,
                            "allowed": False,
                            "reasons": result.get("reasons", []),
                        }
                        await plugin.storage_service.save_data()
                else:
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
            # 保留图片/语音等富媒体组件，只替换文本部分，并同步到 llm_response。
            extra["result_chain"] = non_plain
            _sync_rewritten_llm_response(extra.get("llm_response"), rewritten)
            logger.info("[主动消息] 自然度守卫已替换为改写后的文本喵。")
        else:
            extra["response_text"] = str(result.get("text") or text)

        try:
            lock = getattr(plugin, "data_lock", None)
            if lock is not None:
                async with lock:
                    payload = plugin.session_data.setdefault(session_id, {})
                    payload["last_guard"] = {
                        "at": ctx.started_at,
                        "allowed": True,
                        **result.get("guard", {}),
                    }
            else:
                payload = plugin.session_data.setdefault(session_id, {})
                payload["last_guard"] = {
                    "at": ctx.started_at,
                    "allowed": True,
                    **result.get("guard", {}),
                }
        except Exception:
            pass

        # OnLLMResponseEvent 延迟到 DELIVER 阶段（Critic 之后、真正发送前）
        # 派发，避免守卫/Critic 拦截文本时 TTS 插件已经先发出语音。
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
