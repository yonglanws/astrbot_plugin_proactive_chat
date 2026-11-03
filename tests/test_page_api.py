"""AstrBot 插件页面后端 API 测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_proactive_chat.core.console import page_api as page_api_module
from astrbot_plugin_proactive_chat.core.console.page_api import (
    PLUGIN_NAME,
    PageApiRoutes,
)


class FakeContext:
    def __init__(self):
        self.registered = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))


class FakeConfig(dict):
    def __init__(self):
        super().__init__(
            private_settings={"enable": True},
            web_admin={"password": "secret", "enabled": True},
            notification_settings={"enabled": True},
            telemetry_config={"enabled": False},
        )
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakeOverrideStore:
    def __init__(self):
        self.overrides = {}
        self.saved_effective = None

    def get_override(self, session):
        return self.overrides.get(session, {})

    async def set_override(self, session, override):
        self.overrides[session] = override

    async def update_session_from_effective(self, session, base, effective):
        self.saved_effective = (session, base, effective)
        self.overrides[session] = {"enable": effective.get("enable")}

    async def delete_override(self, session):
        self.overrides.pop(session, None)


class FakeSessionService:
    @staticmethod
    def normalize_session_id(umo):
        return umo

    @staticmethod
    def get_session_name(umo, config=None):
        return "name"

    @staticmethod
    def get_session_display_name(umo, config=None):
        return "display"


class FakeConfigService:
    def get_base_session_config(self, umo):
        return {"enable": True, "schedule_settings": {}}

    def get_session_config(self, umo):
        return {"enable": True, "schedule_settings": {}}


class FakeScheduler:
    def __init__(self):
        self.rescheduled = False
        self.resumed = False

    def is_session_paused(self, umo):
        return False

    def resume_session(self, umo, clear_cooldown=False):
        self.resumed = True
        return True

    async def schedule_next_chat_and_save(self, umo, reset_counter=False):
        self.rescheduled = True

    def remove_job(self, umo):
        raise RuntimeError("no job")

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeSchedulerService(FakeScheduler):
    pass


class FakeStorage:
    async def save_data(self):
        pass


class FakePlugin:
    def __init__(self):
        self.context = FakeContext()
        self.config = FakeConfig()
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService()
        self.session_override_manager = FakeOverrideStore()
        self.session_data = {}
        self.data_lock = asyncio.Lock()
        self.scheduler = FakeScheduler()
        self.scheduler_service = FakeSchedulerService()
        self.storage_service = FakeStorage()
        self.manual_trigger_sessions = set()
        self.notification_center = None
        self.telemetry = None
        self.web_admin_server = None
        self.data_dir = Path(".")
        self.triggered = []
        self.tracked = []

    async def check_and_chat(self, umo, trigger=None):
        self.triggered.append((umo, trigger))

    def _track_task(self, task):
        self.tracked.append(task)


@pytest.fixture()
def plugin():
    return FakePlugin()


@pytest.fixture()
def routes(plugin, monkeypatch):
    page = PageApiRoutes(plugin)
    page.state = SimpleNamespace(
        build_status_payload=lambda: {"running": True},
        list_known_sessions=lambda: ["qq_official:FriendMessage:U1"],
        collect_jobs=lambda: [],
        collect_intensity_sessions=lambda: [],
        build_intensity_summary=lambda: {},
        build_notification_payload=lambda: {},
        list_markdown_documents=lambda: [],
        resolve_markdown_document=lambda raw: None,
        to_workspace_relative_path=lambda path: "docs/x.md",
    )
    fake_request = page_api_module.request
    fake_request.method = "GET"
    fake_request.query = {}
    fake_request.json_body = None
    return page


def _payload(response):
    assert isinstance(response, dict), response
    if response.get("status") == "error":
        return response
    return response.get("data")


def test_register_routes_use_plugin_prefix(routes, plugin):
    assert routes.register() is True
    count = len(plugin.context.registered)
    # 重复注册应幂等，不能重复追加路由。
    assert routes.register() is True
    assert len(plugin.context.registered) == count
    suffix = {
        route.split(f"/{PLUGIN_NAME}/", 1)[1]
        for route, *_ in plugin.context.registered
    }
    assert "status" in suffix
    assert "session-config" in suffix
    assert "jobs/trigger" in suffix
    assert all(
        route.startswith(f"/{PLUGIN_NAME}/") for route, *_ in plugin.context.registered
    )


def test_status_payload(routes):
    result = asyncio.run(routes.get_status())
    assert _payload(result) == {"running": True}


def test_config_update_persists_and_filters_password(routes, plugin):
    fake_request = page_api_module.request
    fake_request.json_body = {
        "private_settings": {"enable": False},
        "telemetry_config": {"enabled": True},
    }
    result = asyncio.run(routes.update_config())
    assert _payload(result) == {"ok": True}
    assert plugin.config["private_settings"]["enable"] is False
    assert plugin.config["telemetry_config"]["enabled"] is True
    assert plugin.config.saved == 1

    fake_request.method = "GET"
    fake_request.json_body = None
    config = _payload(asyncio.run(routes.get_config()))
    assert "password" not in config["web_admin"]


def test_update_config_rejects_non_object_group(routes):
    fake_request = page_api_module.request
    fake_request.json_body = {"private_settings": "bad"}
    result = asyncio.run(routes.update_config())
    assert result["status"] == "error"
    assert result["status_code"] == 400


def test_session_config_get_and_update(routes, plugin):
    fake_request = page_api_module.request
    fake_request.query = {"umo": "qq_official:FriendMessage:U1"}
    result = _payload(asyncio.run(routes.get_session_config()))
    assert result["session"] == "qq_official:FriendMessage:U1"

    fake_request.method = "POST"
    fake_request.json_body = {
        "umo": "qq_official:FriendMessage:U1",
        "mode": "effective",
        "effective": {"enable": False},
    }
    result = _payload(asyncio.run(routes.update_session_config()))
    assert result["ok"] is True
    assert plugin.session_override_manager.saved_effective[2] == {"enable": False}


def test_session_config_delete_accepts_post_body(routes, plugin):
    plugin.session_override_manager.overrides["qq_official:FriendMessage:U1"] = {
        "enable": True
    }
    fake_request = page_api_module.request
    fake_request.method = "POST"
    fake_request.json_body = {"umo": "qq_official:FriendMessage:U1"}
    result = _payload(asyncio.run(routes.reset_session_config()))
    assert result["ok"] is True
    assert "qq_official:FriendMessage:U1" not in plugin.session_override_manager.overrides


def test_trigger_job_starts_background_task(routes, plugin):
    fake_request = page_api_module.request
    fake_request.method = "POST"
    fake_request.json_body = {"umo": "qq_official:FriendMessage:U1"}

    async def scenario():
        result = await routes.trigger_job()
        assert len(plugin.tracked) == 1
        await plugin.tracked[0]
        return result

    result = _payload(asyncio.run(scenario()))
    assert result["in_progress"] is True
    assert plugin.triggered[0][0] == "qq_official:FriendMessage:U1"


def test_resume_and_reschedule_jobs(routes, plugin):
    fake_request = page_api_module.request
    fake_request.method = "POST"
    fake_request.json_body = {"umo": "qq_official:FriendMessage:U1"}
    result = _payload(asyncio.run(routes.resume_job()))
    assert result["resumed"] is True
    assert plugin.scheduler_service.resumed is True
    assert plugin.scheduler_service.rescheduled is True


def test_markdown_file_reads_workspace_document(routes, plugin, tmp_path):
    document = tmp_path / "guide.md"
    document.write_text("# hi", encoding="utf-8")
    routes.state.resolve_markdown_document = lambda raw: document
    fake_request = page_api_module.request
    fake_request.query = {"path": "docs/guide.md"}
    result = _payload(asyncio.run(routes.get_markdown_file()))
    assert result["content"] == "# hi"
    assert result["path"] == "docs/x.md"


def test_plugin_view_assets_and_i18n_exist():
    root = Path(__file__).resolve().parents[1]
    view_root = root / "views" / "proactive-chat"
    assert (view_root / "index.html").is_file()
    assert (view_root / "css" / "astrbot-page.css").is_file()
    assert (view_root / "js" / "utils" / "bridge.js").is_file()
    assert (view_root / "js" / "components" / "layout" / "EmbeddedShell.jsx").is_file()
    assert not (view_root / "index.html").read_text(encoding="utf-8").count("../logo.png")

    zh = json.loads(
        (root / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(
            encoding="utf-8"
        )
    )
    assert zh["views"]["proactive-chat"]["title"]
