"""LLM 调用与提示词构建服务。

由旧 core/llm_adapter.py 的提示词与 Provider 调用逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from ...utils.time_utils import format_current_time, format_relative_gap
from ..adapters.astrbot_event import dispatch_event_hook
from ..adapters.astrbot_provider import build_provider_request, invoke_provider

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.star.star_handler import EventType
except ImportError:  # pragma: no cover
    EventType = None  # type: ignore[assignment]


class LlmService:
    """提示词构建与 LLM 调用服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 动态内容块
    # ------------------------------------------------------------------
    def build_dynamic_context_text(
        self,
        unanswered_count: int,
        session_config: dict | None = None,
        *,
        include_time: bool = True,
        include_unanswered: bool = True,
        intensity_snapshot: dict | None = None,
        humanization: dict | None = None,
    ) -> str:
        """构建每轮都会变化的运行时上下文块。"""
        lines: list[str] = []

        if include_time:
            lines.append(f"- 当前时间：{format_current_time(self.plugin.timezone)}")
        if include_unanswered:
            lines.append(f"- 本次主动消息的未回复累计次数：{unanswered_count}")

        lines.append("- 会话场景：私聊")

        if isinstance(intensity_snapshot, dict) and intensity_snapshot:
            score = intensity_snapshot.get("score")
            tier_label = intensity_snapshot.get("tier_label") or "未知"
            probability = intensity_snapshot.get("probability_percent")
            if score is not None:
                lines.append(
                    f"- 对方最近的互动强度：{tier_label}（{score}/100），"
                    f"主动触发概率参考 {probability}%"
                )
                tier = intensity_snapshot.get("tier")
                if tier in {"close", "blazing"}:
                    lines.append(
                        "- 对方最近聊得很投入：可以更主动地延续话题、表达想念或分享日常。"
                    )
                elif tier in {"dormant", "steady"}:
                    lines.append(
                        "- 对方最近聊得较少：语气宜轻、宜短，先试探性地开启话题。"
                    )

        if isinstance(humanization, dict) and humanization:
            lines.extend(self._build_humanization_lines(humanization))

        if not lines:
            return ""

        return "<dynamic_context>\n" + "\n".join(lines) + "\n</dynamic_context>"

    @staticmethod
    def _build_humanization_lines(humanization: dict) -> list[str]:
        """把关系状态、意图、精力与作息压缩成少量提示行。"""
        lines: list[str] = []
        state = humanization.get("conversation_state") or {}
        if state.get("state_label"):
            lines.append(
                f"- 当前关系状态：{state['state_label']}"
                + (
                    f"（原因：{state['reason']}）"
                    if state.get("reason")
                    else ""
                )
            )
        now = time.time()
        last_user_at = state.get("last_user_at")
        last_proactive_at = state.get("last_proactive_at")
        if isinstance(last_user_at, (int, float)) and last_user_at > 0:
            lines.append(
                f"- 对方最后一条消息：{format_relative_gap(now - last_user_at)}"
            )
        if isinstance(last_proactive_at, (int, float)) and last_proactive_at > 0:
            lines.append(
                f"- 你上一次主动消息：{format_relative_gap(now - last_proactive_at)}"
            )
        intent_label = humanization.get("intent_label")
        brief = humanization.get("intent_brief")
        if intent_label:
            lines.append(
                f"- 本轮主动意图：{intent_label}"
                + (f"。{brief}" if brief else "")
            )

        persona = humanization.get("persona") or {}
        if persona.get("enabled"):
            lines.append(
                f"- 你此刻的状态：{persona.get('mood_label', '平静')}，"
                f"精力 {persona.get('energy_percent', 60)}%，"
                f"{persona.get('activity', '日常')}。"
                "状态低时就短一点、轻一点，不要硬撑热情。"
            )

        rhythm = humanization.get("rhythm") or {}
        if rhythm.get("enabled"):
            daily = (
                f"{rhythm.get('daily_count', 0)}/{rhythm.get('daily_limit', 0)}"
                if rhythm.get("daily_limit")
                else str(rhythm.get("daily_count", 0))
            )
            lines.append(
                f"- 今日已主动发送：{daily} 次；"
                f"对方通常活跃时段：{rhythm.get('active_hours_label', '样本不足')}。"
            )

        conversation_memory = humanization.get("conversation_memory") or {}
        if conversation_memory.get("enabled"):
            open_loop_count = int(
                conversation_memory.get("open_loop_count", 0) or 0
            )
            if open_loop_count > 0:
                lines.append(
                    f"- 还有 {open_loop_count} 个未闭环话题；"
                    "优先自然跟进其中一条，不要逐条追问。"
                )
            profile_text = str(
                conversation_memory.get("profile_summary") or ""
            ).strip()
            if profile_text:
                lines.append(f"- 对方表达习惯：{profile_text}")

        plan = humanization.get("plan") or {}
        if plan:
            plan_parts = []
            if plan.get("topic"):
                plan_parts.append(f"话题={plan['topic']}")
            if plan.get("goal"):
                plan_parts.append(f"目标={plan['goal']}")
            if plan.get("tone"):
                plan_parts.append(f"语气={plan['tone']}")
            if plan.get("avoid"):
                plan_parts.append(f"避免={plan['avoid']}")
            if plan_parts:
                lines.append("- 本轮写作计划：" + "；".join(plan_parts))

        return lines

    def build_extra_content_parts(
        self,
        platform_context: str,
        prompt_template: str,
        unanswered_count: int,
        session_config: dict | None = None,
        intensity_snapshot: dict | None = None,
        humanization: dict | None = None,
        memory_context: str = "",
        topic_context: str = "",
    ) -> list:
        """构建追加在本轮用户消息之后的临时内容块列表。"""
        parts: list = []
        if platform_context:
            part = self.plugin.context_service.make_temp_text_part(platform_context)
            if part is not None:
                parts.append(part)

        if memory_context:
            memory_part = self.plugin.context_service.make_temp_text_part(
                memory_context
            )
            if memory_part is not None:
                parts.append(memory_part)

        if topic_context:
            topic_part = self.plugin.context_service.make_temp_text_part(
                topic_context
            )
            if topic_part is not None:
                parts.append(topic_part)

        dynamic_text = self.build_dynamic_context_text(
            unanswered_count,
            session_config,
            include_time="{{current_time}}" not in prompt_template,
            include_unanswered="{{unanswered_count}}" not in prompt_template,
            intensity_snapshot=intensity_snapshot,
            humanization=humanization,
        )
        if dynamic_text:
            dynamic_part = self.plugin.context_service.make_temp_text_part(dynamic_text)
            if dynamic_part is not None:
                parts.append(dynamic_part)

        return parts

    # ------------------------------------------------------------------
    # 钩子派发
    # ------------------------------------------------------------------
    async def dispatch_llm_request_hooks(self, event: Any, req: Any) -> bool:
        """派发 LLM 请求前置钩子。"""
        if event is None or EventType is None:
            return False

        await dispatch_event_hook(event, EventType.OnWaitingLLMRequestEvent)
        if event.is_stopped():
            return True

        return await dispatch_event_hook(event, EventType.OnLLMRequestEvent, req)

    async def dispatch_llm_response_hooks(self, event: Any, resp: Any) -> bool:
        """派发 LLM 响应后置钩子。"""
        if event is None or EventType is None or resp is None:
            return False
        return await dispatch_event_hook(event, EventType.OnLLMResponseEvent, resp)

    # ------------------------------------------------------------------
    # Provider 解析
    # ------------------------------------------------------------------
    async def resolve_chat_provider(self, session_id: str) -> Any:
        """解析用于本轮请求的 Provider 实例。"""
        plugin = self.plugin
        provider_id = None
        try:
            provider_id = await plugin.context.get_current_chat_provider_id(session_id)
        except Exception as e:
            logger.warning(f"[主动消息] 获取当前对话 Provider 失败喵: {e}")

        if provider_id:
            try:
                provider = await plugin.context.provider_manager.get_provider_by_id(
                    provider_id
                )
                if provider:
                    return provider
            except Exception as e:
                logger.warning(f"[主动消息] 按 ID 获取 Provider 失败喵: {e}")

        try:
            return plugin.context.get_using_provider(umo=session_id)
        except Exception as e:
            logger.warning(f"[主动消息] 回退获取 Provider 失败喵: {e}")
            return None

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    async def generate_llm_response(
        self,
        session_id: str,
        session_config: dict,
        history_messages: list,
        system_prompt: str,
        unanswered_count: int,
        event: Any = None,
        conversation: Any = None,
        platform_context: str = "",
        model: str | None = None,
        intensity_snapshot: dict | None = None,
        humanization: dict | None = None,
        memory_context: str = "",
        topic_context: str = "",
    ) -> tuple[Any | None, str]:
        """统一 LLM 调用入口。

        model 为 None 时使用 Provider 自身的默认模型；传入非空值时仅覆盖本次
        请求的模型，供后续按场景（压缩、降级、专用模型）分层调用复用。

        intensity_snapshot 由 GATE 阶段产出，用于把「互动强度」注入提示词，
        让模型据此调整语气与主动性。
        """
        plugin = self.plugin
        motivation_template = session_config.get("proactive_prompt", "") or ""
        now_str = format_current_time(plugin.timezone)
        intensity_score = ""
        intensity_tier = ""
        intensity_probability = ""
        if isinstance(intensity_snapshot, dict) and intensity_snapshot:
            intensity_score = str(intensity_snapshot.get("score", ""))
            intensity_tier = str(intensity_snapshot.get("tier_label", ""))
            intensity_probability = str(
                intensity_snapshot.get("probability_percent", "")
            )

        humanization = humanization if isinstance(humanization, dict) else {}
        conversation_state = humanization.get("conversation_state") or {}
        persona = humanization.get("persona") or {}
        rhythm = humanization.get("rhythm") or {}
        state_label = str(conversation_state.get("state_label") or "")
        intent_label = str(humanization.get("intent_label") or "")
        intent_brief = str(humanization.get("intent_brief") or "")
        persona_text = (
            f"{persona.get('mood_label', '平静')}，精力 "
            f"{persona.get('energy_percent', 60)}%，{persona.get('activity', '日常')}"
            if persona
            else ""
        )
        active_hours = str(rhythm.get("active_hours_label") or "")
        daily_budget = (
            f"{rhythm.get('daily_count', 0)}/{rhythm.get('daily_limit', 0)}"
            if rhythm.get("daily_limit")
            else ""
        )

        final_user_simulation_prompt = (
            motivation_template.replace("{{unanswered_count}}", str(unanswered_count))
            .replace("{{current_time}}", now_str)
            .replace("{{intensity_score}}", intensity_score)
            .replace("{{intensity_tier}}", intensity_tier)
            .replace("{{intensity_probability}}", intensity_probability)
            .replace("{{conversation_state}}", state_label)
            .replace("{{intent}}", intent_label)
            .replace("{{intent_brief}}", intent_brief)
            .replace("{{persona_state}}", persona_text)
            .replace("{{active_hours}}", active_hours)
            .replace("{{daily_budget}}", daily_budget)
            .replace("{{memory_block}}", memory_context or "")
            .replace("{{topic_ledger}}", topic_context or "")
        )

        logger.debug("[主动消息] 已生成包含动机、时间与互动强度的 Prompt 喵。")

        history_messages = plugin.context_service.sanitize_history_content(
            history_messages or []
        )
        # prompt_context 关闭时不追加强度动态上下文；用户在模板里显式写的
        # 占位符仍会正常替换，避免“配置关闭”与“模板要求”互相打架。
        context_intensity_snapshot = intensity_snapshot
        intensity_service = getattr(plugin, "intensity_service", None)
        if intensity_service is not None:
            try:
                if not intensity_service.get_settings(session_config).prompt_context:
                    context_intensity_snapshot = None
            except Exception:
                pass

        extra_parts = self.build_extra_content_parts(
            platform_context=platform_context,
            prompt_template=motivation_template,
            unanswered_count=unanswered_count,
            session_config=session_config,
            intensity_snapshot=context_intensity_snapshot,
            humanization=humanization,
            memory_context=memory_context,
            topic_context=topic_context,
        )

        provider = await self.resolve_chat_provider(session_id)
        if not provider:
            logger.warning("[主动消息] 未找到 LLM Provider，放弃并重新调度喵。")
            return None, final_user_simulation_prompt

        req = build_provider_request(
            prompt=final_user_simulation_prompt,
            session_id=session_id,
            contexts=history_messages,
            system_prompt=system_prompt,
            extra_parts=extra_parts,
            conversation=conversation,
            model=model,
        )

        try:
            stopped = await self.dispatch_llm_request_hooks(event, req)
        except Exception as e:
            logger.error(f"[主动消息] 派发 LLM 前置钩子失败喵: {e}")
            stopped = False
        if stopped:
            logger.info("[主动消息] LLM 前置钩子终止了事件传播，放弃本次请求喵。")
            return None, final_user_simulation_prompt

        llm_response_obj = None
        try:
            llm_response_obj = await invoke_provider(provider, req)
            logger.info("[主动消息] 调用 LLM 成功喵。")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_feature(
                            "llm_generate_result",
                            {
                                "provider_mode": "provider_request",
                                "success": True,
                                "history_count": len(history_messages),
                                "extra_part_count": len(req.extra_user_content_parts),
                            },
                        )
                    )
                )
        except Exception as llm_error:
            logger.error(f"[主动消息] 调用 LLM 失败喵: {llm_error}")
            logger.info(f"[主动消息] 错误类型喵: {type(llm_error).__name__}")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_error(
                            llm_error,
                            module="core.llm_adapter._generate_llm_response",
                        )
                    )
                )
            return None, final_user_simulation_prompt

        if llm_response_obj is None:
            logger.warning("[主动消息] LLM 返回空响应，重新调度喵。")
            return None, final_user_simulation_prompt

        response_stopped = False
        try:
            response_stopped = await self.dispatch_llm_response_hooks(
                event, llm_response_obj
            )
        except Exception as e:
            logger.error(f"[主动消息] 派发 LLM 后置钩子失败喵: {e}")

        if response_stopped:
            logger.info("[主动消息] LLM 后置钩子终止了事件传播，放弃本次生成结果喵。")
            return None, final_user_simulation_prompt

        response_text = self.extract_response_text(llm_response_obj)
        if not response_text:
            if getattr(llm_response_obj, "result_chain", None):
                logger.info("[主动消息] 生成结果无文本但有消息链，继续后续流程喵。")
                return llm_response_obj, self.resolve_final_user_prompt(
                    req, final_user_simulation_prompt
                )
            logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度喵。")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_feature(
                            "llm_generate_result",
                            {
                                "provider_mode": "unknown",
                                "success": False,
                                "history_count": len(history_messages),
                            },
                        )
                    )
                )
            return None, final_user_simulation_prompt

        if response_text == "[object Object]":
            logger.error(
                "[主动消息] 喵呜！LLM 返回了意料之外的 '[object Object]' 字符串喵！"
            )
            logger.warning(
                "[主动消息] 这通常是因为上下文或 Prompt 中包含了无法解析的对象喵。已拦截本次发送喵。"
            )
            return None, final_user_simulation_prompt

        logger.info(f"[主动消息] LLM 已生成文本喵，长度: {len(response_text)}。")
        if plugin.telemetry and plugin.telemetry.enabled:
            plugin._track_task(
                asyncio.create_task(
                    plugin.telemetry.track_feature(
                        "llm_response_ready",
                        {
                            "response_length": len(response_text),
                            "session_type": session_config.get(
                                "_session_type", "unknown"
                            ),
                        },
                    )
                )
            )
        return llm_response_obj, self.resolve_final_user_prompt(
            req, final_user_simulation_prompt
        )

    @staticmethod
    def resolve_final_user_prompt(req: Any, fallback: str) -> str:
        """取钩子处理后的最终用户提示词。"""
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str) and prompt.strip():
            return prompt
        return fallback

    @staticmethod
    def extract_response_text(llm_response_obj: Any) -> str:
        """从 LLM 响应中安全提取纯文本。"""
        if llm_response_obj is None:
            return ""
        try:
            text = llm_response_obj.completion_text
        except Exception:
            text = None
        if text is None:
            return ""
        return str(text).strip()

    @staticmethod
    def extract_response_chain(llm_response_obj: Any) -> list:
        """从 LLM 响应中安全提取消息链组件列表。"""
        if llm_response_obj is None:
            return []
        chain_obj = getattr(llm_response_obj, "result_chain", None)
        if chain_obj is None:
            return []
        chain = getattr(chain_obj, "chain", None)
        if chain is None:
            return []
        try:
            return [comp for comp in chain if comp is not None]
        except Exception:
            return []
