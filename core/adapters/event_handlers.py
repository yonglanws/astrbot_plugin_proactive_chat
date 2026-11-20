"""私聊消息事件监听适配器。

本插件只服务于私聊主动消息：
- 私聊消息会刷新会话活跃时间、取消待触发的自动消息、重置未回复计数，
  并更新该用户的互动强度状态；
- 不包含任何群聊处理逻辑。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..domain.humanization import ReplyQuality, classify_reply
from ..domain.session_key import is_group_umo
from ..store.session_store import SessionStore


class EventHandlers:
    """私聊事件处理逻辑。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _migrate_raw_key(self, session_id: str, normalized_session_id: str) -> bool:
        """把 raw 键的载荷安全合并到规范键，返回是否有变更。"""
        if (
            normalized_session_id == session_id
            or session_id not in self.plugin.session_data
        ):
            return False
        existing_payload = self.plugin.session_data.get(session_id, {})
        current_payload = self.plugin.session_data.get(normalized_session_id)
        if isinstance(current_payload, dict) and isinstance(existing_payload, dict):
            merge_fn = getattr(
                getattr(self.plugin, "storage_service", None),
                "merge_session_info",
                SessionStore.merge_session_info,
            )
            self.plugin.session_data[normalized_session_id] = merge_fn(
                current_payload, existing_payload
            )
        elif isinstance(existing_payload, dict):
            self.plugin.session_data[normalized_session_id] = dict(existing_payload)
        del self.plugin.session_data[session_id]
        return True

    def _mark_memory_message_time(
        self, normalized_session_id: str, current_time: float
    ) -> None:
        """即时更新内存态消息时间（供 REVIEW / 自动触发读取）。

        必须在进入 data_lock 之前调用，保证复核阶段能立即看到最新时间。
        """
        self.plugin.last_message_times[normalized_session_id] = current_time

    def _mark_persisted_message_time(
        self, normalized_session_id: str, current_time: float
    ) -> bool:
        """在锁内持久化消息时间。

        仅记录插件启动之后的时间，避免历史值污染自动触发判定。
        """
        if current_time >= self.plugin.plugin_start_time:
            self.plugin.session_data.setdefault(normalized_session_id, {})[
                "last_message_time"
            ] = current_time
            return True
        return False

    def _get_command_prefixes(self) -> tuple[str, ...]:
        """读取 AstrBot 的唤醒/命令前缀，并补上常见全角写法。"""
        prefixes: list[str] = ["/", "／"]
        try:
            config = self.plugin.context.get_config()
            raw = config.get("wake_prefix", "/")
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    value = str(item or "").strip()
                    if value and value not in prefixes:
                        prefixes.append(value)
        except Exception:
            pass
        return tuple(prefixes)

    @staticmethod
    def extract_event_text(event: AstrMessageEvent) -> str:
        """尽力提取私聊消息的纯文本，供互动强度评分使用。"""
        try:
            text = event.message_str
            if isinstance(text, str) and text.strip():
                return text.strip()
        except Exception:
            pass

        try:
            messages = event.get_messages() or []
        except Exception:
            messages = []

        parts: list[str] = []
        for component in messages:
            text = getattr(component, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            if isinstance(component, dict) and isinstance(component.get("text"), str):
                parts.append(component["text"])
        return "".join(parts).strip()

    async def _persist_user_message_to_platform_history(
        self,
        event: AstrMessageEvent,
        normalized_session_id: str,
    ) -> None:
        """把私聊用户消息补写进平台流水。

        AstrBot 4.x 只对群聊和 webchat 私聊自动落库；QQ / aiocqhttp 等
        普通私聊如果完全依赖平台流水模式，模型只能看到 Bot 自己说过的话。
        这里按与主动消息相同的 platform_id + target_id 口径补写用户侧记录。
        """
        plugin = self.plugin
        try:
            parsed = plugin.session_service.parse_session_id(normalized_session_id)
            if not parsed:
                return
            platform_id, _message_type, target_id = parsed
            # webchat 由 AstrBot 自己写入，避免重复。
            if platform_id == "webchat":
                return

            history_mgr = getattr(plugin.context, "message_history_manager", None)
            insert_message_chain = getattr(history_mgr, "insert_message_chain", None)
            if not callable(insert_message_chain):
                return

            from astrbot.core.message.message_event_result import MessageChain

            sender_id = ""
            sender_name = ""
            try:
                sender_id = str(event.get_sender_id() or "")
            except Exception:
                pass
            try:
                sender_name = str(event.get_sender_name() or "")
            except Exception:
                pass

            await insert_message_chain(
                platform_id=platform_id,
                user_id=target_id,
                message_chain=MessageChain(list(event.get_messages() or [])),
                role="user",
                sender_id=sender_id or None,
                sender_name=sender_name or None,
                # 与 AstrBot 内置群聊流水保持同一保留上限，避免无限增长。
                max_messages=700,
            )
        except Exception as e:
            logger.debug(f"[主动消息] 补写私聊用户流水失败喵: {e}")

    # ------------------------------------------------------------------
    # 私聊
    # ------------------------------------------------------------------
    async def on_friend_message(self, event: AstrMessageEvent) -> None:
        """处理私聊消息：记录强度、取消调度、重置计数并重新排期。"""
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        # 防御性校验：即使平台把群聊事件误投递到私聊入口，也绝不处理。
        if is_group_umo(session_id):
            logger.debug(f"[主动消息] 忽略非私聊会话喵: {session_id}")
            return

        # 平台把 Bot 自己的消息回投成事件时，不能把它算成用户回复。
        try:
            self_id = str(event.get_self_id() or "")
            sender_id = str(event.get_sender_id() or "")
            if self_id and sender_id and self_id == sender_id:
                logger.debug("[主动消息] 忽略 Bot 自身消息回投喵。")
                return
        except Exception:
            pass

        message_text = self.extract_event_text(event)
        # 命令消息不是“用户回复”，不更新强度、不清零未回复计数、
        # 也不触发话题判定 LLM。
        #
        # AstrBot 在私聊里会先剥掉 wake_prefix，所以不能只判断消息是否以
        # “/”开头。4.30 的 waking_check 会在匹配完 handler 后把
        # parsed_params pop 掉，只在 handler 内保留；它同时把每个命中
        # 指令的 handler 汇总成 handlers_parsed_params 写回事件。
        # 因此这里优先读 handlers_parsed_params（覆盖“已注册指令但无前缀”
        # 的私聊场景），parsed_params 仅作为旧版本兜底。
        missing = object()
        try:
            if event.get_extra("parsed_params", missing) is not missing:
                logger.debug("[主动消息] 忽略命令消息喵。")
                return
            handlers_parsed_params = event.get_extra(
                "handlers_parsed_params", None
            )
            if isinstance(handlers_parsed_params, dict) and handlers_parsed_params:
                logger.debug("[主动消息] 忽略命令消息喵。")
                return
        except Exception:
            pass
        if message_text.startswith(self._get_command_prefixes()):
            logger.debug("[主动消息] 忽略命令消息喵。")
            return
        # 兜底：私聊未配置 wake_prefix 时，命令文本可能已被剥成纯指令名。
        first_token = message_text.split(" ", 1)[0].strip().lower()
        if first_token in {
            "主动回复",
            "主动回复状态",
            "开启主动回复",
            "关闭主动回复",
            "主动回复开启",
            "主动回复关闭",
            "proactive",
            "proactive_on",
            "proactive_off",
            "proactive_status",
        }:
            logger.debug("[主动消息] 忽略本插件指令消息喵。")
            return

        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )
        session_config = self.plugin.config_service.get_session_config(
            normalized_session_id
        )
        is_enabled = bool(session_config and session_config.get("enable", False))
        if not is_enabled:
            logger.debug(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 先在锁外记录内存态消息时间，供后续逻辑即时读取。
        current_time = time.time()
        self._mark_memory_message_time(normalized_session_id, current_time)

        state_service = getattr(self.plugin, "conversation_state_service", None)
        feedback_settings = None
        if state_service is not None:
            try:
                feedback_settings = state_service.get_feedback_settings(
                    session_config
                )
            except Exception:
                feedback_settings = None
        min_substantive_length = (
            feedback_settings.min_substantive_length
            if feedback_settings is not None
            else 6
        )
        quality = classify_reply(
            message_text,
            has_media=not message_text,
            min_substantive_length=min_substantive_length,
        )
        feedback_enabled = bool(
            feedback_settings is not None and feedback_settings.enabled
        )
        is_negative = feedback_enabled and quality is ReplyQuality.NEGATIVE
        is_perfunctory = bool(
            feedback_enabled
            and quality is ReplyQuality.PERFUNCTORY
            and feedback_settings is not None
            and feedback_settings.perfunctory_no_reset
        )

        # 在锁内读取暂停状态与 paused_at，避免与并发暂停流程交错。
        async with self.plugin.data_lock:
            self._migrate_raw_key(session_id, normalized_session_id)
            pre_payload = self.plugin.session_data.get(normalized_session_id, {})
            was_paused = bool(
                self.plugin.scheduler_service.is_session_paused(
                    normalized_session_id
                )
            )
            paused_at_before = (
                pre_payload.get("paused_at")
                if isinstance(pre_payload, dict)
                else None
            )

        # 暂停期间用户发来消息：先让话题判定器决定是否真正恢复。
        # LLM 调用必须在 data_lock 之外，避免长时间持有锁阻塞其他任务。
        resume_allowed = True
        judge_result: dict | None = None
        if was_paused and (is_negative or is_perfunctory):
            # 负反馈和纯敷衍不值得再花一次 LLM：它们本来就不算重新开启话题。
            resume_allowed = False
            judge_result = {
                "new_topic": False,
                "reason": f"reply_quality:{quality.value}",
                "mode": "quality_gate",
                "fallback": False,
            }
        elif was_paused:
            try:
                resume_allowed, judge_result = (
                    await self.plugin.topic_judge_service.should_resume(
                        normalized_session_id,
                        message_text,
                        session_config,
                    )
                )
            except Exception as e:
                logger.warning(
                    f"[主动消息] 话题判定异常，按恢复处理以避免用户被永久暂停喵: {e}"
                )
                resume_allowed = True
                judge_result = {
                    "new_topic": True,
                    "reason": f"judge_exception:{type(e).__name__}",
                    "fallback": True,
                }

        keep_paused = bool(was_paused and not resume_allowed)

        async with self.plugin.data_lock:
            # 先迁移旧键，再读取锁内最新暂停状态，防止并发暂停被覆盖。
            self._migrate_raw_key(session_id, normalized_session_id)
            payload = self.plugin.session_data.setdefault(
                normalized_session_id, {}
            )
            now_paused = bool(
                self.plugin.scheduler_service.is_session_paused(
                    normalized_session_id
                )
            )
            paused_at_now = payload.get("paused_at")

            if keep_paused:
                # 判定期间管理端可能已经手动恢复：以锁内最新状态为准。
                if not now_paused:
                    keep_paused = False
            else:
                # 在锁外等待期间，若并发任务（如上限触发或负反馈）刚把会话暂停，
                # 或写入了更新的暂停时间戳，绝不误调用 resume_session()。
                if (not was_paused and now_paused) or (
                    was_paused
                    and now_paused
                    and paused_at_now is not None
                    and paused_at_now != paused_at_before
                ):
                    keep_paused = True

            try:
                self_id = str(event.get_self_id() or "")
                if self_id:
                    payload["self_id"] = self_id
            except Exception:
                pass
            try:
                sender_id = str(event.get_sender_id() or "")
                if sender_id:
                    payload["last_sender_id"] = sender_id
            except Exception:
                pass
            try:
                sender_name = str(event.get_sender_name() or "")
                if sender_name:
                    payload["last_sender_name"] = sender_name
            except Exception:
                pass

            self._mark_persisted_message_time(normalized_session_id, current_time)

            if is_enabled:
                payload = self.plugin.session_data.setdefault(
                    normalized_session_id, {}
                )
                if judge_result is not None:
                    payload["last_topic_judge"] = {
                        **judge_result,
                        "at": current_time,
                    }

                if keep_paused:
                    logger.info(
                        f"[主动消息] 话题判定为旧话题/继续，"
                        f"{self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} "
                        f"继续保持未回复暂停喵。"
                    )
                else:
                    # 用户重新发来消息且判定为新话题：解除未回复暂停，
                    # 后续 schedule_next_chat_and_save 会重新排期。
                    self.plugin.scheduler_service.resume_session(
                        normalized_session_id
                    )

                # 只有已启用私聊主动消息的会话才维护强度状态，避免为无关用户
                # 在持久化文件里堆积大量无用数据。
                try:
                    self.plugin.intensity_service.record_user_message(
                        normalized_session_id,
                        message_text,
                        now=current_time,
                        settings=self.plugin.intensity_service.get_settings(
                            session_config
                        ),
                        # 敷衍/负反馈不算“已回复”，避免抬高主动回复率。
                        counts_as_reply=not (is_perfunctory or is_negative),
                    )
                except Exception as e:
                    logger.debug(f"[主动消息] 更新互动强度失败喵: {e}")

                try:
                    memory_service = getattr(
                        self.plugin, "conversation_memory_service", None
                    )
                    if memory_service is not None:
                        memory_service.record_user_message(
                            normalized_session_id,
                            message_text,
                            quality,
                            now=current_time,
                            session_config=session_config,
                        )
                except Exception as e:
                    logger.debug(f"[主动消息] 更新话题账本失败喵: {e}")

                try:
                    rhythm_service = getattr(self.plugin, "rhythm_service", None)
                    if rhythm_service is not None:
                        rhythm_service.record_user_activity(
                            normalized_session_id, now=current_time
                        )
                except Exception as e:
                    logger.debug(f"[主动消息] 更新作息分布失败喵: {e}")

                if state_service is not None:
                    try:
                        state_service.record_user_message(
                            normalized_session_id,
                            quality,
                            now=current_time,
                            session_config=session_config,
                        )
                        payload["last_reply_quality"] = quality.value
                    except Exception as e:
                        logger.debug(f"[主动消息] 更新会话状态失败喵: {e}")

                try:
                    persona_service = getattr(
                        self.plugin, "persona_state_service", None
                    )
                    if persona_service is not None:
                        persona_service.record_user_message(
                            normalized_session_id,
                            quality,
                            now=current_time,
                            session_config=session_config,
                        )
                except Exception as e:
                    logger.debug(f"[主动消息] 更新人格状态失败喵: {e}")

                if is_negative:
                    # 明确负反馈：暂停主动消息，直到用户之后重新开启话题。
                    try:
                        await self.plugin.scheduler_service.pause_session(
                            normalized_session_id,
                            reason="negative_feedback",
                            threshold=0,
                            unanswered_count=int(
                                payload.get("unanswered_count", 0) or 0
                            ),
                            cooldown_seconds=(
                                feedback_settings.negative_cooldown_hours * 3600
                                if feedback_settings is not None
                                else 0
                            ),
                        )
                        keep_paused = True
                    except Exception as e:
                        logger.warning(
                            f"[主动消息] 负反馈暂停失败喵: {e}"
                        )

        # 启用会话顺手补齐用户侧平台流水，保证“平台流水/混合模式”有真实对话可看。
        # 放在锁外执行，避免数据库 IO 长时间阻塞其他会话。
        if is_enabled:
            await self._persist_user_message_to_platform_history(
                event, normalized_session_id
            )

        auto_trigger_cancelled = (
            await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                session_id
            )
        )
        if normalized_session_id != session_id:
            normalized_cancelled = (
                await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                    normalized_session_id
                )
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        if (
            auto_trigger_cancelled
            and is_enabled
            and normalized_session_id not in self.plugin.first_message_logged
        ):
            self.plugin.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        if keep_paused:
            # 统一走 StorageService 的串行合并写：这里只登记脏数据并由
            # 版本号决定最终落盘内容，避免锁外立即保存的旧快照覆盖
            # 随后在锁内更新的新状态。
            await self.plugin.storage_service.save_data()
            logger.info(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} "
                f"的话题判定未通过，继续保持暂停，等待下一次真正的新话题喵。"
            )
            return

        if not is_enabled:
            logger.debug(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 敷衍回应不把未回复计数清零：这既避免了“嗯”被当作重新投入，
        # 也避免用户只是礼貌应付时被连续追问。
        should_reset_counter = not is_perfunctory

        cancelled = False
        try:
            self.plugin.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.plugin.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        self.plugin.scheduler_service.purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 用户已回复喵，已取消 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )

        if should_reset_counter:
            logger.info(
                f"[主动消息] 重置 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的未回复计数器为0喵。"
            )
        else:
            logger.info(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} "
                f"本次为敷衍回应，保留未回复计数并继续按强度排期喵。"
            )
        await self.plugin.scheduler_service.schedule_next_chat_and_save(
            normalized_session_id, reset_counter=should_reset_counter
        )
