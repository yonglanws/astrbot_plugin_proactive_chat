"""DELIVER 阶段默认功能：发送主动消息（含装饰/分段/TTS）。"""

from __future__ import annotations

import inspect
from typing import Any

from astrbot.api import logger

from ...domain.delivery_report import DeliveryReport
from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage


class DeliverStage(BaseStage):
    """消息投递阶段。"""

    name = "deliver_stage"
    hook = HookPoint.DELIVER
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """投递主动消息；发送失败时不存档、不计数，直接重调度。"""
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id
        # extra 承载上游阶段产出的待发送内容。
        extra = getattr(ctx, "extra", {}) or {}
        started_at = float(getattr(ctx, "started_at", 0) or 0)
        start_last_msg = float((getattr(ctx, "start_state", {}) or {}).get("last_message_time", 0) or 0)

        def should_abort_send() -> bool:
            """用户已回复、会话被指令关闭，或插件正在终止时不再发送。"""
            if getattr(plugin, "_terminating", False):
                return True
            current_config = plugin.config_service.get_session_config(session_id)
            if not current_config or not current_config.get("enable", False):
                return True
            latest_msg = float(plugin.last_message_times.get(session_id, 0) or 0)
            if started_at and latest_msg > started_at:
                return True
            return bool(start_last_msg) and latest_msg > start_last_msg

        report = DeliveryReport()
        send_fn = plugin.sender_service.send_proactive_message
        send_kwargs: dict[str, Any] = {
            "event": ctx.event,
            "initial_chain": extra.get("result_chain"),
            "delivery_plan": extra.get("delivery_plan"),
            "abort_check": should_abort_send,
        }
        try:
            sig = inspect.signature(send_fn)
            if "report_out" in sig.parameters or any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            ):
                send_kwargs["report_out"] = report
        except (TypeError, ValueError):
            pass

        sent_ok = (
            await send_fn(
                session_id,
                extra.get("response_text", ""),
                **send_kwargs,
            )
            is True
        )
        if not report.any_sent and sent_ok:
            report.sent = True
        ctx.delivery = report
        ctx.extra["delivery_report"] = report
        if report.delivered_text:
            ctx.extra["delivered_text"] = report.delivered_text
        elif sent_ok:
            ctx.extra["delivered_text"] = str(extra.get("response_text") or "")

        if not sent_ok:
            if should_abort_send():
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 在发送窗口内出现用户新消息，已放弃本次主动消息喵。"
                )
                ctx.abort("new_message_before_send")
                return StageResult.STOP
            logger.warning(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的本次主动消息未能送达，已跳过存档与计数并重新调度喵。"
            )
            ctx.abort("send_failed")
            return StageResult.RESCHEDULE
        return StageResult.CONTINUE


class SplitDeliveryFeature:
    """DELIVER 阶段功能（默认发送行为）。"""

    name = "split_delivery_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIVER, stage=DeliverStage(self.orchestrator), priority=10
        )
