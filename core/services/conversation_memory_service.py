"""短期工作记忆：用户画像、开放话题与话题账本。

与 LivingMemory 的分工：
- LivingMemory 负责长期事实（偏好、人物、计划、历史约定）；
- 本服务负责“最近聊到哪、有什么还没闭环”，并把它注入提示词，
  让主动消息能自然跟进，而不是每次重新问“在干嘛”。

状态保存在 ``session_data[umo]["conversation_memory"]``。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..domain.conversation_memory import (
    close_answered_questions,
    extract_loop_candidates,
    format_open_loops,
    loop_matches_text,
    prune_loops,
    profile_summary,
    update_profile,
)
from ..domain.humanization import ConversationMemorySettings, ReplyQuality

CONVERSATION_MEMORY_KEY = "conversation_memory"
STATE_VERSION = 1
MAX_STORED_LOOPS = 40


class ConversationMemoryService:
    """开放话题与轻量画像服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def get_private_settings(self) -> dict[str, Any]:
        config = getattr(self.plugin, "config", {}) or {}
        for key in ("private_settings", "friend_settings"):
            value = config.get(key) if hasattr(config, "get") else None
            if isinstance(value, dict):
                return value
        return {}

    def get_settings(
        self, session_config: Mapping[str, Any] | None = None
    ) -> ConversationMemorySettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            humanization = session_config.get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("conversation_memory_settings")
        if not isinstance(raw, Mapping):
            humanization = self.get_private_settings().get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("conversation_memory_settings")
        return ConversationMemorySettings.from_dict(raw)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "profile": {},
            "open_loops": [],
            "recent_topics": [],
            "last_updated": 0.0,
        }

    def get_state(self, session_id: str, *, create: bool = False) -> dict[str, Any]:
        session_data = getattr(self.plugin, "session_data", None)
        if not isinstance(session_data, dict):
            return self.default_state()
        payload = session_data.get(session_id)
        if not isinstance(payload, dict):
            if not create:
                return self.default_state()
            payload = {}
            session_data[session_id] = payload
        state = payload.get(CONVERSATION_MEMORY_KEY)
        if not isinstance(state, dict):
            if not create:
                return self.default_state()
            state = self.default_state()
            payload[CONVERSATION_MEMORY_KEY] = state
        if not isinstance(state.get("profile"), dict):
            state["profile"] = {}
        if not isinstance(state.get("open_loops"), list):
            state["open_loops"] = []
        if not isinstance(state.get("recent_topics"), list):
            state["recent_topics"] = []
        return state

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def _add_loops(
        self,
        state: dict[str, Any],
        text: str,
        *,
        role: str,
        now: float,
        settings: ConversationMemorySettings,
    ) -> None:
        existing = {
            str(loop.get("fingerprint") or "")
            for loop in state.get("open_loops", [])
            if isinstance(loop, dict)
        }
        for candidate in extract_loop_candidates(text, role=role, now=now):
            fingerprint = str(candidate.get("fingerprint") or "")
            if not fingerprint or fingerprint in existing:
                continue
            state["open_loops"].append(candidate)
            existing.add(fingerprint)

    def _add_recent_topic(
        self,
        state: dict[str, Any],
        text: str,
        *,
        role: str,
        now: float,
        settings: ConversationMemorySettings,
    ) -> None:
        content = " ".join(str(text or "").split())[:160]
        if not content:
            return
        topics = state.setdefault("recent_topics", [])
        if isinstance(topics, list):
            topics.append({"text": content, "role": role, "at": now})
            keep = max(0, int(settings.max_recent_topics))
            if keep <= 0:
                topics.clear()
            else:
                del topics[:-keep]

    def record_user_message(
        self,
        session_id: str,
        text: str,
        quality: ReplyQuality,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        if not settings.enabled:
            return {}
        state = self.get_state(session_id, create=True)

        loops = state.get("open_loops", [])
        # 用户实质回复后，关闭更早的助手提问。
        close_answered_questions(
            loops,
            after_ts=timestamp,
            quality=quality.value,
            now=timestamp,
        )
        # 用户明确回应了某个开放话题，也标记闭环。
        for loop in loops:
            if not isinstance(loop, dict) or loop.get("status") != "open":
                continue
            if loop_matches_text(loop, text):
                loop["status"] = "closed"
                loop["closed_at"] = timestamp
                loop["close_reason"] = "user_mentioned"
                loop["last_mentioned"] = timestamp

        if settings.profile_enabled:
            state["profile"] = update_profile(
                state.get("profile", {}),
                text,
                quality=quality.value,
                now=timestamp,
            )
        self._add_loops(
            state, text, role="user", now=timestamp, settings=settings
        )
        self._add_recent_topic(
            state, text, role="user", now=timestamp, settings=settings
        )
        state["open_loops"] = prune_loops(
            [loop for loop in loops if isinstance(loop, dict)],
            now=timestamp,
            ttl_days=settings.ttl_days,
            max_open=settings.max_open_loops,
        )
        state["last_updated"] = timestamp
        return self.snapshot(
            session_id, session_config=session_config, now=timestamp
        )

    def record_proactive(
        self,
        session_id: str,
        text: str,
        intent: str = "",
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> None:
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        if not settings.enabled:
            return
        state = self.get_state(session_id, create=True)
        self._add_loops(
            state, text, role="assistant", now=timestamp, settings=settings
        )
        self._add_recent_topic(
            state, text, role="assistant", now=timestamp, settings=settings
        )
        if intent:
            topics = state.get("recent_topics", [])
            if isinstance(topics, list) and topics:
                topics[-1]["intent"] = intent
        state["open_loops"] = prune_loops(
            [loop for loop in state.get("open_loops", []) if isinstance(loop, dict)],
            now=timestamp,
            ttl_days=settings.ttl_days,
            max_open=settings.max_open_loops,
        )
        state["last_updated"] = timestamp

    def seed_from_history(
        self,
        session_id: str,
        history: list,
        *,
        now: float | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> None:
        """首次接入时用最近对话补一份工作记忆（幂等）。"""
        state = self.get_state(session_id)
        if state.get("open_loops") or state.get("recent_topics"):
            return
        timestamp = float(now if now is not None else time.time())
        messages = list(history or [])[-20:]
        for index, item in enumerate(messages):
            if isinstance(item, dict):
                role = str(item.get("role") or "")
                content = item.get("content")
            else:
                role = str(getattr(item, "role", "") or "")
                content = getattr(item, "content", None)
            text = str(content or "").strip()
            if not text or role not in {"user", "assistant"}:
                continue
            at = timestamp - (len(messages) - index) * 120.0
            if role == "assistant":
                self.record_proactive(
                    session_id,
                    text,
                    now=at,
                    session_config=session_config,
                )
            else:
                from ..domain.humanization import classify_reply

                quality = classify_reply(text)
                self.record_user_message(
                    session_id,
                    text,
                    quality,
                    now=at,
                    session_config=session_config,
                )

    # ------------------------------------------------------------------
    # 快照与提示词
    # ------------------------------------------------------------------
    def snapshot(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        settings = self.get_settings(session_config)
        state = self.get_state(session_id)
        stored_loops = [
            loop
            for loop in state.get("open_loops", [])
            if isinstance(loop, dict)
        ]
        stored_loops = prune_loops(
            stored_loops,
            now=timestamp,
            ttl_days=settings.ttl_days,
            max_open=settings.max_open_loops,
        )
        state["open_loops"] = stored_loops
        loops = [
            loop
            for loop in stored_loops
            if loop.get("status") == "open"
        ]
        loops.sort(
            key=lambda item: float(item.get("created_at", 0) or 0), reverse=True
        )
        topic_list = list(state.get("recent_topics", []))
        keep_topics = max(0, settings.max_recent_topics)
        recent_topics = topic_list[-keep_topics:] if keep_topics else []
        return {
            "enabled": settings.enabled,
            "profile": dict(state.get("profile", {})),
            "profile_summary": profile_summary(state.get("profile", {})),
            "open_loops": loops[: max(0, settings.max_open_loops)],
            "open_loop_count": len(loops),
            "recent_topics": recent_topics,
            "last_updated": state.get("last_updated"),
            "followup_weight": settings.followup_weight,
        }

    def format_for_prompt(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> str:
        snapshot = self.snapshot(
            session_id, session_config=session_config, now=now
        )
        return format_open_loops(
            snapshot.get("open_loops", []),
            now=float(now if now is not None else time.time()),
        )

    def open_loop_count(
        self,
        session_id: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> int:
        return int(
            self.snapshot(
                session_id, session_config=session_config, now=now
            ).get("open_loop_count", 0)
            or 0
        )


__all__ = ["CONVERSATION_MEMORY_KEY", "ConversationMemoryService"]
