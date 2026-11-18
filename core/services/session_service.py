"""会话解析、规范化与日志格式化服务。

由旧 core/session_parser.py 等价搬迁而来，逻辑与输出格式保持不变。
"""

from __future__ import annotations

from typing import Any

from astrbot.core.platform.platform import PlatformStatus

from ..adapters.astrbot_platform import iter_active_platform_instances
from ..domain.session_key import SessionKey, is_friend_type, is_group_type


class SessionService:
    """会话标识解析与展示服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def build_session_key(self, session_id: str) -> SessionKey:
        """尽力解析会话键；解析失败时回退为占位键。"""
        key = SessionKey.parse(session_id)
        if key is not None:
            return key
        return SessionKey("", session_id, session_id)

    def parse_session_id(self, session_id: str) -> tuple[str, str, str] | None:
        """解析会话 UMO，返回 (platform, message_type, target_id)。"""
        key = SessionKey.parse(session_id)
        if key is None:
            return None
        return key.platform, key.message_type, key.target_id

    def get_session_name(
        self, session_id: str, session_config: dict | None = None
    ) -> str:
        """获取会话备注名（用于日志与前端展示）。"""
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)

        def _pick_name(payload: dict | None) -> str:
            if not isinstance(payload, dict):
                return ""
            for key in ("session_name", "_session_name", "alias"):
                raw = payload.get(key)
                if raw is None:
                    continue
                text = str(raw).strip()
                if text:
                    return text
            return ""

        name = _pick_name(session_config)
        if name:
            return name

        try:
            resolved_config = plugin.config_service.get_session_config(
                normalized_session_id
            )
            name = _pick_name(resolved_config)
            if name:
                return name
        except Exception:
            pass

        manager = getattr(plugin, "session_override_manager", None)
        if manager:
            try:
                override = manager.get_override(normalized_session_id)
                name = _pick_name(override)
                if name:
                    return name
            except Exception:
                pass

        data = getattr(plugin, "session_data", {})
        if isinstance(data, dict):
            name = _pick_name(data.get(normalized_session_id))
            if name:
                return name
            name = _pick_name(data.get(session_id))
            if name:
                return name

        return ""

    def get_session_display_name(
        self, session_id: str, session_config: dict | None = None
    ) -> str:
        """获取会话展示名：备注名优先，缺失时回退 UMO。"""
        name = self.get_session_name(session_id, session_config)
        return name if name else session_id

    def get_session_log_str(
        self, session_id: str, session_config: dict | None = None
    ) -> str:
        """获取统一格式的会话日志字符串。"""
        parsed = self.parse_session_id(session_id)
        session_name = self.get_session_name(session_id, session_config)

        if not parsed:
            return f"{session_id} ({session_name})" if session_name else session_id

        _, msg_type, target_id = parsed
        type_str = "未知类型"
        if is_friend_type(msg_type):
            type_str = "私聊"
        elif is_group_type(msg_type):
            type_str = "群聊"

        log_str = f"{type_str} {target_id}"
        if session_name:
            log_str += f" ({session_name})"
        return log_str

    def _is_explicitly_configured_platform(
        self, platform: str, target_id: str, msg_type: str
    ) -> bool:
        """判断平台标识是否在配置、持久化状态或会话覆写中明确出现。"""
        plugin = self.plugin
        candidate_umo = f"{platform}:{msg_type}:{target_id}"

        config_service = getattr(plugin, "config_service", None)
        if config_service is not None:
            try:
                private_settings = config_service.get_private_settings() or {}
                session_list = private_settings.get("session_list") or []
                for entry in session_list:
                    if isinstance(entry, str) and entry.startswith(f"{platform}:"):
                        return True
            except Exception:
                pass

        session_data = getattr(plugin, "session_data", None)
        if isinstance(session_data, dict) and candidate_umo in session_data:
            return True

        override_mgr = getattr(plugin, "session_override_manager", None)
        if override_mgr is not None:
            try:
                if candidate_umo in override_mgr.list_sessions():
                    return True
            except Exception:
                pass

        return False

    def resolve_full_umo(
        self, target_id: str, msg_type: str, preferred_platform: str | None = None
    ) -> str:
        """动态解析并验证存活的 UMO。"""
        plugin = self.plugin
        type_keyword = "Friend" if is_friend_type(msg_type) else "Group"

        platform_manager = getattr(getattr(plugin, "context", None), "platform_manager", None)
        active_insts = (
            {
                p.meta().id: p
                for p in iter_active_platform_instances(platform_manager)
            }
            if platform_manager is not None
            else {}
        )

        # 只要目标平台实例仍然存在（即便暂时断开/重连），或已在配置/状态中
        # 明确指定，就保留原平台前缀，避免瞬时断连把 UMO 漂移到其他平台导致计数清零。
        if preferred_platform:
            if preferred_platform in active_insts or self._is_explicitly_configured_platform(
                preferred_platform, target_id, msg_type
            ):
                return f"{preferred_platform}:{msg_type}:{target_id}"

        for existing_id in getattr(plugin, "session_data", {}).keys():
            if type_keyword in existing_id and existing_id.endswith(f":{target_id}"):
                p_id = existing_id.split(":")[0]
                if (
                    p_id in active_insts
                    and active_insts[p_id].status == PlatformStatus.RUNNING
                ):
                    return existing_id

        running_platforms = [
            p for p in active_insts.values() if p.status == PlatformStatus.RUNNING
        ]
        if running_platforms:
            return f"{running_platforms[0].meta().id}:{msg_type}:{target_id}"

        if preferred_platform:
            return f"{preferred_platform}:{msg_type}:{target_id}"

        fallback_p_id = list(active_insts.keys())[0] if active_insts else "default"
        return f"{fallback_p_id}:{msg_type}:{target_id}"

    def normalize_session_id(self, session_id: str) -> str:
        """规范化 UMO，确保使用可运行的平台前缀。"""
        parsed = self.parse_session_id(session_id)
        if not parsed:
            return session_id

        platform, msg_type, target_id = parsed
        return self.resolve_full_umo(target_id, msg_type, platform)
