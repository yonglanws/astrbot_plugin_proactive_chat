"""Web 控制台令牌鉴权。

由旧 core/web_admin_server.py 的令牌逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable

from astrbot.api import logger


class TokenAuth:
    """负责登录令牌的签发、校验与过期清理。"""

    DEFAULT_MAX_TOKENS = 256

    def __init__(
        self,
        auth_enabled_provider: Callable[[], bool] | bool,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        # 鉴权启用状态提供者：优先传回调动态读取当前密码，改密码后即时生效。
        self._auth_enabled_provider = auth_enabled_provider
        # 令牌有效期：24 小时。
        self._token_expire_seconds = 60 * 60 * 24
        # 令牌表容量上限，防止长期运行或高频登录导致内存无限增长。
        self._max_tokens = max(1, int(max_tokens))
        # token -> 过期时间戳 的映射（保持插入顺序以支持最旧淘汰）。
        self._tokens: dict[str, float] = {}
        self._token_cleanup_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        # 每次读取都按当前配置重新计算，避免改密码后需重启才生效。
        provider = self._auth_enabled_provider
        try:
            result = provider() if callable(provider) else provider
        except Exception:
            return False
        return bool(result)

    def _evict_if_needed(self, now: float) -> None:
        """先清理过期令牌，若仍超出容量上限则淘汰最早签发的令牌。"""
        expired = [k for k, exp in list(self._tokens.items()) if now > exp]
        for k in expired:
            self._tokens.pop(k, None)

        while len(self._tokens) >= self._max_tokens:
            oldest_key = next(iter(self._tokens), None)
            if oldest_key is None:
                break
            self._tokens.pop(oldest_key, None)

    def issue_token(self) -> str:
        """签发一个新的随机令牌并记录其过期时间。"""
        now = time.time()
        self._evict_if_needed(now)
        token = secrets.token_urlsafe(24)
        self._tokens[token] = now + self._token_expire_seconds
        return token

    def verify_token(self, token: str) -> bool:
        """校验令牌是否有效；过期令牌顺手清理。"""
        if not token:
            return False
        if token == "no-auth":
            # 固定占位令牌只在未启用鉴权时放行；启用后必须走真实令牌校验。
            return not self.enabled
        expire_at = self._tokens.get(token)
        if not expire_at:
            return False
        if time.time() > expire_at:
            self._tokens.pop(token, None)
            return False
        return True

    async def _cleanup_tokens_loop(self) -> None:
        """后台循环：每小时清理一次过期令牌。"""
        while True:
            try:
                await asyncio.sleep(3600)
                now = time.time()
                expired = [k for k, v in list(self._tokens.items()) if now > v]
                for k in expired:
                    self._tokens.pop(k, None)
                if expired:
                    logger.debug(f"[主动消息] 已清理 {len(expired)} 个过期令牌喵。")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[主动消息] 清理过期令牌异常喵: {e}")

    def start_cleanup(self) -> None:
        """启动令牌清理任务。

        鉴权状态可能在运行期由关闭变为开启（改密码），
        故无条件启动清理循环，仅负责清理过期令牌，开销可忽略。
        """
        if self._token_cleanup_task is None or self._token_cleanup_task.done():
            task = asyncio.create_task(self._cleanup_tokens_loop())
            self._token_cleanup_task = task

            def _clear_done_ref(done_task: asyncio.Task[None]) -> None:
                if self._token_cleanup_task is done_task:
                    self._token_cleanup_task = None

            task.add_done_callback(_clear_done_ref)

    async def stop_cleanup(self) -> None:
        """取消并等待令牌清理任务退出。"""
        task = self._token_cleanup_task
        self._token_cleanup_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[主动消息] 停止令牌清理任务异常喵: {e}")
