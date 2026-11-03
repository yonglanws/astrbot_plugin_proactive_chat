"""外部长期记忆插件桥接（当前适配 LivingMemory）。

本插件不重复实现长期记忆，而是以鸭子类型方式复用已安装的记忆插件：
- 生成主动消息前按当前会话召回相关记忆；
- 主动消息成功发送后，把 Bot 这句话写回记忆插件自己的会话库。

所有访问都是可选的：未安装、未初始化或接口不兼容时静默降级，
不会影响主动消息主流程。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Mapping

from astrbot.api import logger

from ..domain.humanization import MemoryIntegrationSettings

MEMORY_CONTEXT_MAX_CHARS = 1800
MEMORY_ITEM_MAX_CHARS = 300
_PLUGIN_CACHE_SECONDS = 30.0


class MemoryBridge:
    """LivingMemory 等外部记忆插件的可选桥接层。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._plugin_ref: Any = None
        self._plugin_checked_at = 0.0

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
    ) -> MemoryIntegrationSettings:
        raw: Any = None
        if isinstance(session_config, Mapping):
            humanization = session_config.get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("memory_integration_settings")
        if not isinstance(raw, Mapping):
            humanization = self.get_private_settings().get("humanization_settings")
            if isinstance(humanization, Mapping):
                raw = humanization.get("memory_integration_settings")
        return MemoryIntegrationSettings.from_dict(raw)

    # ------------------------------------------------------------------
    # 插件发现
    # ------------------------------------------------------------------
    async def _get_all_stars(self) -> list[Any]:
        context = getattr(self.plugin, "context", None)
        getter = getattr(context, "get_all_stars", None)
        if callable(getter):
            try:
                stars = getter()
                if inspect.isawaitable(stars):
                    stars = await stars
                if isinstance(stars, Mapping):
                    return list(stars.values())
                if isinstance(stars, (list, tuple)):
                    return list(stars)
            except Exception as e:
                logger.debug(f"[主动消息] 枚举插件失败喵: {e}")

        # 兼容旧版 AstrBot 的 star_manager 结构。
        manager = getattr(context, "star_manager", None)
        for attr in ("star_insts", "stars", "get_all_stars"):
            value = getattr(manager, attr, None)
            if callable(value):
                try:
                    value = value()
                    if inspect.isawaitable(value):
                        value = await value
                except Exception:
                    continue
            if isinstance(value, Mapping):
                return list(value.values())
            if isinstance(value, (list, tuple)):
                return list(value)
        return []

    async def resolve_plugin(self, *, force: bool = False) -> Any:
        """定位外部记忆插件实例；结果短时缓存。"""
        now = time.time()
        if (
            not force
            and self._plugin_ref is not None
            and now - self._plugin_checked_at < _PLUGIN_CACHE_SECONDS
        ):
            return self._plugin_ref
        if (
            not force
            and now - self._plugin_checked_at < _PLUGIN_CACHE_SECONDS
            and self._plugin_ref is None
        ):
            return None

        settings = self.get_settings()
        target = settings.plugin_id or "astrbot_plugin_livingmemory"
        stars = await self._get_all_stars()
        for entry in stars:
            if entry is None:
                continue
            # AstrBot 的 get_all_stars() 返回 StarMetadata；真正插件实例在
            # star_cls（部分版本叫 instance）里。也兼容直接返回实例的版本。
            candidate = (
                getattr(entry, "star_cls", None)
                or getattr(entry, "instance", None)
                or entry
            )
            if inspect.isclass(candidate):
                # 只有类、没有实例时无法安全调用其运行时方法。
                continue
            if not (
                hasattr(candidate, "initializer")
                or hasattr(candidate, "memory_engine")
                or hasattr(candidate, "conversation_manager")
            ):
                continue
            module = str(getattr(type(candidate), "__module__", "") or "")
            class_name = str(getattr(type(candidate), "__name__", "") or "")
            module_path = str(
                getattr(entry, "module_path", "")
                or getattr(entry, "root_dir_name", "")
                or ""
            )
            if (
                target in module_path
                or target in module
                or "livingmemory" in module_path.lower()
                or "livingmemory" in module.lower()
                or class_name == "LivingMemoryPlugin"
            ):
                self._plugin_ref = candidate
                self._plugin_checked_at = now
                logger.info("[主动消息] 已连接外部长期记忆插件喵。")
                return candidate

        self._plugin_ref = None
        self._plugin_checked_at = now
        return None

    def _memory_engine(self, star: Any) -> Any:
        if star is None:
            return None
        initializer = getattr(star, "initializer", None)
        for owner in (initializer, star):
            engine = getattr(owner, "memory_engine", None)
            if engine is not None:
                return engine
        return None

    def _conversation_manager(self, star: Any) -> Any:
        if star is None:
            return None
        initializer = getattr(star, "initializer", None)
        for owner in (initializer, star):
            manager = getattr(owner, "conversation_manager", None)
            if manager is not None:
                return manager
        return None

    async def is_available(self) -> bool:
        star = await self.resolve_plugin()
        return self._memory_engine(star) is not None

    # ------------------------------------------------------------------
    # 召回
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_scope(
        star: Any, session_id: str, persona_id: str | None
    ) -> tuple[str | None, str | None]:
        """按 LivingMemory 的过滤配置解析会话作用域与人格过滤。"""
        config = getattr(star, "config_manager", None)
        getter = getattr(config, "get", None)

        def _get(key: str, default: Any) -> Any:
            if callable(getter):
                try:
                    return getter(key, default)
                except TypeError:
                    return getter(key)
            return default

        mode = str(_get("filtering_settings.memory_scope_mode", "legacy")).lower()
        use_session = bool(_get("filtering_settings.use_session_filtering", True))
        use_persona = bool(_get("filtering_settings.use_persona_filtering", True))

        if mode == "session":
            scope: str | None = session_id
        elif mode == "global":
            scope = "livingmemory:global"
        elif mode == "user":
            parts = str(session_id).split(":")
            platform = parts[0] if parts else "unknown"
            target = parts[-1] if parts else ""
            identity = target
            aliases_raw = str(
                _get("access_control.identity_aliases", "") or ""
            )
            alias_candidates = {
                f"{platform.lower()}:{target}".casefold(),
                target.casefold(),
            }
            for line in aliases_raw.splitlines():
                source, separator, alias = line.partition("=")
                if separator and source.strip().casefold() in alias_candidates:
                    identity = alias.strip() or identity
                    break
            scope = f"livingmemory:user:{platform.lower()}:{identity}"
        elif use_session:
            scope = session_id
        else:
            scope = None
        return scope, (persona_id if use_persona else None)

    async def recall(
        self,
        session_id: str,
        query: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        persona_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """召回与当前会话相关的长期记忆。"""
        settings = self.get_settings(session_config)
        if not settings.enabled or not str(query or "").strip():
            return []
        if settings.top_k <= 0 or not settings.inject_into_prompt:
            return []

        star = await self.resolve_plugin()
        engine = self._memory_engine(star)
        if engine is None:
            return []

        recall_session_id, recall_persona_id = self._resolve_scope(
            star, session_id, persona_id
        )
        try:
            results = await asyncio.wait_for(
                engine.search_memories(
                    query=str(query).strip()[:1000],
                    k=settings.top_k,
                    session_id=recall_session_id,
                    persona_id=recall_persona_id,
                ),
                timeout=settings.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[主动消息] 长期记忆召回超时（{settings.timeout_seconds:g}s），"
                f"本次跳过记忆注入喵。"
            )
            return []
        except Exception as e:
            logger.warning(f"[主动消息] 长期记忆召回失败喵: {e}")
            return []

        memories: list[dict[str, Any]] = []
        for item in results or []:
            if isinstance(item, Mapping):
                content = item.get("content") or item.get("text")
                score = item.get("final_score") or item.get("score")
                metadata = item.get("metadata") or {}
            else:
                content = getattr(item, "content", None)
                score = getattr(item, "final_score", None)
                metadata = getattr(item, "metadata", None)
            text = str(content or "").strip()
            if not text:
                continue
            metadata = metadata if isinstance(metadata, Mapping) else {}
            try:
                importance = float(metadata.get("importance", 0.5) or 0.5)
            except (TypeError, ValueError):
                importance = 0.5
            memories.append(
                {
                    "content": text[:MEMORY_ITEM_MAX_CHARS],
                    "score": (
                        round(float(score), 3)
                        if isinstance(score, (int, float))
                        else None
                    ),
                    "importance": round(max(0.0, min(1.0, importance)), 2),
                    "create_time": metadata.get("create_time"),
                }
            )
            if len(memories) >= settings.top_k:
                break

        if memories:
            logger.info(
                f"[主动消息] 已召回 {len(memories)} 条长期记忆喵。"
            )
        return memories

    @staticmethod
    def format_memories(memories: list[dict[str, Any]]) -> str:
        """把召回结果格式化为提示词中的只读事实块。"""
        if not memories:
            return ""
        lines = [
            "<long_term_memory>",
            "以下是与当前私聊对象相关的长期记忆，仅作为事实参考：",
            "- 不要逐条复述、不要暴露“记忆库”概念；只在自然合适时引用。",
            "- 如果与最近聊天冲突，以最近聊天为准。",
        ]
        used = 0
        for item in memories:
            text = str(item.get("content") or "").strip()
            if not text:
                continue
            importance = item.get("importance")
            prefix = (
                f"[重要度 {importance:.1f}] " if isinstance(importance, (int, float)) else ""
            )
            line = f"- {prefix}{text}"
            if used + len(line) > MEMORY_CONTEXT_MAX_CHARS:
                break
            lines.append(line)
            used += len(line)
        lines.append("</long_term_memory>")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 写回
    # ------------------------------------------------------------------
    async def store_proactive(
        self,
        session_id: str,
        text: str,
        *,
        session_config: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> bool:
        """把已发送的主动消息写入外部记忆插件的会话库。"""
        settings = self.get_settings(session_config)
        if not settings.enabled or not settings.write_proactive:
            return False
        content = str(text or "").strip()
        if not content:
            return False

        star = await self.resolve_plugin()
        manager = self._conversation_manager(star)
        if manager is None:
            return False

        # 避免同一句话因为重试/重复收尾被写两次。
        try:
            last = await manager.get_last_message(session_id)
            if (
                last is not None
                and getattr(last, "role", "") == "assistant"
                and str(getattr(last, "content", "") or "").strip() == content
            ):
                return False
        except Exception:
            pass

        platform = str(session_id).split(":", 1)[0] if ":" in str(session_id) else "unknown"
        try:
            await asyncio.wait_for(
                manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=content,
                    sender_id="bot",
                    sender_name="Bot",
                    platform=platform,
                    is_bot_message=True,
                    metadata={
                        "source": "proactive_chat",
                        "memory_origin": "proactive",
                    },
                    timestamp=float(now if now is not None else time.time()),
                ),
                timeout=settings.timeout_seconds,
            )
            logger.info("[主动消息] 已把本次主动消息写入长期记忆会话库喵。")
            return True
        except asyncio.TimeoutError:
            logger.warning("[主动消息] 写入长期记忆超时喵。")
        except Exception as e:
            logger.warning(f"[主动消息] 写入长期记忆失败喵: {e}")
        return False

    def status(self, session_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """同步状态快照，供管理端展示（不触发插件枚举）。"""
        settings = self.get_settings(session_config)
        star = self._plugin_ref
        return {
            "enabled": settings.enabled,
            "available": self._memory_engine(star) is not None,
            "plugin_id": settings.plugin_id,
            "top_k": settings.top_k,
            "inject_into_prompt": settings.inject_into_prompt,
            "write_proactive": settings.write_proactive,
        }


__all__ = ["MemoryBridge"]
