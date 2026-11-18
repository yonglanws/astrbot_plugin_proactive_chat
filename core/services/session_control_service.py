"""会话级主动回复开关。

通过聊天指令为「当前私聊」单独开启/关闭主动回复，配置写入
``session_overrides.json``，不需要修改全局 ``session_list`` 或插件配置。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from astrbot.api import logger

from ..domain.session_key import is_friend_type


class SessionControlService:
    """处理当前私聊的主动回复开关与状态查询。"""

    HELP_TEXT = (
        "主动回复指令：\n"
        "- /开启主动回复：为当前私聊开启\n"
        "- /关闭主动回复：为当前私聊关闭\n"
        "- /主动回复状态：查看当前状态\n"
        "也可以使用 /主动回复 开、/主动回复 关、/主动回复 状态。"
    )

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------
    def _resolve_private_session(self, event: Any) -> tuple[str, str] | None:
        """解析当前事件对应的私聊 UMO 与会话配置。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return None
        parsed = self.plugin.session_service.parse_session_id(umo)
        if parsed and not is_friend_type(parsed[1]):
            return None
        normalized = self.plugin.session_service.normalize_session_id(umo)
        return normalized, umo

    def _format_timestamp(self, timestamp: Any) -> str:
        if not isinstance(timestamp, (int, float)) or timestamp <= 0:
            return "未排期"
        try:
            return datetime.fromtimestamp(
                float(timestamp), tz=getattr(self.plugin, "timezone", None)
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return "未排期"

    async def _save_override(self, session_id: str, enabled: bool) -> None:
        manager = self.plugin.session_override_manager
        override = manager.get_override(session_id)
        if not isinstance(override, dict):
            override = {}
        override["enable"] = bool(enabled)
        await manager.set_override(session_id, override)

    # ------------------------------------------------------------------
    # 开启 / 关闭 / 状态
    # ------------------------------------------------------------------
    async def enable(self, event: Any) -> str:
        """为当前私聊开启主动回复，并立即安排下一次。"""
        resolved = self._resolve_private_session(event)
        if resolved is None:
            return "主动回复只支持私聊会话。"

        session_id, _umo = resolved
        private_settings = self.plugin.config_service.get_private_settings()
        if not private_settings.get("enable", False):
            return (
                "全局私聊主动消息总开关当前是关闭的，无法为单个会话开启。"
                "请先在插件配置中启用「私聊主动消息」。"
            )

        await self._save_override(session_id, True)
        # 显式开启视为一次重新开始：解除未回复暂停/负反馈冷却。
        try:
            data_lock = getattr(self.plugin, "data_lock", None)
            if data_lock is not None:
                async with data_lock:
                    self.plugin.scheduler_service.resume_session(
                        session_id, clear_cooldown=True
                    )
            else:
                self.plugin.scheduler_service.resume_session(
                    session_id, clear_cooldown=True
                )
        except Exception as e:
            logger.debug(f"[主动消息] 解除会话暂停失败喵: {e}")

        try:
            await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                session_id
            )
            await self.plugin.scheduler_service.schedule_next_chat_and_save(
                session_id, reset_counter=True
            )
        except Exception as e:
            logger.error(f"[主动消息] 开启主动回复后安排任务失败喵: {e}")
            return "已开启当前私聊的主动回复，但安排下一次任务失败，请查看日志。"

        return "已开启当前私聊的主动回复"

    async def disable(self, event: Any) -> str:
        """关闭当前私聊的主动回复，并取消已有任务。"""
        resolved = self._resolve_private_session(event)
        if resolved is None:
            return "主动回复只支持私聊会话。"

        session_id, _umo = resolved
        await self._save_override(session_id, False)

        try:
            self.plugin.scheduler.remove_job(session_id)
        except Exception:
            pass
        try:
            self.plugin.scheduler_service.purge_related_jobs(session_id)
        except Exception:
            pass
        try:
            await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                session_id
            )
        except Exception as e:
            logger.debug(f"[主动消息] 关闭主动回复时取消自动触发器失败喵: {e}")

        async with self.plugin.data_lock:
            self.plugin.scheduler_service.clear_session_schedule_state(session_id)
            await self.plugin.storage_service.save_data()

        return (
            "已关闭当前私聊的主动回复。已有排期已取消；"
            "发送 /开启主动回复 可重新开启。"
        )

    async def status(self, event: Any) -> str:
        """查询当前私聊的主动回复状态。"""
        resolved = self._resolve_private_session(event)
        if resolved is None:
            return "主动回复只支持私聊会话。"

        session_id, _umo = resolved
        session_config = self.plugin.config_service.get_session_config(session_id)
        override = self.plugin.session_override_manager.get_override(session_id)
        if not session_config:
            if isinstance(override, dict) and override.get("enable") is False:
                return (
                    "主动回复：已关闭（当前会话的会话级开关）\n"
                    "发送 /开启主动回复 可重新开启。"
                )
            return (
                "当前私聊尚未开启主动回复。\n"
                "发送 /开启主动回复 即可为这个会话开启，无需修改配置文件。"
            )

        enabled = bool(session_config.get("enable", False))
        payload = self.plugin.session_data.get(session_id, {})
        lines = [
            f"主动回复：{'已开启' if enabled else '已关闭'}",
            f"下一次触发：{self._format_timestamp(payload.get('next_trigger_time')) if enabled else '—'}",
            f"未回复次数：{int(payload.get('unanswered_count', 0) or 0)}",
        ]
        if self.plugin.scheduler_service.is_session_paused(session_id):
            lines.append("当前状态：未回复暂停中，用户发来新消息或重新开启后恢复")
        elif self.plugin.scheduler_service.is_in_cooldown(session_id):
            remaining = int(
                self.plugin.scheduler_service.cooldown_remaining(session_id)
            )
            lines.append(f"当前状态：负反馈冷却中，约 {max(1, remaining // 60)} 分钟")
        else:
            lines.append("当前状态：正常")
        return "\n".join(lines)

    async def handle(self, event: Any, action: str = "") -> str:
        """按 /主动回复 后面的动作分发。"""
        normalized = str(action or "").strip().lower()
        if normalized in {"开", "开启", "启用", "on", "true", "1", "enable"}:
            return await self.enable(event)
        if normalized in {"关", "关闭", "禁用", "off", "false", "0", "disable"}:
            return await self.disable(event)
        if normalized in {"", "状态", "status", "查看"}:
            return await self.status(event)
        return self.HELP_TEXT


__all__ = ["SessionControlService"]
