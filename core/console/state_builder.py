"""Web 控制台状态载荷构建（私聊版）。

只输出私聊会话、自动触发计时器与互动强度相关数据。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..services.scheduler_service import (
    PAUSE_AT,
    PAUSE_FLAG,
    PAUSE_REASON,
    PAUSE_THRESHOLD,
    PAUSE_UNANSWERED,
)


class StateBuilder:
    """构建运行状态、任务卡片、会话摘要、强度面板与文档列表载荷。"""

    def __init__(
        self,
        plugin: Any,
        metadata_version: str,
        connection_counter: Any = None,
    ) -> None:
        self.plugin = plugin
        self._metadata_version = metadata_version
        # WebSocket 连接计数由实时通道提供，供状态载荷展示连接状态。
        self._connection_counter = connection_counter or (lambda: 0)

    # ------------------------------------------------------------------
    # 计时器元信息
    # ------------------------------------------------------------------
    def safe_timer_meta(self, timer: Any, now: float) -> dict[str, float | int | None]:
        """安全读取 asyncio 计时器的剩余时间与绝对目标时间。"""
        if timer is None:
            return {"remaining_seconds": None, "target_time": None}

        try:
            if getattr(timer, "cancelled", lambda: False)():
                return {"remaining_seconds": None, "target_time": None}
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

        when_method = getattr(timer, "when", None)
        if not callable(when_method):
            return {"remaining_seconds": None, "target_time": None}

        try:
            loop_time = when_method()
            loop = getattr(timer, "_loop", None)
            current_loop_time = loop.time() if loop else None
            if current_loop_time is None:
                return {"remaining_seconds": None, "target_time": None}

            remaining_precise = max(0.0, loop_time - current_loop_time)
            target_time = now + remaining_precise
            return {
                "remaining_seconds": max(0, int(math.ceil(remaining_precise))),
                "target_time": target_time,
            }
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

    def detect_session_category(self, session_id: str) -> str:
        """兼容旧前端字段：本插件所有会话都是私聊。"""
        return "friend"

    def is_private_umo(self, session_id: str) -> bool:
        try:
            parsed = self.plugin.session_service.parse_session_id(session_id)
        except Exception:
            return True
        if not parsed:
            return True
        return "friend" in str(parsed[1]).lower() or "private" in str(parsed[1]).lower()

    # ------------------------------------------------------------------
    # 计时器卡片
    # ------------------------------------------------------------------
    def collect_timer_cards(self, now: float) -> dict[str, list[dict[str, Any]]]:
        """收集私聊自动触发计时器卡片。"""
        plugin = self.plugin
        auto_cards: list[dict[str, Any]] = []

        for session_id, timer in list(plugin.auto_trigger_timers.items()):
            if not self.is_private_umo(str(session_id)):
                continue
            normalized_session_id = plugin.session_service.normalize_session_id(
                str(session_id)
            )
            session_config = plugin.config_service.get_session_config(session_id) or {}
            session_data = plugin.session_data.get(session_id, {})
            auto_settings = session_config.get("auto_trigger_settings", {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            trigger_delay_minutes = int(
                auto_settings.get("auto_trigger_after_minutes", 0) or 0
            )
            trigger_delay_seconds = max(0, trigger_delay_minutes * 60)
            timer_meta = self.safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            started_at = max(plugin.plugin_start_time, now - trigger_delay_seconds)
            progress_percent = 0
            if trigger_delay_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, trigger_delay_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / trigger_delay_seconds) * 100))
                )

            intensity = self._intensity_snapshot(normalized_session_id)
            auto_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": plugin.session_service.get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": "friend",
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "auto_trigger",
                    "title": "自动触发检测",
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at,
                    "window_seconds": trigger_delay_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "auto_trigger_after_minutes": trigger_delay_minutes,
                    "intensity_score": intensity.get("score"),
                    "intensity_tier": intensity.get("tier"),
                    "intensity_tier_label": intensity.get("tier_label"),
                    "intensity_probability_percent": intensity.get(
                        "probability_percent"
                    ),
                }
            )

        auto_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        return {"auto_trigger_cards": auto_cards}

    def _intensity_snapshot(self, session_id: str) -> dict[str, Any]:
        service = getattr(self.plugin, "intensity_service", None)
        if service is None:
            return {}
        try:
            return service.evaluate(session_id)
        except Exception:
            return {}

    def _memory_status(self) -> dict[str, Any]:
        bridge = getattr(self.plugin, "memory_bridge", None)
        if bridge is None:
            return {}
        try:
            return bridge.status()
        except Exception:
            return {}

    def collect_paused_sessions(self) -> list[dict[str, Any]]:
        """列出因连续未回复而暂停的私聊会话。"""
        plugin = self.plugin
        results: list[dict[str, Any]] = []
        for session_id in self.list_known_sessions():
            info = plugin.session_data.get(session_id)
            if not isinstance(info, dict) or not info.get(PAUSE_FLAG):
                continue

            session_config = (
                plugin.config_service.get_session_config(session_id) or {}
            )
            intensity = self._intensity_snapshot(session_id)
            paused_at = info.get(PAUSE_AT)
            pause_reason = str(info.get(PAUSE_REASON) or "")
            results.append(
                {
                    "session_id": session_id,
                    "session_name": plugin.session_service.get_session_name(
                        session_id, session_config
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        session_id, session_config
                    ),
                    "enabled": bool(session_config.get("enable", False)),
                    "paused_at": (
                        datetime.fromtimestamp(
                            float(paused_at), tz=getattr(plugin, "timezone", None)
                        ).isoformat()
                        if isinstance(paused_at, (int, float))
                        else None
                    ),
                    "pause_reason": pause_reason,
                    "pause_reason_label": (
                        "用户明确表示不想收到主动消息"
                        if pause_reason == "negative_feedback"
                        else "连续未回复"
                    ),
                    "cooldown_until": info.get("cooldown_until"),
                    "cooldown_remaining_seconds": (
                        int(
                            plugin.scheduler_service.cooldown_remaining(session_id)
                        )
                        if hasattr(plugin.scheduler_service, "cooldown_remaining")
                        else 0
                    ),
                    "last_reply_quality": intensity.get("last_reply_quality"),
                    "conversation_state_label": (
                        intensity.get("conversation_state") or {}
                    ).get("state_label"),
                    "paused_unanswered_count": int(
                        info.get(PAUSE_UNANSWERED, 0) or 0
                    ),
                    "paused_threshold": int(info.get(PAUSE_THRESHOLD, 0) or 0),
                    "unanswered_count": int(info.get("unanswered_count", 0) or 0),
                    "last_topic_judge": info.get("last_topic_judge") or {},
                    "intensity_score": intensity.get("score"),
                    "intensity_tier": intensity.get("tier"),
                    "intensity_tier_label": intensity.get("tier_label"),
                    "intensity_probability_percent": intensity.get(
                        "probability_percent"
                    ),
                }
            )

        results.sort(key=lambda item: str(item.get("paused_at") or ""), reverse=True)
        return results

    # ------------------------------------------------------------------
    # 通知与状态
    # ------------------------------------------------------------------
    async def build_notification_payload(self) -> dict[str, Any]:
        """构建通知载荷（通知中心不可用时返回空结构）。"""
        notification_center = getattr(self.plugin, "notification_center", None)
        if not notification_center:
            return {
                "items": [],
                "meta": {
                    "unread_count": 0,
                    "last_sync_at": None,
                    "total_count": 0,
                },
            }
        return await notification_center.get_payload()

    def build_intensity_summary(self) -> dict[str, Any]:
        service = getattr(self.plugin, "intensity_service", None)
        if service is None:
            return {}
        try:
            return service.summary(self.list_known_sessions())
        except Exception:
            return {}

    def build_status_payload(self) -> dict[str, Any]:
        """构建运行状态载荷（版本、运行时长、任务计数、计时器与强度概览）。"""
        plugin = self.plugin
        now = time.time()
        uptime_sec = max(0, int(now - plugin.plugin_start_time))
        timer_cards = self.collect_timer_cards(now)
        paused_sessions = self.collect_paused_sessions()

        return {
            "running": True,
            "version": getattr(plugin, "version", None)
            or getattr(plugin, "__version__", None)
            or self._metadata_version
            or "未知版本",
            "uptime_seconds": uptime_sec,
            "uptime": str(
                datetime.fromtimestamp(now)
                - datetime.fromtimestamp(plugin.plugin_start_time)
            ),
            "scheduler_running": bool(plugin.scheduler and plugin.scheduler.running),
            "sessions_count": len(self.list_known_sessions()),
            "auto_trigger_timers": len(plugin.auto_trigger_timers),
            "jobs_count": len(plugin.scheduler.get_jobs()) if plugin.scheduler else 0,
            "timer_cards_total": len(timer_cards["auto_trigger_cards"]),
            "auto_trigger_cards": timer_cards["auto_trigger_cards"],
            "paused_count": len(paused_sessions),
            "paused_sessions": paused_sessions,
            "intensity_summary": self.build_intensity_summary(),
            "memory_status": self._memory_status(),
            "ws_connections": self._connection_counter(),
            "timestamp": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # 任务与会话
    # ------------------------------------------------------------------
    def collect_jobs(self) -> list[dict[str, Any]]:
        """收集 APScheduler 中已注册的私聊主动消息任务摘要。"""
        plugin = self.plugin
        if not plugin.scheduler:
            return []

        jobs = []
        for job in plugin.scheduler.get_jobs():
            session_id = str(job.id)
            if not self.is_private_umo(session_id):
                continue
            session_data = plugin.session_data.get(session_id, {})
            session_config = plugin.config_service.get_session_config(session_id) or {}
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            auto_trigger_settings = session_config.get("auto_trigger_settings", {})
            intensity = self._intensity_snapshot(session_id)
            conversation_state = intensity.get("conversation_state") or {}
            rhythm = intensity.get("rhythm") or {}
            persona = intensity.get("persona") or {}
            conversation_memory = intensity.get("conversation_memory") or {}
            jobs.append(
                {
                    "id": session_id,
                    "session_name": plugin.session_service.get_session_name(
                        session_id, session_config
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        session_id, session_config
                    ),
                    "session_category": "friend",
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times",
                        auto_trigger_settings.get("max_unanswered_times", 0),
                    ),
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "manual_trigger_in_progress": session_id
                    in plugin.manual_trigger_sessions,
                    "next_trigger_time": session_data.get("next_trigger_time"),
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                    "last_schedule_intensity_score": session_data.get(
                        "last_schedule_intensity_score"
                    ),
                    "schedule_min_interval_minutes": schedule_settings.get(
                        "min_interval_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_interval_minutes"
                    ),
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                    "intensity_score": intensity.get("score"),
                    "intensity_tier": intensity.get("tier"),
                    "intensity_tier_label": intensity.get("tier_label"),
                    "intensity_probability_percent": intensity.get(
                        "probability_percent"
                    ),
                    "intensity_effective_interval_minutes": intensity.get(
                        "effective_interval_minutes"
                    ),
                    "conversation_state": conversation_state.get("state"),
                    "conversation_state_label": conversation_state.get(
                        "state_label"
                    ),
                    "last_intent": conversation_state.get("last_intent"),
                    "last_intent_label": conversation_state.get(
                        "last_intent_label"
                    ),
                    "last_reply_quality": intensity.get("last_reply_quality"),
                    "persona_mood_label": persona.get("mood_label"),
                    "persona_energy_percent": persona.get("energy_percent"),
                    "rhythm_active_hours_label": rhythm.get(
                        "active_hours_label"
                    ),
                    "rhythm_daily_count": rhythm.get("daily_count"),
                    "rhythm_daily_limit": rhythm.get("daily_limit"),
                    "open_loop_count": conversation_memory.get("open_loop_count", 0),
                    "profile_summary": conversation_memory.get("profile_summary", ""),
                }
            )
        return jobs

    def list_known_sessions(self) -> list[str]:
        """汇总私聊配置、会话数据与差异配置中出现过的所有私聊会话。"""
        plugin = self.plugin
        sessions: set[str] = set()

        private_settings = plugin.config_service.get_private_settings()
        for session in private_settings.get("session_list", []) or []:
            if isinstance(session, str) and session:
                normalized = plugin.session_service.normalize_session_id(session)
                if self.is_private_umo(normalized):
                    sessions.add(normalized)

        for session in plugin.session_data.keys():
            if self.is_private_umo(session):
                sessions.add(session)

        for session in plugin.session_override_manager.list_sessions():
            if self.is_private_umo(session):
                sessions.add(session)

        return sorted(sessions)

    def list_known_session_summaries(self) -> list[dict[str, Any]]:
        """返回带展示信息与强度摘要的已知私聊会话。"""
        plugin = self.plugin
        result: list[dict[str, Any]] = []
        for session in self.list_known_sessions():
            effective = plugin.config_service.get_session_config(session)
            intensity = self._intensity_snapshot(session)
            result.append(
                {
                    "session": session,
                    "session_name": plugin.session_service.get_session_name(
                        session, effective
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        session, effective
                    ),
                    "has_override": bool(
                        plugin.session_override_manager.get_override(session)
                    ),
                    "unanswered_count": plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                    "manual_trigger_in_progress": session
                    in plugin.manual_trigger_sessions,
                    "paused": bool(
                        (plugin.session_data.get(session) or {}).get(PAUSE_FLAG)
                    ),
                    "paused_at": (plugin.session_data.get(session) or {}).get(
                        PAUSE_AT
                    ),
                    "paused_unanswered_count": int(
                        (plugin.session_data.get(session) or {}).get(
                            PAUSE_UNANSWERED, 0
                        )
                        or 0
                    ),
                    "intensity_score": intensity.get("score"),
                    "intensity_tier": intensity.get("tier"),
                    "intensity_tier_label": intensity.get("tier_label"),
                    "intensity_probability_percent": intensity.get(
                        "probability_percent"
                    ),
                    "conversation_state_label": (
                        intensity.get("conversation_state") or {}
                    ).get("state_label"),
                    "last_intent_label": (
                        intensity.get("conversation_state") or {}
                    ).get("last_intent_label"),
                    "last_reply_quality": intensity.get("last_reply_quality"),
                }
            )
        return result

    def collect_intensity_sessions(self) -> list[dict[str, Any]]:
        """构建互动强度面板数据。"""
        service = getattr(self.plugin, "intensity_service", None)
        if service is None:
            return []

        snapshots = service.collect_snapshots(self.list_known_sessions())
        for item in snapshots:
            session_id = item.get("session_id", "")
            session_config = (
                self.plugin.config_service.get_session_config(session_id) or {}
            )
            item["enabled"] = bool(session_config.get("enable", False))
        return snapshots

    # ------------------------------------------------------------------
    # Markdown 文档
    # ------------------------------------------------------------------
    def list_markdown_documents(self) -> list[dict[str, Any]]:
        """列出允许浏览的 Markdown 文档摘要。"""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        plugin_root = Path(__file__).resolve().parents[2].resolve()
        docs_root = (plugin_root / "docs").resolve()

        allowed_paths: list[Path] = []
        if plugin_root.exists():
            allowed_paths.extend(sorted(plugin_root.glob("*.md")))
        if docs_root.exists():
            allowed_paths.extend(sorted(docs_root.rglob("*.md")))

        for path in allowed_paths:
            if not path.is_file():
                continue
            try:
                relative_path = self.to_workspace_relative_path(path)
            except ValueError:
                continue
            normalized = relative_path.replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)
            items.append(
                {
                    "path": normalized,
                    "title": path.stem,
                    "filename": path.name,
                    "category": "root"
                    if path.parent.resolve() == plugin_root
                    else path.parent.name,
                }
            )

        items.sort(
            key=lambda item: (
                0 if item["path"].count("/") == 0 else 1,
                item["path"].lower(),
            )
        )
        return items

    def resolve_markdown_document(self, raw_path: str) -> Path | None:
        """将前端请求的 Markdown 相对路径解析为插件目录中的受信任文件。"""
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if not normalized or not normalized.lower().endswith(".md"):
            return None
        if (
            normalized.startswith("/")
            or normalized.startswith("../")
            or "/../" in normalized
        ):
            return None

        plugin_root = Path(__file__).resolve().parents[2].resolve()
        docs_root = (plugin_root / "docs").resolve()
        candidate = (plugin_root / normalized).resolve()

        if not candidate.is_file():
            return None

        try:
            relative_path = candidate.relative_to(plugin_root)
        except ValueError:
            return None

        if relative_path.parent == Path("."):
            return candidate

        try:
            candidate.relative_to(docs_root)
            return candidate
        except ValueError:
            return None

    def to_workspace_relative_path(self, path: Path) -> str:
        """将绝对路径转换为插件工作区内的相对路径。"""
        plugin_root = Path(__file__).resolve().parents[2].resolve()
        return str(path.resolve().relative_to(plugin_root)).replace("\\", "/")
