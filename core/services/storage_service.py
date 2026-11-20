"""会话持久化服务。

由旧 core/data_storage.py 等价搬迁而来，读写与清理逻辑保持不变。
本服务在 SessionStore 之上提供会话键规范化与无效数据清理。
"""

from __future__ import annotations

import asyncio
import json
import time
import weakref
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..store.session_store import SessionStore

INTENSITY_SEED_FILE = "intensity_seed.json"
# 会话数据防抖窗口：窗口内的多次变更只落盘一次。
DEFAULT_SAVE_DEBOUNCE_SECONDS = 1.5
# 未启用且长期无活动的会话默认保留天数（0 表示不清理）。
DEFAULT_STALE_SESSION_DAYS = 30

# 同一数据目录下仍有待写数据的 StorageService。
# 热重载会在同一进程内创建新实例；加载前必须先把旧实例的脏数据落盘，
# 否则新实例会读到落后于内存状态的旧文件。弱引用避免实例被卸载后滞留。
_PENDING_SAVES: "weakref.WeakValueDictionary[str, StorageService]" = (
    weakref.WeakValueDictionary()
)


class StorageService:
    """会话数据加载/保存与迁移清理服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._store = SessionStore(plugin.data_dir)
        # 复用容器中已存在的文件路径，保持完全一致的落盘位置。
        if getattr(plugin, "session_data_file", None):
            self._store.session_data_file = plugin.session_data_file
        # 合并写状态：版本号只增不减，落盘前比较版本可丢弃过期快照。
        self._save_version = 0
        self._saved_version = 0
        self._save_task: asyncio.Task | None = None
        self._save_lock: asyncio.Lock | None = None
        self._save_lock_loop: asyncio.AbstractEventLoop | None = None

    @property
    def _save_key(self) -> str:
        """按会话数据文件的绝对路径区分不同插件实例的数据。"""
        try:
            return str(Path(self._store.session_data_file).resolve())
        except Exception:
            return str(self._store.session_data_file)

    async def load_data(self) -> None:
        """从文件中加载会话数据，并合并独立的强度种子。"""
        await self._flush_pending_for_same_file()
        self.plugin.session_data = await self._store.load()
        # 重新加载后内存状态与文件一致，版本号归零。
        self._save_version = 0
        self._saved_version = 0
        await self._merge_intensity_seed()

    async def _flush_pending_for_same_file(self) -> None:
        """加载前先落盘同路径旧实例的待写数据，避免热重载读到旧快照。"""
        pending = _PENDING_SAVES.get(self._save_key)
        if pending is None or pending is self:
            return
        try:
            await pending.flush()
        except Exception as e:
            logger.warning(f"[主动消息] 加载前刷新旧实例待写数据失败喵: {e}")

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

    async def save_data(self, *, immediate: bool = False) -> None:
        """标记会话数据已变更，并把落盘合并到同一条串行路径。

        默认防抖：1.5 秒内的多次变更只写一次，避免每条私聊消息都在
        ``data_lock`` 内对整份 session_data.json 做 deepcopy + 落盘。
        ``immediate=True`` 用于插件卸载等必须立刻持久化的场景。
        """
        self._save_version += 1
        _PENDING_SAVES[self._save_key] = self
        if immediate:
            await self.flush()
            return
        self._schedule_save()

    def _schedule_save(self) -> None:
        """安排一次防抖落盘；已有待执行任务时不重复创建。"""
        if self._save_task is not None and not self._save_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_task = loop.create_task(self._debounced_save())

    async def _debounced_save(self) -> None:
        try:
            await asyncio.sleep(DEFAULT_SAVE_DEBOUNCE_SECONDS)
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[主动消息] 合并保存会话数据失败喵: {e}")

    def _get_save_lock(self) -> asyncio.Lock:
        """返回当前事件循环上的保存锁（跨循环复用时重建）。"""
        loop = asyncio.get_running_loop()
        if self._save_lock is None or self._save_lock_loop is not loop:
            self._save_lock = asyncio.Lock()
            self._save_lock_loop = loop
        return self._save_lock

    async def flush(self) -> None:
        """立即把内存快照落盘；并发调用由同一把锁串行化。

        落盘前记录当前版本；写完后若版本已推进（落盘期间又有新变更），
        继续写一次最新快照，保证不会用旧快照覆盖新状态。
        """
        current_task = asyncio.current_task()
        pending_task = self._save_task
        if (
            pending_task is not None
            and pending_task is not current_task
            and not pending_task.done()
        ):
            pending_task.cancel()
        self._save_task = None

        while self._saved_version < self._save_version:
            async with self._get_save_lock():
                target_version = self._save_version
                if self._saved_version >= target_version:
                    continue
                saved = await self._store.save(self.plugin.session_data)
                if not saved:
                    # 保留脏标记与待写登记，下次变更或卸载时重试。
                    logger.error("[主动消息] 会话数据落盘失败喵，保留待写标记喵。")
                    return
                self._saved_version = target_version
        if _PENDING_SAVES.get(self._save_key) is self:
            _PENDING_SAVES.pop(self._save_key, None)

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

    # ------------------------------------------------------------------
    # 保守淘汰长期未活跃的未启用会话
    # ------------------------------------------------------------------
    def _get_stale_retention_days(self) -> float:
        """读取未启用会话的保留天数，配置缺失/非法时回退默认值。"""
        config = getattr(self.plugin, "config", None)
        raw: Any = DEFAULT_STALE_SESSION_DAYS
        try:
            private = config.get("private_settings", {}) if config is not None else {}
            retention = (
                private.get("data_retention_settings", {})
                if isinstance(private, dict)
                else {}
            )
            if isinstance(retention, dict):
                raw = retention.get(
                    "stale_session_days", DEFAULT_STALE_SESSION_DAYS
                )
        except Exception:
            raw = DEFAULT_STALE_SESSION_DAYS
        try:
            days = float(raw)
        except (TypeError, ValueError):
            days = float(DEFAULT_STALE_SESSION_DAYS)
        return max(0.0, days)

    @staticmethod
    def _as_timestamp(value: Any) -> float:
        if isinstance(value, bool) or value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _session_last_active_ts(cls, payload: dict[str, Any]) -> float:
        """从会话载荷中取最近一次活动时间；没有任何时间戳时返回 0。"""
        candidates: list[Any] = [
            payload.get("last_message_time"),
            payload.get("last_scheduled_at"),
            payload.get("paused_at"),
            payload.get("cooldown_until"),
        ]
        for key, nested_keys in (
            ("conversation_state", ("last_user_at", "last_proactive_at")),
            ("conversation_memory", ("last_updated",)),
            ("persona_state", ("updated_at",)),
            ("rhythm", ("last_update_ts",)),
            ("intensity", ("last_user_ts",)),
            ("last_topic_judge", ("at",)),
            ("last_guard", ("at",)),
            ("last_critic", ("at",)),
        ):
            nested = payload.get(key)
            if not isinstance(nested, dict):
                continue
            candidates.extend(nested.get(nested_key) for nested_key in nested_keys)

        # 强度事件列表本身也可能比 last_user_ts 更新（旧数据只剩事件）。
        intensity = payload.get("intensity")
        if isinstance(intensity, dict):
            events = intensity.get("events")
            if isinstance(events, list) and events:
                last_event = events[-1]
                if isinstance(last_event, dict):
                    candidates.append(last_event.get("t"))
                else:
                    candidates.append(last_event)

        return max((cls._as_timestamp(value) for value in candidates), default=0.0)

    def prune_stale_session_data(self, *, now: float | None = None) -> int:
        """保守淘汰未启用且长期无活动的会话，返回清理条数。

        只动 ``enable=false`` 的会话；启用中的会话、有近期活动时间戳的
        会话、以及完全没有时间戳的会话一律保留，避免误删仍在使用的数据。
        """
        plugin = self.plugin
        retention_days = self._get_stale_retention_days()
        if retention_days <= 0:
            return 0

        current_time = time.time() if now is None else float(now)
        threshold = current_time - retention_days * 86400.0
        removed = 0

        for session_id in list(plugin.session_data.keys()):
            payload = plugin.session_data.get(session_id)
            if not isinstance(payload, dict):
                continue
            try:
                session_config = plugin.config_service.get_session_config(session_id)
            except Exception:
                session_config = None
            if session_config and session_config.get("enable", False):
                continue

            last_active = self._session_last_active_ts(payload)
            if last_active <= 0 or last_active >= threshold:
                continue

            del plugin.session_data[session_id]
            removed += 1
            logger.info(
                f"[主动消息] 已清理 {retention_days:g} 天未活跃的未启用会话数据: "
                f"{plugin.session_service.get_session_log_str(session_id)}"
            )

        return removed
