"""主动消息流水线编排器。

由旧 core/chat_flow.py 的 check_and_chat 改编而来：骨架不变，
具体步骤交由各 Feature 注册的 Stage 执行。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ...utils.time_utils import is_quiet_time
from ..adapters.astrbot_conversation import (
    add_assistant_message,
    verify_message_persisted,
)
from ..domain.domain_enums import HookPoint, StageResult, TriggerSource
from ..domain.request_context import ProactiveContext
from ..domain.session_key import is_friend_type
from ..features.feature_manifest import build_default_features
from .feature_registry import FeatureRegistry
from .flow_context import build_context
from .pipeline_runner import PipelineRunner


def _safe_non_negative_int(value: Any, default: int) -> int:
    """安全将配置或计数转为非负整数。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


class FlowOrchestrator:
    """组织并执行主动消息流水线。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._runner: PipelineRunner | None = None
        self._active_manual_owners: dict[str, object] = {}

    def invalidate_runner(self) -> None:
        """重置缓存的流水线实例，使最新配置在下次运行时重新装配。"""
        self._runner = None

    # ------------------------------------------------------------------
    # 流水线装配
    # ------------------------------------------------------------------
    def build_pipeline(self) -> PipelineRunner:
        """构建流水线（装配 Feature 并注入结束行为）。"""
        registry = FeatureRegistry(config=dict(self.plugin.config))
        registry.register_features(build_default_features(self))

        runner = PipelineRunner(registry.hooks)
        runner.set_stop_handler(HookPoint.GATE, self._on_gate_stop)
        runner.set_reschedule_handler(self._on_reschedule)
        runner.set_abort_handler(self._on_abort)
        self._runner = runner
        return runner

    async def _on_gate_stop(self, ctx: ProactiveContext) -> None:
        """GATE 阶段正常终止（未回复上限/无配置）：不重调度。"""
        return None

    async def _on_reschedule(self, ctx: ProactiveContext, reset: bool) -> None:
        """需要重新调度下一次主动消息。

        互动强度概率未命中时，GATE 会把一个较短的 ``retry_after_seconds``
        放进 ctx.extra；此时使用短重试窗口，让高热度用户更快获得下一次机会。
        """
        retry_after = ctx.extra.get("retry_after_seconds")
        await self.plugin.scheduler_service.schedule_next_chat_and_save(
            ctx.session_id,
            forced_interval_seconds=retry_after if retry_after else None,
        )

    async def _on_abort(self, ctx: ProactiveContext, error: Exception | None) -> None:
        """阶段异常时的兜底：清理调度痕迹并尝试重调度。

        流程：记录错误 -> 清理残留调度状态 -> 重新排期 -> 上报遥测。
        """
        plugin = self.plugin
        session_id = ctx.session_id
        if error is not None:
            logger.error("[主动消息] check_and_chat 任务发生致命错误喵:")
            logger.error(f"[主动消息] 错误类型喵: {type(error).__name__}")
            logger.error(f"[主动消息] 错误信息喵: {error}")

        try:
            async with plugin.data_lock:
                if plugin.scheduler_service.clear_session_schedule_state(session_id):
                    await plugin.storage_service.save_data()
        except Exception as clean_e:
            logger.debug(f"[主动消息] 清理失败任务数据时出错喵: {clean_e}")

        try:
            normalized_session_id = plugin.session_service.normalize_session_id(
                session_id
            )
            parsed = plugin.session_service.parse_session_id(normalized_session_id)
            if parsed and not is_friend_type(parsed[1]):
                logger.info(
                    f"[主动消息] {normalized_session_id} 不是私聊会话，跳过错误恢复重调度喵。"
                )
                return

            session_config = plugin.config_service.get_session_config(
                normalized_session_id
            )
            if not session_config or not session_config.get("enable", False):
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} "
                    f"已禁用或无有效配置，跳过错误恢复重调度喵。"
                )
                return

            if plugin.scheduler_service.is_session_paused(normalized_session_id):
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} "
                    f"处于暂停状态，跳过错误恢复重调度喵。"
                )
                return

            schedule_conf = session_config.get("schedule_settings", {})
            max_unanswered = _safe_non_negative_int(
                schedule_conf.get("max_unanswered_times", 4), 4
            )
            unanswered_count = _safe_non_negative_int(
                plugin.session_data.get(normalized_session_id, {}).get(
                    "unanswered_count", 0
                ),
                0,
            )
            if max_unanswered > 0 and unanswered_count >= max_unanswered:
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} "
                    f"未回复次数 ({unanswered_count}) 已达上限 ({max_unanswered})，转为暂停状态喵。"
                )
                async with plugin.data_lock:
                    await plugin.scheduler_service.pause_session(
                        normalized_session_id,
                        threshold=max_unanswered,
                        unanswered_count=unanswered_count,
                    )
                return

            logger.info(
                f"[主动消息] 尝试重新调度 {plugin.session_service.get_session_log_str(normalized_session_id)} 的主动消息任务喵。"
            )
            await plugin.scheduler_service.schedule_next_chat_and_save(
                normalized_session_id
            )
        except Exception as se:
            logger.error(f"[主动消息] 在错误处理中重新调度失败喵: {se}")

        if error is not None and plugin.telemetry and plugin.telemetry.enabled:
            plugin._track_task(
                asyncio.create_task(
                    plugin.telemetry.track_error(
                        error, module="core.chat_flow.check_and_chat"
                    )
                )
            )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def run(
        self, session_id: str, trigger: TriggerSource = TriggerSource.SCHEDULE
    ) -> None:
        """执行一次完整的主动消息流程。

        构建上下文并交由流水线执行；发生异常时走 _on_abort 兜底；
        无论成败都会释放手动触发占用状态。
        """
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        if getattr(plugin, "_terminating", False):
            logger.debug("[主动消息] 插件正在终止，跳过本次 check_and_chat 喵。")
            if (
                trigger is TriggerSource.MANUAL
                and normalized_session_id not in self._active_manual_owners
            ):
                await self.clear_manual_trigger_state(normalized_session_id)
            return

        active_sessions = getattr(plugin, "active_trigger_sessions", None)
        if not isinstance(active_sessions, set):
            active_sessions = set()
            plugin.active_trigger_sessions = active_sessions

        # 同一会话同一时刻只允许一条主动消息流水线：定时任务、自动触发
        # 与 Web“立即触发”共享同一把执行占用锁。
        if normalized_session_id in active_sessions:
            logger.info(
                f"[主动消息] {session_id} 已有主动消息正在执行，跳过本次 {trigger.value} 触发喵。"
            )
            # 仅当当前没有真正在跑的 MANUAL 占用者时才清理，避免误清正在执行中的手动触发状态。
            if (
                trigger is TriggerSource.MANUAL
                and normalized_session_id not in self._active_manual_owners
            ):
                await self.clear_manual_trigger_state(normalized_session_id)
            return
        if (
            trigger is not TriggerSource.MANUAL
            and normalized_session_id in plugin.manual_trigger_sessions
        ):
            logger.info(
                f"[主动消息] {session_id} 正在执行手动触发，跳过本次 {trigger.value} 触发喵。"
            )
            return
        active_sessions.add(normalized_session_id)
        owner_token: object | None = None
        if trigger is TriggerSource.MANUAL:
            owner_token = object()
            self._active_manual_owners[normalized_session_id] = owner_token
        try:
            await self._run_locked(normalized_session_id, trigger)
        finally:
            active_sessions.discard(normalized_session_id)
            if owner_token is not None:
                if self._active_manual_owners.get(normalized_session_id) is owner_token:
                    self._active_manual_owners.pop(normalized_session_id, None)
                    await self.clear_manual_trigger_state(normalized_session_id)
            elif normalized_session_id not in self._active_manual_owners:
                await self.clear_manual_trigger_state(normalized_session_id)

    async def _run_locked(
        self, normalized_session_id: str, trigger: TriggerSource
    ) -> None:
        """在已持有执行占用的情况下运行流水线。"""
        plugin = self.plugin
        # 全链路私聊守卫：任何来源（定时器 / 自动触发 / Web 手动）都不能
        # 为群聊会话生成主动消息。
        parsed = plugin.session_service.parse_session_id(normalized_session_id)
        if parsed and not is_friend_type(parsed[1]):
            logger.warning(
                f"[主动消息] 拒绝为非私聊会话执行主动消息喵: {normalized_session_id}"
            )
            return

        try:
            session_config = plugin.config_service.get_session_config(
                normalized_session_id
            )
            ctx = build_context(
                normalized_session_id,
                plugin.session_service.build_session_key(normalized_session_id),
                trigger,
                session_config,
                plugin.session_data.get(normalized_session_id, {}).get(
                    "unanswered_count", 0
                ),
            )
            # 在流水线最开始记录基线，确保 GATE -> CONTEXT 之间到达的用户消息
            # 也能被 REVIEW / 发送前检查识别，而不是被后来才拍的快照当成基线。
            ctx.start_state = {
                "last_message_time": plugin.last_message_times.get(
                    normalized_session_id, 0
                ),
                "unanswered_count": ctx.unanswered_count,
                "timestamp": ctx.started_at,
            }

            runner = self._runner or self.build_pipeline()
            await runner.run(ctx)
        except Exception as e:
            await self._on_abort(
                build_context(
                    normalized_session_id,
                    plugin.session_service.build_session_key(normalized_session_id),
                    trigger,
                    None,
                ),
                e,
            )

    # ------------------------------------------------------------------
    # 准入与收尾（供 Feature 复用）
    # ------------------------------------------------------------------
    async def is_chat_allowed(
        self,
        session_id: str,
        trigger: TriggerSource = TriggerSource.SCHEDULE,
    ) -> tuple[bool, str]:
        """检查是否允许进行主动聊天，并返回阻断原因。

        阻断原因：not_private / session_config_missing / session_disabled /
        quiet_hours。allowed 表示通过校验。

        手动触发（Web 端“立即触发”）绕过免打扰时段，否则夜间点击按钮会
        静默无响应；会话禁用、暂停与未回复上限仍然生效。
        """
        plugin = self.plugin
        parsed = plugin.session_service.parse_session_id(session_id)
        if parsed and not is_friend_type(parsed[1]):
            return False, "not_private"

        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            return False, "session_config_missing"
        if not session_config.get("enable", False):
            return False, "session_disabled"

        schedule_conf = session_config.get("schedule_settings", {})
        if trigger is not TriggerSource.MANUAL and is_quiet_time(
            schedule_conf.get("quiet_hours", "1-7"), plugin.timezone
        ):
            return False, "quiet_hours"

        return True, "allowed"

    async def finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        assistant_response: str,
        unanswered_count: int,
        intent: str | None = None,
        started_at: float | None = None,
        *,
        llm_response_dispatched: bool = False,
        llm_original_text: str | None = None,
    ) -> None:
        """主动消息任务完成后的收尾工作。

        步骤：存档对话历史并回读校验 -> 递增未回复计数 ->
        记录本次主动消息（用于回复率统计） -> 计算强度修正后的下一次调度。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            logger.info("[主动消息] 插件正在终止，跳过本次主动消息的收尾与重调度喵。")
            return

        normalized_session_id = plugin.session_service.normalize_session_id(session_id)

        if not (assistant_response or "").strip():
            logger.debug("[主动消息] 本次结果无文本内容，跳过对话历史存档喵。")
        else:
            try:
                # 只存档 assistant 主动消息，不再把完整主动提示词伪装成
                # user 消息写进对话历史（避免污染上下文与浪费 token）。
                await add_assistant_message(
                    plugin.context.conversation_manager,
                    conv_id,
                    assistant_response,
                )
                if await verify_message_persisted(
                    plugin.context.conversation_manager,
                    normalized_session_id,
                    conv_id,
                    assistant_response,
                ):
                    logger.info("[主动消息] 已成功将本次主动消息存档至对话历史喵。")
                else:
                    logger.warning(
                        "[主动消息] 本次主动消息已提交存档，但回读校验未命中末尾记录喵，请检查对话历史持久化是否正常喵。"
                    )
            except Exception as e:
                logger.error(f"[主动消息] 存档对话历史失败喵: {e}")
                logger.warning("[主动消息] 对话存档失败喵，但会继续执行后续步骤喵。")

        parsed = plugin.session_service.parse_session_id(normalized_session_id)
        if parsed and not is_friend_type(parsed[1]):
            logger.warning(
                f"[主动消息] 非私聊会话，跳过收尾与重调度喵: {normalized_session_id}"
            )
            return

        session_config = plugin.config_service.get_session_config(normalized_session_id)
        if not session_config:
            return

        scheduled_job_payload = None
        skipped_by_user_reply = False
        paused_or_capped = False

        # 递增未回复计数、记录主动消息、计算并写入下一次调度。
        async with plugin.data_lock:
            # 发送完成到进入收尾之间仍可能收到用户消息；锁内再确认一次，
            # 避免把“已经回复”误记成一次未回复，并与 event_handlers 的
            # 新排期互相覆盖。
            if started_at is not None and (
                plugin.last_message_times.get(normalized_session_id, 0)
                > float(started_at)
            ):
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} "
                    f"在发送后检测到用户新消息，跳过未回复计数与重排，交由消息事件处理喵。"
                )
                skipped_by_user_reply = True
            else:
                new_unanswered_count = _safe_non_negative_int(unanswered_count, 0) + 1
                plugin.session_data.setdefault(normalized_session_id, {})[
                    "unanswered_count"
                ] = new_unanswered_count
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} 的第 {new_unanswered_count} 次主动消息已发送完成，当前未回复次数: {new_unanswered_count} 次喵。"
                )

                try:
                    decision_score = (
                        plugin.intensity_service.get_state(normalized_session_id)
                        .get("last_decision", {})
                        .get("score")
                    )
                    plugin.intensity_service.record_proactive_sent(
                        normalized_session_id,
                        settings=plugin.intensity_service.get_settings(session_config),
                        score=decision_score,
                    )
                except Exception as e:
                    logger.debug(f"[主动消息] 记录主动消息强度事件失败喵: {e}")

                # 作息、关系状态、人格状态与自然度守卫共享同一次发送记录。
                try:
                    rhythm_service = getattr(plugin, "rhythm_service", None)
                    if rhythm_service is not None:
                        rhythm_service.record_proactive_sent(normalized_session_id)
                    state_service = getattr(plugin, "conversation_state_service", None)
                    if state_service is not None:
                        state_service.record_proactive_sent(
                            normalized_session_id,
                            intent or "continue_topic",
                        )
                    conversation_memory = getattr(
                        plugin, "conversation_memory_service", None
                    )
                    if conversation_memory is not None:
                        conversation_memory.record_proactive(
                            normalized_session_id,
                            assistant_response,
                            intent or "continue_topic",
                            session_config=session_config,
                        )
                    persona_service = getattr(plugin, "persona_state_service", None)
                    if persona_service is not None:
                        persona_service.record_proactive_sent(
                            normalized_session_id,
                            session_config=session_config,
                        )
                    guard_service = getattr(plugin, "guard_service", None)
                    if guard_service is not None and (assistant_response or "").strip():
                        guard_service.record_sent_text(
                            normalized_session_id, assistant_response
                        )
                except Exception as e:
                    logger.debug(f"[主动消息] 记录拟人化状态失败喵: {e}")

                schedule_conf = session_config.get("schedule_settings", {})
                max_unanswered = _safe_non_negative_int(
                    schedule_conf.get("max_unanswered_times", 4), 4
                )

                if plugin.scheduler_service.is_session_paused(normalized_session_id):
                    plugin.scheduler_service.clear_session_schedule_state(
                        normalized_session_id
                    )
                    await plugin.storage_service.save_data()
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} 已处于未回复暂停中，本次发送后不再安排下一次主动消息喵。"
                    )
                    paused_or_capped = True
                elif max_unanswered > 0 and new_unanswered_count >= max_unanswered:
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id, session_config)} "
                        f"已连续 {new_unanswered_count} 次未获回复（阈值 {max_unanswered}），发送完成后立即暂停，不再安排下一次主动消息喵。"
                    )
                    await plugin.scheduler_service.pause_session(
                        normalized_session_id,
                        threshold=max_unanswered,
                        unanswered_count=new_unanswered_count,
                    )
                    paused_or_capped = True
                else:
                    (
                        run_date,
                        scheduled_at,
                        min_interval,
                        max_interval,
                        random_interval,
                        next_trigger_time,
                        intensity_score,
                    ) = plugin.scheduler_service.compute_next_trigger(
                        schedule_conf, session_id=normalized_session_id
                    )

                    session_payload = plugin.session_data.setdefault(
                        normalized_session_id, {}
                    )
                    session_payload.pop("schedule_cancelled", None)
                    session_payload["next_trigger_time"] = next_trigger_time
                    session_payload["last_scheduled_at"] = scheduled_at
                    session_payload["last_schedule_min_interval_seconds"] = min_interval
                    session_payload["last_schedule_max_interval_seconds"] = max_interval
                    session_payload["last_schedule_random_interval_seconds"] = (
                        random_interval
                    )
                    if intensity_score is not None:
                        session_payload["last_schedule_intensity_score"] = intensity_score
                    scheduled_job_payload = {
                        "run_date": run_date,
                        "session_config": session_config,
                    }

                    # 在同一个 data_lock 内注册任务，避免用户消息刚排好的新任务
                    # 被这里的 purge_related_jobs 清掉。
                    plugin.scheduler_service.add_chat_job(
                        normalized_session_id, scheduled_job_payload["run_date"]
                    )

                    await plugin.storage_service.save_data()

        if skipped_by_user_reply:
            return

        # 主动消息的 on_llm_response 已在 DELIVER 阶段显式派发：记忆插件
        # 会按 completion_text 原文落库。这里先确认末尾是否已经是这段原文，
        # 已写入就不再重复写；确实未写入时才用原文兜底（而不是使用改写后的
        # 气泡拼接文本），保证一条消息只走一条写入路径。
        memory_bridge = getattr(plugin, "memory_bridge", None)
        if memory_bridge is not None and (assistant_response or "").strip():
            original_text = str(llm_original_text or "").strip()
            memory_text = original_text or assistant_response
            already_written = False
            if llm_response_dispatched and original_text:
                checker = getattr(memory_bridge, "last_message_is", None)
                if callable(checker):
                    try:
                        already_written = await checker(
                            normalized_session_id, original_text
                        )
                    except Exception as e:
                        logger.debug(
                            f"[主动消息] 确认长期记忆写入状态失败喵: {e}"
                        )
                        already_written = False
            if already_written:
                logger.debug(
                    "[主动消息] on_llm_response 已写入长期记忆，跳过重复写入喵。"
                )
            else:
                try:
                    await memory_bridge.store_proactive(
                        normalized_session_id,
                        memory_text,
                        session_config=session_config,
                    )
                except Exception as e:
                    logger.debug(f"[主动消息] 写回长期记忆失败喵: {e}")

        if paused_or_capped or scheduled_job_payload is None:
            return

        logger.info(
            f"[主动消息] 已为 {plugin.session_service.get_session_log_str(normalized_session_id, scheduled_job_payload['session_config'])} 安排下一次主动消息喵，时间：{scheduled_job_payload['run_date'].strftime('%Y-%m-%d %H:%M:%S')} 喵。"
        )

    async def clear_manual_trigger_state(self, session_id: str) -> None:
        """释放指定会话的手动触发占用状态，并向管理端广播任务刷新。"""
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        if normalized_session_id not in plugin.manual_trigger_sessions:
            return

        plugin.manual_trigger_sessions.discard(normalized_session_id)
        if plugin.web_admin_server:
            try:
                await plugin.web_admin_server._broadcast_update("jobs")
            except Exception as e:
                logger.debug(f"[主动消息] 广播手动触发状态更新失败喵: {e}")


__all__ = ["FlowOrchestrator", "StageResult"]
