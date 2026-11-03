"""AstrBot 插件可视化视图（views/）后端 API。

AstrBot 的插件页面运行在受限 iframe 中，通过 ``window.AstrBotPluginView``
bridge 调用插件注册的 Web API。这里把独立 Web 管理端已有的能力，以
``context.register_web_api()`` 的形式重新暴露给插件页面，避免页面直接
访问 FastAPI 端口、Dashboard Cookie 或 localStorage。

设计约束：
- 仅依赖 ``astrbot.api.web`` 的稳定接口；旧版本 AstrBot 没有该模块时
  自动跳过注册，不影响插件主体与独立 Web 管理端。
- 路由统一以插件名开头，页面侧 endpoint 不需要带前缀。
- 页面通过轮询刷新状态，因此这里不依赖 WebSocket。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ...utils.version_utils import get_plugin_version
from ..domain.domain_enums import TriggerSource
from .state_builder import StateBuilder

PLUGIN_NAME = "astrbot_plugin_proactive_chat"

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover
    error_response = None  # type: ignore[assignment]
    json_response = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]

# 页面 API 只暴露这些后端路由；路径与前端 bridge 适配器一一对应。
_ROUTE_DESCRIPTIONS = {
    "status": "插件运行状态",
    "config": "读取或保存插件配置",
    "config-schema": "读取配置 Schema",
    "sessions": "列出已知私聊会话",
    "session-config": "读取或保存会话差异配置",
    "jobs": "列出主动消息调度任务",
    "jobs/reschedule": "重新安排下一次主动消息",
    "jobs/trigger": "立即触发一次主动消息",
    "jobs/resume": "解除未回复暂停",
    "jobs/cancel": "取消会话调度",
    "intensity": "互动强度快照",
    "notifications": "通知列表",
    "notifications/read": "标记通知已读",
    "notifications/read-all": "全部通知已读",
    "notifications/refresh": "刷新远端通知",
    "markdown-files": "Markdown 文档列表",
    "markdown-file": "读取 Markdown 文档",
    "open-directory": "打开插件目录",
    "auth-info": "插件页面鉴权信息",
}


def _is_running_in_docker() -> bool:
    """检测是否运行在容器中；容器内无法直接打开宿主机文件管理器。"""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            content = cgroup_path.read_text(encoding="utf-8", errors="ignore")
            if "/docker/" in content or "/kubepods/" in content:
                return True
    except Exception:
        pass
    return os.environ.get("DOCKER_CONTAINER") == "true"


class PageApiRoutes:
    """把管理端能力注册为 AstrBot 插件 Web API。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.state = StateBuilder(plugin, get_plugin_version(default="未知版本"))
        self.registered = False
        self._register_error: str | None = None

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self) -> bool:
        """注册页面 API；不支持插件页面时返回 False。"""
        if self.registered:
            return True
        if request is None or json_response is None or error_response is None:
            self._register_error = "当前 AstrBot 版本缺少 astrbot.api.web"
            logger.info(
                "[主动消息] 当前 AstrBot 不支持插件页面 Web API，已跳过页面后端注册喵。"
            )
            return False

        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            self._register_error = "当前 AstrBot 版本缺少 register_web_api"
            logger.info(
                "[主动消息] 当前 AstrBot 不支持 context.register_web_api，已跳过页面后端注册喵。"
            )
            return False

        routes: list[tuple[str, Any, list[str]]] = [
            ("status", self.get_status, ["GET"]),
            ("config", self.get_config, ["GET"]),
            ("config", self.update_config, ["POST"]),
            ("config-schema", self.get_config_schema, ["GET"]),
            ("sessions", self.list_sessions, ["GET"]),
            ("session-config", self.get_session_config, ["GET"]),
            ("session-config", self.update_session_config, ["POST"]),
            ("session-config/delete", self.reset_session_config, ["POST"]),
            ("session-config", self.reset_session_config, ["DELETE"]),
            ("jobs", self.list_jobs, ["GET"]),
            ("jobs/reschedule", self.reschedule_job, ["POST"]),
            ("jobs/trigger", self.trigger_job, ["POST"]),
            ("jobs/resume", self.resume_job, ["POST"]),
            ("jobs/cancel", self.cancel_job, ["POST"]),
            ("intensity", self.list_intensity, ["GET"]),
            ("notifications", self.get_notifications, ["GET"]),
            ("notifications/read", self.mark_notification_read, ["POST"]),
            ("notifications/read-all", self.mark_all_notifications_read, ["POST"]),
            ("notifications/refresh", self.refresh_notifications, ["POST"]),
            ("markdown-files", self.list_markdown_files, ["GET"]),
            ("markdown-file", self.get_markdown_file, ["GET"]),
            ("open-directory", self.open_directory, ["POST"]),
            ("auth-info", self.auth_info, ["GET"]),
        ]

        for suffix, handler, methods in routes:
            route = f"/{PLUGIN_NAME}/{suffix}"
            try:
                register(
                    route,
                    handler,
                    methods,
                    _ROUTE_DESCRIPTIONS.get(suffix, suffix),
                )
            except Exception as e:
                self._register_error = str(e)
                logger.error(
                    f"[主动消息] 注册插件页面 API 失败喵: {route} -> {e}"
                )
                return False

        self.registered = True
        logger.info("[主动消息] 已注册 AstrBot 插件页面 API 喵。")
        return True

    def status_snapshot(self) -> dict[str, Any]:
        """供管理端展示页面 API 是否可用。"""
        return {
            "supported": request is not None,
            "registered": self.registered,
            "error": self._register_error,
        }

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    async def _broadcast(self, reason: str) -> None:
        """通知独立 Web 管理端刷新；AstrBot 页面采用轮询，无需广播。"""
        web = getattr(self.plugin, "web_admin_server", None)
        broadcast = getattr(web, "_broadcast_update", None)
        if callable(broadcast):
            try:
                await broadcast(reason)
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.debug(f"[主动消息] 页面 API 广播失败喵: {exc}")

    @staticmethod
    def _error(message: str, status_code: int = 400):
        return error_response(message, status_code=status_code)

    @staticmethod
    def _query_umo() -> str:
        value = request.query.get("umo") if request is not None else None
        return str(value or "").strip()

    def _normalize(self, umo: str) -> str:
        return self.plugin.session_service.normalize_session_id(umo)

    def _config_group(self, key: str) -> dict[str, Any]:
        value = self.plugin.config.get(key)
        return dict(value) if isinstance(value, dict) else {}

    # ------------------------------------------------------------------
    # 状态 / 配置
    # ------------------------------------------------------------------
    async def get_status(self):
        return json_response(self.state.build_status_payload())

    async def auth_info(self):
        # 插件页面已经由 Dashboard 完成鉴权，页面内不需要再次输入密码。
        return json_response({"auth_required": False, "embedded": True})

    async def get_config(self):
        web_admin = {
            key: value
            for key, value in self._config_group("web_admin").items()
            if key != "password"
        }
        private_settings = self._config_group("private_settings")
        if not private_settings:
            private_settings = self._config_group("friend_settings")
        return json_response(
            {
                "private_settings": private_settings,
                "web_admin": web_admin,
                "notification_settings": self._config_group(
                    "notification_settings"
                ),
                "telemetry_config": self._config_group("telemetry_config"),
            }
        )

    async def get_config_schema(self):
        schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
        if not schema_path.exists():
            return json_response({})
        try:
            schema_text = await asyncio.to_thread(
                schema_path.read_text, encoding="utf-8"
            )
            return json_response(json.loads(schema_text))
        except Exception as e:
            logger.error(f"[主动消息] 页面读取 Schema 失败喵: {e}")
            return json_response({})

    async def update_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("配置载荷必须是对象")
        allowed_keys = {
            "private_settings",
            "web_admin",
            "notification_settings",
            "telemetry_config",
        }
        if "friend_settings" in payload and "private_settings" not in payload:
            payload["private_settings"] = payload["friend_settings"]
        invalid_keys = [
            key
            for key in sorted(allowed_keys)
            if key in payload and not isinstance(payload[key], dict)
        ]
        if invalid_keys:
            return self._error(
                "配置分组必须是对象，非法字段: " + ", ".join(invalid_keys)
            )

        telemetry_payload = payload.get("telemetry_config")
        if isinstance(telemetry_payload, dict) and "enabled" in telemetry_payload:
            if not isinstance(telemetry_payload["enabled"], bool):
                return self._error("telemetry_config.enabled 必须是布尔值")

        for key in allowed_keys:
            if key not in payload:
                continue
            if key == "web_admin":
                old = self._config_group("web_admin")
                old.update(payload.get("web_admin", {}))
                self.plugin.config["web_admin"] = old
            else:
                self.plugin.config[key] = payload[key]

        telemetry = getattr(self.plugin, "telemetry", None)
        apply_config = getattr(telemetry, "apply_config", None)
        if callable(apply_config):
            try:
                apply_config(dict(self.plugin.config))
            except Exception as e:
                logger.debug(f"[主动消息] 页面同步遥测开关失败喵: {e}")

        try:
            self.plugin.config.save_config()
        except Exception as e:
            logger.error(f"[主动消息] 页面保存配置失败喵: {e}")
            await self._broadcast("config")
            return self._error(f"配置已应用到内存，但保存到磁盘失败: {e}", 500)

        await self._broadcast("config")
        return json_response({"ok": True})

    # ------------------------------------------------------------------
    # 会话差异配置
    # ------------------------------------------------------------------
    async def list_sessions(self):
        result = []
        for session in self.state.list_known_sessions():
            override = self.plugin.session_override_manager.get_override(session)
            effective = self.plugin.config_service.get_session_config(session)
            result.append(
                {
                    "session": session,
                    "session_name": self.plugin.session_service.get_session_name(
                        session, effective
                    ),
                    "session_display_name": (
                        self.plugin.session_service.get_session_display_name(
                            session, effective
                        )
                    ),
                    "has_override": bool(override),
                    "override_keys": list(override.keys()),
                    "enabled": bool(effective and effective.get("enable", False)),
                    "next_trigger_time": self.plugin.session_data.get(session, {}).get(
                        "next_trigger_time"
                    ),
                    "unanswered_count": self.plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                }
            )
        return json_response({"sessions": result})

    async def get_session_config(self):
        umo = self._query_umo()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        return json_response(
            {
                "session": normalized,
                "base": self.plugin.config_service.get_base_session_config(normalized),
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                "effective": self.plugin.config_service.get_session_config(normalized),
            }
        )

    async def update_session_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("会话配置载荷必须是对象")
        umo = str(payload.get("umo") or self._query_umo()).strip()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        mode = payload.get("mode", "effective")

        if mode == "override":
            override = payload.get("override", {})
            if not isinstance(override, dict):
                return self._error("override 必须是对象")
            await self.plugin.session_override_manager.set_override(
                normalized, override
            )
        else:
            effective = payload.get("effective", {})
            if not isinstance(effective, dict):
                return self._error("effective 必须是对象")
            base = self.plugin.config_service.get_base_session_config(normalized)
            if not base:
                return self._error(
                    "会话未命中 private_settings 私聊全局配置，无法保存 effective"
                )
            await self.plugin.session_override_manager.update_session_from_effective(
                normalized, base, effective
            )

        await self._broadcast("session-config")
        return json_response(
            {
                "ok": True,
                "session": normalized,
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                "effective": self.plugin.config_service.get_session_config(normalized),
            }
        )

    async def reset_session_config(self):
        payload: Any = {}
        if request.method == "POST":
            payload = await request.json(default={})
        umo = str(
            (payload.get("umo") if isinstance(payload, dict) else None)
            or self._query_umo()
        ).strip()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        await self.plugin.session_override_manager.delete_override(normalized)
        await self._broadcast("session-config")
        return json_response(
            {
                "ok": True,
                "session": normalized,
                "override": {},
                "effective": self.plugin.config_service.get_session_config(normalized),
            }
        )

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    async def list_jobs(self):
        return json_response({"jobs": self.state.collect_jobs()})

    async def list_intensity(self):
        return json_response(
            {
                "sessions": self.state.collect_intensity_sessions(),
                "summary": self.state.build_intensity_summary(),
            }
        )

    async def reschedule_job(self):
        payload = await request.json(default={})
        umo = str((payload or {}).get("umo") or self._query_umo()).strip()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        session_config = self.plugin.config_service.get_session_config(normalized)
        if not session_config or not session_config.get("enable", False):
            return self._error("会话未启用或配置不存在，无法重新调度", 400)
        if self.plugin.scheduler_service.is_session_paused(normalized):
            return self._error(
                "会话处于未回复暂停中，请先恢复或等待用户发来新消息", 409
            )

        await self.plugin.scheduler_service.schedule_next_chat_and_save(
            normalized, reset_counter=False
        )
        await self._broadcast("jobs")
        return json_response(
            {
                "ok": True,
                "session": normalized,
                "message": "已重新调度下一次主动消息时间",
            }
        )

    async def trigger_job(self):
        payload = await request.json(default={})
        umo = str((payload or {}).get("umo") or self._query_umo()).strip()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        if self.plugin.scheduler_service.is_session_paused(normalized):
            return self._error(
                "会话处于未回复暂停中，请先恢复或等待用户发来新消息", 409
            )
        if normalized in self.plugin.manual_trigger_sessions:
            return self._error("该任务正在立即触发中，请等待当前执行完成", 409)

        self.plugin.manual_trigger_sessions.add(normalized)
        track_task = getattr(self.plugin, "_track_task", None)
        task = asyncio.create_task(
            self.plugin.check_and_chat(normalized, TriggerSource.MANUAL)
        )
        if callable(track_task):
            track_task(task)
        await self._broadcast("jobs")
        return json_response(
            {
                "ok": True,
                "session": normalized,
                "in_progress": True,
                "message": "已开始立即触发，正在等待 LLM 完成回复",
            }
        )

    async def resume_job(self):
        payload = await request.json(default={})
        umo = str((payload or {}).get("umo") or self._query_umo()).strip()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        session_config = self.plugin.config_service.get_session_config(normalized)
        if not session_config or not session_config.get("enable", False):
            return self._error("会话未启用或配置不存在，无法恢复", 400)

        resumed = self.plugin.scheduler_service.resume_session(
            normalized, clear_cooldown=True
        )
        await self.plugin.scheduler_service.schedule_next_chat_and_save(
            normalized, reset_counter=True
        )
        await self._broadcast("jobs")
        return json_response(
            {
                "ok": True,
                "session": normalized,
                "resumed": bool(resumed),
                "message": "已解除未回复暂停并重新安排下一次主动消息",
            }
        )

    async def cancel_job(self):
        payload = await request.json(default={})
        umo = str((payload or {}).get("umo") or self._query_umo()).strip()
        if not umo:
            return self._error("缺少会话标识 umo")
        normalized = self._normalize(umo)
        removed = False
        try:
            self.plugin.scheduler.remove_job(normalized)
            removed = True
        except Exception:
            pass

        async with self.plugin.data_lock:
            if normalized in self.plugin.session_data:
                self.plugin.session_data[normalized].pop("next_trigger_time", None)
                await self.plugin.storage_service.save_data()

        await self._broadcast("jobs")
        return json_response(
            {"ok": True, "session": normalized, "removed": removed}
        )

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    async def get_notifications(self):
        return json_response(await self.state.build_notification_payload())

    async def mark_notification_read(self):
        if not getattr(self.plugin, "notification_center", None):
            return self._error("通知系统不可用", 503)
        payload = await request.json(default={})
        notification_id = (payload or {}).get("id")
        if notification_id is None:
            return self._error("缺少必填字段 id")
        try:
            normalized_id = int(notification_id)
        except (TypeError, ValueError):
            return self._error("id 必须是数字")
        result = await self.plugin.notification_center.mark_as_read(normalized_id)
        await self._broadcast("notifications")
        return json_response(result)

    async def mark_all_notifications_read(self):
        if not getattr(self.plugin, "notification_center", None):
            return self._error("通知系统不可用", 503)
        result = await self.plugin.notification_center.mark_all_as_read()
        await self._broadcast("notifications")
        return json_response(result)

    async def refresh_notifications(self):
        if not getattr(self.plugin, "notification_center", None):
            return self._error("通知系统不可用", 503)
        changed = await self.plugin.notification_center.refresh()
        await self._broadcast("notifications")
        payload = await self.plugin.notification_center.get_payload()
        return json_response(
            {
                "ok": True,
                "changed": changed,
                "items": payload.get("items", []),
                "meta": payload.get("meta", {}),
            }
        )

    # ------------------------------------------------------------------
    # Markdown 文档
    # ------------------------------------------------------------------
    async def list_markdown_files(self):
        return json_response({"items": self.state.list_markdown_documents()})

    async def get_markdown_file(self):
        raw_path = str(
            (request.query.get("path") if request is not None else "") or ""
        ).strip()
        if not raw_path:
            return self._error("缺少文档路径 path")
        resolved = self.state.resolve_markdown_document(raw_path)
        if not resolved:
            return self._error("文档不存在或不允许访问", 404)
        try:
            content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            return self._error("文档编码不受支持，仅支持 UTF-8 Markdown 文件")
        except Exception as e:
            logger.error(f"[主动消息] 页面读取 Markdown 文档失败喵: {e}")
            return self._error("读取文档失败", 500)
        return json_response(
            {
                "path": self.state.to_workspace_relative_path(resolved),
                "title": resolved.stem,
                "content": content,
                "content_format": "markdown",
            }
        )

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    async def open_directory(self):
        payload = await request.json(default={})
        target = str((payload or {}).get("path", "plugin")).strip().lower()
        if target == "data":
            directory = Path(self.plugin.data_dir)
        else:
            directory = Path(__file__).resolve().parents[2]

        try:
            directory.mkdir(parents=True, exist_ok=True)
            dir_str = str(directory)
            if _is_running_in_docker():
                return self._error(
                    "Docker 环境下不支持在宿主机直接打开目录，请手动查看挂载路径", 400
                )
            if os.name == "nt":
                await asyncio.to_thread(os.startfile, dir_str)
            elif sys.platform == "darwin":
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["open", dir_str],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return self._error(
                        f"打开目录失败（macOS）: {(result.stderr or result.stdout or '未知错误').strip()}",
                        500,
                    )
            else:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["xdg-open", dir_str],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return self._error(
                        f"打开目录失败（Linux）: {(result.stderr or result.stdout or '未知错误').strip()}",
                        500,
                    )
            return json_response(
                {
                    "ok": True,
                    "path": dir_str,
                    "message": "已在系统文件管理器中打开目录",
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 页面打开目录失败喵: {e}")
            return self._error(f"打开目录失败: {e}", 500)


__all__ = ["PageApiRoutes", "PLUGIN_NAME"]
