"""CONTEXT 阶段默认功能：准备上下文与人格。

默认行为由 context_service.prepare_llm_request 提供（覆盖对话历史、
平台流水与混合模式），本功能仅负责在流水线中调用它。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage


class ContextStage(BaseStage):
    """上下文准备阶段。"""

    name = "context_stage"
    hook = HookPoint.CONTEXT
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id

        request_package = await plugin.context_service.prepare_llm_request(session_id)
        if not request_package:
            ctx.abort("context_prepare_failed")
            return StageResult.RESCHEDULE

        # 记录准备结果，供后续阶段消费。
        ctx.conversation_id = request_package["conv_id"]
        ctx.extra["history"] = request_package["history"]
        ctx.extra["system_prompt"] = request_package["system_prompt"]
        ctx.extra["platform_context"] = request_package.get("platform_context", "")
        ctx.extra["conversation"] = request_package.get("conversation")

        # 收集拟人化上下文：关系状态、作息、人格状态与长期记忆。
        effective_session_id = plugin.session_service.normalize_session_id(
            request_package.get("session_id", session_id)
        )
        humanization: dict[str, Any] = {
            "intent": ctx.extra.get("intent", ""),
            "intent_brief": ctx.extra.get("intent_brief", ""),
            "intent_label": (
                (ctx.extra.get("intent_decision") or {}).get("intent_label", "")
            ),
        }
        session_config = (
            plugin.config_service.get_session_config(effective_session_id) or {}
        )
        try:
            humanization["conversation_state"] = (
                plugin.conversation_state_service.snapshot(
                    effective_session_id, session_config=session_config
                )
            )
        except Exception:
            humanization["conversation_state"] = {}
        try:
            humanization["rhythm"] = plugin.rhythm_service.snapshot(
                effective_session_id, session_config=session_config
            )
        except Exception:
            humanization["rhythm"] = {}
        try:
            humanization["persona"] = plugin.persona_state_service.snapshot(
                effective_session_id, session_config=session_config
            )
        except Exception:
            humanization["persona"] = {}

        topic_context = ""
        conversation_memory = getattr(plugin, "conversation_memory_service", None)
        if conversation_memory is not None:
            try:
                conversation_memory.seed_from_history(
                    effective_session_id,
                    request_package.get("history", []),
                    now=time.time(),
                    session_config=session_config,
                )
                humanization["conversation_memory"] = (
                    conversation_memory.snapshot(
                        effective_session_id,
                        session_config=session_config,
                        now=time.time(),
                    )
                )
                topic_context = conversation_memory.format_for_prompt(
                    effective_session_id,
                    session_config=session_config,
                    now=time.time(),
                )
            except Exception as e:
                logger.debug(f"[主动消息] 构建话题账本失败喵: {e}")
        ctx.extra["topic_context"] = topic_context

        memory_context = ""
        memory_bridge = getattr(plugin, "memory_bridge", None)
        if memory_bridge is not None:
            try:
                query = self.build_memory_query(
                    request_package.get("history", []),
                    humanization,
                )
                persona_id = getattr(
                    request_package.get("conversation"), "persona_id", None
                )
                memories = await memory_bridge.recall(
                    effective_session_id,
                    query,
                    persona_id=persona_id,
                )
                memory_context = memory_bridge.format_memories(memories)
                humanization["memory_count"] = len(memories)
            except Exception as e:
                logger.debug(f"[主动消息] 召回长期记忆失败喵: {e}")
        ctx.extra["memory_context"] = memory_context
        ctx.extra["humanization"] = humanization

        # 会话标识最终确定后再次规范化，确保事件构造、last_message_times 与
        # session_data 读取键全局一致（与旧实现完全相同）。
        request_session_id = plugin.session_service.normalize_session_id(
            request_package.get("session_id", session_id)
        )
        ctx.session_id = request_session_id
        ctx.session = plugin.session_service.build_session_key(request_session_id)
        ctx.event = plugin.sender_service.build_proactive_event(request_session_id)

        # 记录任务开始状态快照，用于检测 LLM 生成窗口内是否出现用户新消息。
        ctx.start_state = {
            "last_message_time": plugin.last_message_times.get(request_session_id, 0),
            "unanswered_count": ctx.unanswered_count,
            "timestamp": time.time(),
        }
        logger.debug("[主动消息] 上下文准备完成喵。")
        return StageResult.CONTINUE

    @staticmethod
    def build_memory_query(
        history: list, humanization: dict[str, Any] | None = None
    ) -> str:
        """用最近几条对话与当前意图构造记忆召回查询。"""
        parts: list[str] = []
        for item in list(history or [])[-4:]:
            if isinstance(item, dict):
                content = item.get("content")
            else:
                content = getattr(item, "content", None)
            text = str(content or "").strip()
            if text and not text.startswith(("[系统任务", "[系统", "系统任务：")):
                parts.append(text[:200])
        state = (humanization or {}).get("conversation_state") or {}
        intent_label = str((humanization or {}).get("intent_label") or "")
        if state.get("state_label"):
            parts.append(f"关系状态：{state['state_label']}")
        if intent_label:
            parts.append(f"本轮意图：{intent_label}")
        return " | ".join(parts)[:1200]


class ConversationSourceFeature:
    """CONTEXT 阶段功能（默认上下文来源）。"""

    name = "conversation_source_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.CONTEXT, stage=ContextStage(self.orchestrator), priority=10
        )
