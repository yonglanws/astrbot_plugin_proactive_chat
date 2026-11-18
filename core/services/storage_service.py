"""会话持久化服务。

由旧 core/data_storage.py 等价搬迁而来，读写与清理逻辑保持不变。
本服务在 SessionStore 之上提供会话键规范化与无效数据清理。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..store.session_store import SessionStore

INTENSITY_SEED_FILE = "intensity_seed.json"


class StorageService:
    """会话数据加载/保存与迁移清理服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._store = SessionStore(plugin.data_dir)
        # 复用容器中已存在的文件路径，保持完全一致的落盘位置。
        if getattr(plugin, "session_data_file", None):
            self._store.session_data_file = plugin.session_data_file

    async def load_data(self) -> None:
        """从文件中加载会话数据，并合并独立的强度种子。"""
        self.plugin.session_data = await self._store.load()
        await self._merge_intensity_seed()

    @staticmethod
    def apply_intensity_seed(
        session_data: dict[str, Any],
        seed_sessions: dict[str, Any],
    ) -> int:
        """把数据库预热种子合并进会话数据，返回实际写入的会话数。

        只填充“没有强度事件”的会话；已有运行时事件优先保留。若已有的是
        旧种子而新种子的最后用户消息更晚，则允许用新种子替换。
        """
        if not isinstance(session_data, dict) or not isinstance(seed_sessions, dict):
            return 0

        merged = 0
        for umo, state in seed_sessions.items():
            if not isinstance(umo, str) or not isinstance(state, dict):
                continue
            if not state.get("events"):
                continue
            payload = session_data.setdefault(umo, {})
            if not isinstance(payload, dict):
                continue
            existing = payload.get("intensity")
            if isinstance(existing, dict) and existing.get("events"):
                existing_last = float(existing.get("last_user_ts", 0) or 0)
                seed_last = float(state.get("last_user_ts", 0) or 0)
                if seed_last <= existing_last:
                    continue
            payload["intensity"] = state
            merged += 1
        return merged

    async def _merge_intensity_seed(self) -> None:
        """读取 intensity_seed.json 并合并到内存数据。"""
        seed_path = Path(self.plugin.data_dir) / INTENSITY_SEED_FILE
        if not seed_path.exists():
            return
        try:
            raw = await asyncio.to_thread(seed_path.read_text, encoding="utf-8")
            document = await asyncio.to_thread(json.loads, raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[主动消息] 读取强度种子失败喵: {e}")
            return

        sessions = (
            document.get("sessions")
            if isinstance(document, dict) and isinstance(document.get("sessions"), dict)
            else document
        )
        merged = self.apply_intensity_seed(self.plugin.session_data, sessions)
        if merged:
            logger.info(
                f"[主动消息] 已从 {INTENSITY_SEED_FILE} 合并 {merged} 个会话的强度种子喵。"
            )
            try:
                await self._store.save(self.plugin.session_data)
            except Exception as e:
                logger.warning(f"[主动消息] 保存合并后的会话数据失败喵: {e}")

    async def save_data(self) -> None:
        """将会话数据保存到文件（异步无锁内部实现）。"""
        await self._store.save(self.plugin.session_data)

    def merge_session_info(self, base: dict, incoming: dict) -> dict:
        """转发到 SessionStore 的会话数据合并实现。"""
        return self._store.merge_session_info(base, incoming)

    def normalize_session_data(self) -> bool:
        """规范化并合并 session_data 中的重复会话键。

        相同规范键的载荷按 SessionStore 的合并策略归并，返回是否有变更。
        """
        plugin = self.plugin
        if not plugin.session_data:
            return False

        normalized_data: dict[str, dict] = {}
        changed = False

        for session_id, payload in list(plugin.session_data.items()):
            normalized_id = plugin.session_service.normalize_session_id(session_id)
            if normalized_id != session_id:
                changed = True
                logger.info(
                    f"[主动消息] 规范化会话键: {plugin.session_service.get_session_log_str(session_id)} -> {normalized_id}"
                )

            existing = normalized_data.get(normalized_id)
            if existing:
                normalized_data[normalized_id] = self._store.merge_session_info(
                    existing, payload
                )
                changed = True
            else:
                normalized_data[normalized_id] = payload

        if changed:
            plugin.session_data = normalized_data

        return changed

    def cleanup_invalid_session_data(self) -> int:
        """清理无效的会话数据（旧格式遗留或不可解析条目）。

        判定标准：以旧前缀开头，或无法被 session_service 解析为合法 UMO。
        返回被清理的条目数。
        """
        plugin = self.plugin
        cleaned_count = 0
        invalid_sessions: list[str] = []

        for session_id in list(plugin.session_data.keys()):
            parsed = plugin.session_service.parse_session_id(session_id)
            if (
                session_id.startswith("friend_message:")
                or session_id.startswith("group_message:")
                or not parsed
            ):
                invalid_sessions.append(session_id)
                cleaned_count += 1

        for session_id in invalid_sessions:
            del plugin.session_data[session_id]
            logger.info(
                f"[主动消息] 清理了无效的会话数据: {plugin.session_service.get_session_log_str(session_id)}"
            )

        return cleaned_count
