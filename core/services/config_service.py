"""私聊配置读取与验证服务。

本插件只支持私聊主动消息，因此配置路由只解析 ``FriendMessage`` /
``PrivateMessage`` 会话；群聊 UMO 一律返回 ``None``，从源头阻断群聊行为。

兼容性
------
旧版本使用 ``friend_settings`` 作为一级配置块。本版本改名为
``private_settings``，并在启动时自动把旧配置复制到新键，避免升级丢配置。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from ..domain.session_key import is_friend_type

PRIVATE_SETTINGS_KEY = "private_settings"
LEGACY_FRIEND_SETTINGS_KEY = "friend_settings"


class ConfigService:
    """配置校验与会话配置路由服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 私聊配置块
    # ------------------------------------------------------------------
    def get_private_settings(self) -> dict[str, Any]:
        """返回私聊配置块（兼容旧的 friend_settings）。"""
        config = getattr(self.plugin, "config", {}) or {}
        if not hasattr(config, "get"):
            return {}

        private = config.get(PRIVATE_SETTINGS_KEY)
        if isinstance(private, dict):
            return private

        legacy = config.get(LEGACY_FRIEND_SETTINGS_KEY)
        if isinstance(legacy, dict):
            # 只读回退，不在这里写回；迁移由 ensure_private_settings 完成。
            return legacy
        return {}

    def ensure_private_settings(self) -> bool:
        """把旧版 friend_settings 迁移为 private_settings。

        Returns:
            是否发生了迁移。迁移只复制配置，不删除旧键，便于用户回滚。
        """
        config = getattr(self.plugin, "config", {})
        if not hasattr(config, "get") or not hasattr(config, "__setitem__"):
            return False

        if isinstance(config.get(PRIVATE_SETTINGS_KEY), dict):
            return False

        legacy = config.get(LEGACY_FRIEND_SETTINGS_KEY)
        if not isinstance(legacy, dict):
            return False

        config[PRIVATE_SETTINGS_KEY] = copy.deepcopy(legacy)
        logger.info(
            "[主动消息] 检测到旧版 friend_settings，已自动迁移为 private_settings 喵。"
        )
        return True

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------
    async def validate_config(self) -> None:
        """验证私聊配置的完整性。"""
        try:
            self.ensure_private_settings()
            settings = self.get_private_settings()

            if not settings.get("enable", False):
                logger.info(
                    "[主动消息] 私聊主动消息当前未启用喵（private_settings.enable=false）。"
                )
                return

            session_list = settings.get("session_list", [])
            if not session_list:
                logger.warning(
                    "[主动消息] 私聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                )

            schedule_settings = settings.get("schedule_settings", {})
            try:
                min_interval = int(
                    schedule_settings.get("min_interval_minutes", 30) or 30
                )
                max_interval = int(
                    schedule_settings.get("max_interval_minutes", 600) or 600
                )
            except (TypeError, ValueError):
                min_interval, max_interval = 30, 600

            if min_interval > max_interval:
                logger.warning(
                    "[主动消息] 私聊配置中最小间隔大于最大间隔喵，运行时会自动取较大值作为上限。"
                )

            intensity = settings.get("intensity_settings", {})
            if isinstance(intensity, dict) and intensity.get("enable", True):
                p_min = intensity.get("probability_min", 0.15)
                p_max = intensity.get("probability_max", 0.95)
                try:
                    if float(p_min) > float(p_max):
                        logger.warning(
                            "[主动消息] 互动强度概率下限大于上限喵，运行时会自动交换。"
                        )
                except (TypeError, ValueError):
                    logger.warning(
                        "[主动消息] 互动强度概率配置不是数字喵，将使用默认值。"
                    )

            logger.info("[主动消息] 私聊配置验证完成喵。")
        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
            raise

    # ------------------------------------------------------------------
    # 会话配置
    # ------------------------------------------------------------------
    def get_session_config(self, session_id: str) -> dict | None:
        """根据会话 UMO 获取最终生效配置（base + override）。"""
        base = self.get_base_session_config(session_id)
        if not base:
            return None
        return self.build_effective_config(session_id, base)

    def get_base_session_config(self, session_id: str) -> dict | None:
        """获取仅由全局私聊配置与会话命中规则决定的基础配置。"""
        parsed = self.plugin.session_service.parse_session_id(session_id)
        if not parsed:
            return None

        _, message_type, target_id = parsed
        # 群聊配置不参与本插件的任何流程。
        if not is_friend_type(message_type):
            return None

        return self._get_private_session_config(session_id, target_id)

    def build_effective_config(
        self, session_id: str, base_config: dict | None
    ) -> dict | None:
        """将会话差异补丁合并到基础配置，返回最终生效配置。"""
        if not base_config:
            return None

        manager = getattr(self.plugin, "session_override_manager", None)
        if not manager:
            return base_config

        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )
        effective = manager.get_effective(normalized_session_id, base_config)

        if isinstance(effective, dict):
            effective["_session_type"] = "friend"
            effective["_from_session_list"] = base_config.get(
                "_from_session_list", False
            )
            effective["_has_override"] = bool(
                manager.get_override(normalized_session_id)
            )

        return effective

    def _get_private_session_config(
        self, session_id: str, target_id: str
    ) -> dict | None:
        settings = self.get_private_settings()
        if not settings.get("enable", False):
            return None

        session_list = settings.get("session_list", [])
        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )
        candidates = {session_id, normalized_session_id, target_id}

        if any(candidate in session_list for candidate in candidates):
            config_copy = copy.deepcopy(settings)
            config_copy["_session_type"] = "friend"
            config_copy["_from_session_list"] = True
            return config_copy

        return None

    def get_friend_session_config(self, session_id: str, target_id: str) -> dict | None:
        """兼容旧调用点：读取私聊会话配置。"""
        return self._get_private_session_config(session_id, target_id)

    def is_private_session(self, session_id: str) -> bool:
        """判断 UMO 是否属于私聊。"""
        parsed = self.plugin.session_service.parse_session_id(session_id)
        return bool(parsed) and is_friend_type(parsed[1])
