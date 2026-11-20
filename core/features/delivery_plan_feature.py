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
        extra = ctx.extra or {}

        # 到这里 GUARD / CRITIC 都已通过。此时才派发 OnLLMResponseEvent：
        # TTS 等插件常在该钩子里直接发语音，提前派发会导致文本被拦截、
        # 语音却已经发出去。放在发送计划之前，钩子改写后的文本仍会参与分气泡。
        llm_response = extra.get("llm_response")
        if llm_response is not None:
            # 守卫/Critic 可能已改写 extra["response_text"]；先同步回响应对象，
            # 否则 TTS 插件在 OnLLMResponseEvent 里仍会朗读被拦截前的原文。
            current_text = str(extra.get("response_text") or "")
            if current_text:
                try:
                    llm_response.completion_text = current_text
                except Exception as e:
                    logger.debug(f"[主动消息] 同步改写后的响应文本失败喵: {e}")
            raw_chain_before = list(
                getattr(getattr(llm_response, "result_chain", None), "chain", []) or []
            )
            # 记录钩子派发前（守卫/Critic 改写后）的原文：LivingMemory 的
            # on_llm_response 会按这份文本落库，后续兜底写入要用它去重。
            hook_original_text = str(
                getattr(llm_response, "completion_text", "") or ""
            )
            try:
                stopped = await plugin.llm_service.dispatch_llm_response_hooks(
                    ctx.event, llm_response
                )
            except Exception as e:
                logger.error(f"[主动消息] 派发 LLM 后置钩子失败喵: {e}")
                stopped = False
            # 标记本轮已派发 on_llm_response：记忆插件已按原文写入，
            # 收尾阶段不应再用气泡拼接文本调用 store_proactive 写第二次。
            extra["llm_response_hooks_dispatched"] = True
            extra["llm_response_original_text"] = hook_original_text
            if stopped:
                logger.info(
                    "[主动消息] LLM 后置钩子终止了事件传播，放弃本次生成结果喵。"
                )
                ctx.abort("llm_response_stopped")
                return StageResult.STOP
            updated_text = plugin.llm_service.extract_response_text(llm_response)
            updated_chain = plugin.llm_service.extract_response_chain(llm_response)
            if updated_text and (not current_text or updated_text != current_text):
                extra["response_text"] = updated_text
            if updated_chain and (
                not extra.get("result_chain") or updated_chain != raw_chain_before
            ):
                extra["result_chain"] = updated_chain

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
