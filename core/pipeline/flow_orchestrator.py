"""主动消息流水线编排器。

由旧 core/chat_flow.py 的 check_and_chat 改编而来：骨架不变，
具体步骤交由各 Feature 注册的 Stage 执行。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ...utils.time_utils import is_quiet_time
from ..adapters.astrbot_conversation import add_message_pair, verify_message_persisted
from ..domain.domain_enums import HookPoint, StageResult, TriggerSource
from ..domain.request_context import ProactiveContext
from ..domain.session_key import is_friend_type
from ..features.feature_manifest import build_default_features
from .feature_registry import FeatureRegistry
from .flow_context import build_context
from .pipeline_runner import PipelineRunner


class FlowOrchestrator:
    """组织并执行主动消息流水线。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._runner: PipelineRunner | None = None

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
            logger.info(
                f"[主动消息] 尝试重新调度 {plugin.session_service.get_session_log_str(session_id)} 的主动消息任务喵。"
            )
            await plugin.scheduler_service.schedule_next_chat_and_save(session_id)
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
        if getattr(plugin, "_terminating", False):
            logger.debug("[主动消息] 插件正在终止，跳过本次 check_and_chat 喵。")
            return

        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
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
        finally:
            await self.clear_manual_trigger_state(normalized_session_id)

    # ------------------------------------------------------------------
    # 准入与收尾（供 Feature 复用）
    # ------------------------------------------------------------------
    async def is_chat_allowed(self, session_id: str) -> tuple[bool, str]:
        """检查是否允许进行主动聊天，并返回阻断原因。

        阻断原因：not_private / session_config_missing / session_disabled /
        quiet_hours。allowed 表示通过校验。
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
        if is_quiet_time(schedule_conf.get("quiet_hours", "1-7"), plugin.timezone):
            return False, "quiet_hours"

        return True, "allowed"

    async def finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
        intent: str | None = None,
    ) -> None:
        """主动消息任务完成后的收尾工作。

        步骤：存档对话历史并回读校验 -> 递增未回复计数 ->
        记录本次主动消息（用于回复率统计） -> 计算强度修正后的下一次调度。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            logger.info("[主动消息] 插件正在终止，跳过本次主动消息的收尾与重调度喵。")
            return

        if not (assistant_response or "").strip():
            logger.debug("[主动消息] 本次结果无文本内容，跳过对话历史存档喵。")
        else:
            try:
                await add_message_pair(
                    plugin.context.conversation_manager,
                    conv_id,
                    user_prompt,
                    assistant_response,
                )
                if await verify_message_persisted(
                    plugin.context.conversation_manager,
                    session_id,
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

        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
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

        # 递增未回复计数、记录主动消息、计算并写入下一次调度。
        async with plugin.data_lock:
            new_unanswered_count = unanswered_count + 1
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

            if plugin.scheduler_service.is_session_paused(normalized_session_id):
                plugin.scheduler_service.clear_session_schedule_state(
                    normalized_session_id
                )
                await plugin.storage_service.save_data()
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} 已处于未回复暂停中，本次发送后不再安排下一次主动消息喵。"
                )
                return

            schedule_conf = session_config.get("schedule_settings", {})
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
            session_payload["next_trigger_time"] = next_trigger_time
            session_payload["last_scheduled_at"] = scheduled_at
            session_payload["last_schedule_min_interval_seconds"] = min_interval
            session_payload["last_schedule_max_interval_seconds"] = max_interval
            session_payload["last_schedule_random_interval_seconds"] = random_interval
            if intensity_score is not None:
                session_payload["last_schedule_intensity_score"] = intensity_score
            scheduled_job_payload = {
                "run_date": run_date,
                "session_config": session_config,
            }

            await plugin.storage_service.save_data()

        # 主动消息不会经过 AstrBot 的 on_llm_response 钩子，因此需要显式
        # 写回外部长期记忆插件的会话库，保证后续召回能看到这句话。
        memory_bridge = getattr(plugin, "memory_bridge", None)
        if memory_bridge is not None and (assistant_response or "").strip():
            try:
                await memory_bridge.store_proactive(
                    normalized_session_id,
                    assistant_response,
                    session_config=session_config,
                )
            except Exception as e:
                logger.debug(f"[主动消息] 写回长期记忆失败喵: {e}")

        if scheduled_job_payload is not None:
            plugin.scheduler_service.add_chat_job(
                normalized_session_id, scheduled_job_payload["run_date"]
            )
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
