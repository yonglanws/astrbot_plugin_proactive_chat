"""GATE 阶段默认功能：私聊准入、未回复上限与互动强度概率判定。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult, TriggerSource
from ..pipeline.stage_protocol import BaseStage


class GatingStage(BaseStage):
    """准入校验阶段。"""

    name = "gating_stage"
    hook = HookPoint.GATE
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """执行准入校验：免打扰 / 会话禁用 / 未回复上限。

        不满足条件时按原因记录日志并中止流水线（RESCHEDULE 或 STOP）。
        """
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id

        is_allowed, block_reason = await self.orchestrator.is_chat_allowed(session_id)
        if not is_allowed:
            # 免打扰、会话禁用、配置缺失等应重新调度；其余原因统一提示。
            if block_reason == "not_private":
                logger.info(
                    f"[主动消息] {session_id} 不是私聊会话，跳过并终止喵。"
                )
                ctx.abort(block_reason)
                return StageResult.STOP
            if block_reason == "quiet_hours":
                logger.info("[主动消息] 当前为免打扰时段，跳过并重新调度喵。")
            elif block_reason == "session_disabled":
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 已被禁用，跳过并重新调度喵。"
                )
            elif block_reason == "session_config_missing":
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 未命中有效会话配置，跳过并重新调度喵。"
                )
            else:
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 当前不满足触发条件（原因: {block_reason}），跳过并重新调度喵。"
                )
            ctx.abort(block_reason)
            return StageResult.RESCHEDULE

        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            # 配置缺失：终止本次流程，不再重调度。
            ctx.abort("session_config_missing")
            return StageResult.STOP

        # 已暂停的会话不再发送任何主动消息；手动触发也不例外。
        # 只有用户发来新消息（event_handlers 清除暂停）或管理端显式恢复后才会继续。
        if plugin.scheduler_service.is_session_paused(session_id):
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                f"处于未回复暂停中，等待用户重新开启话题，跳过本次触发喵。"
            )
            ctx.abort("proactive_paused")
            return StageResult.STOP

        schedule_conf = session_config.get("schedule_settings", {})
        # 负反馈冷却：用户明确说过“别发了/在忙”后，冷却期内不主动。
        in_cooldown = getattr(plugin.scheduler_service, "is_in_cooldown", None)
        if callable(in_cooldown) and in_cooldown(session_id):
            remaining_fn = getattr(
                plugin.scheduler_service, "cooldown_remaining", None
            )
            remaining = (
                int(remaining_fn(session_id)) if callable(remaining_fn) else 3600
            )
            ctx.extra["retry_after_seconds"] = max(60, remaining)
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                f"处于负反馈冷却中，约 {max(1, remaining // 60)} 分钟后才会再次尝试喵。"
            )
            ctx.abort("negative_cooldown")
            return StageResult.RESCHEDULE

        # 在锁内读取未回复计数并与上限比较。
        async with plugin.data_lock:
            unanswered_count = plugin.session_data.get(session_id, {}).get(
                "unanswered_count", 0
            )
            max_unanswered = schedule_conf.get("max_unanswered_times", 4)
            if max_unanswered > 0 and unanswered_count >= max_unanswered:
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                    f"的未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，"
                    f"临时停用该会话的主动消息，直到用户再次发来消息喵。"
                )
                await plugin.scheduler_service.pause_session(
                    session_id,
                    threshold=max_unanswered,
                    unanswered_count=unanswered_count,
                )
                ctx.abort("proactive_paused")
                return StageResult.STOP

        ctx.unanswered_count = unanswered_count

        # 作息节律：非手动触发时，避免在对方不活跃或今日预算已用完时发送。
        rhythm_service = getattr(plugin, "rhythm_service", None)
        if rhythm_service is not None and ctx.trigger is not TriggerSource.MANUAL:
            allowed, rhythm_reason, delay = rhythm_service.can_send_now(
                session_id,
                now=time.time(),
                schedule_conf=schedule_conf,
                session_config=session_config,
            )
            ctx.extra["rhythm"] = rhythm_service.snapshot(
                session_id, session_config=session_config
            )
            if not allowed:
                retry_after = int(max(60, delay))
                ctx.extra["retry_after_seconds"] = retry_after
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                    f"当前不在适合发送的作息窗口（{rhythm_reason}），"
                    f"约 {max(1, retry_after // 60)} 分钟后重试喵。"
                )
                ctx.abort(f"rhythm_{rhythm_reason}")
                return StageResult.RESCHEDULE

        # 人格精力：低精力时降低本轮主动概率，但手动触发不受影响。
        probability_multiplier = 1.0
        if rhythm_service is not None and ctx.trigger is not TriggerSource.MANUAL:
            try:
                probability_multiplier *= (
                    rhythm_service.circadian_probability_multiplier(
                        session_id,
                        now=time.time(),
                        session_config=session_config,
                    )
                )
            except Exception as e:
                logger.debug(f"[主动消息] 计算昼夜概率系数失败喵: {e}")

        persona_service = getattr(plugin, "persona_state_service", None)
        if persona_service is not None and ctx.trigger is not TriggerSource.MANUAL:
            try:
                persona_snapshot = persona_service.snapshot(
                    session_id, session_config=session_config
                )
                ctx.extra["persona"] = persona_snapshot
                probability_multiplier *= float(
                    persona_snapshot.get("probability_multiplier", 1.0) or 1.0
                )
            except Exception as e:
                logger.debug(f"[主动消息] 读取人格状态失败喵: {e}")

        # 互动强度概率判定：手动触发永远放行；自动触发可按配置关闭强度修正。
        intensity_service = getattr(plugin, "intensity_service", None)
        if intensity_service is not None and ctx.trigger is not TriggerSource.MANUAL:
            intensity_settings = intensity_service.get_settings(session_config)
            eligible = intensity_settings.enabled and (
                ctx.trigger is not TriggerSource.AUTO_TRIGGER
                or intensity_settings.apply_to_auto_trigger
            )
            if eligible:
                will_send, snapshot = intensity_service.decide(
                    session_id,
                    session_config,
                    trigger=ctx.trigger.value,
                    probability_multiplier=probability_multiplier,
                )
                ctx.extra["intensity"] = snapshot
                if not will_send:
                    retry_after = snapshot.get("retry_after_seconds") or 300
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                        f"互动强度 {snapshot['score']}/100（{snapshot['tier_label']}），"
                        f"触发概率 {snapshot['probability_percent']}%，本次随机值 "
                        f"{snapshot.get('roll')} 未命中，将在约 "
                        f"{max(1, int(retry_after) // 60)} 分钟后重新判定喵。"
                    )
                    ctx.extra["retry_after_seconds"] = retry_after
                    ctx.abort("intensity_probability_skip")
                    return StageResult.RESCHEDULE
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} "
                    f"互动强度 {snapshot['score']}/100（{snapshot['tier_label']}），"
                    f"触发概率 {snapshot['probability_percent']}%，本次命中，继续生成喵。"
                )

        logger.info(
            f"[主动消息] 开始生成第 {unanswered_count + 1} 次主动消息喵，当前未回复次数: {unanswered_count} 次喵。"
        )
        # 遥测启用时上报“主动任务已开始”。
        if plugin.telemetry and plugin.telemetry.enabled:
            plugin._track_task(
                asyncio.create_task(
                    plugin.telemetry.track_feature(
                        "proactive_task_started",
                        {
                            "session_type": session_config.get(
                                "_session_type", "unknown"
                            ),
                            "unanswered_count": unanswered_count,
                        },
                    )
                )
            )
        return StageResult.CONTINUE


class GatingFeature:
    """GATE 阶段功能。"""

    name = "gating_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.GATE, stage=GatingStage(self.orchestrator), priority=10
        )
