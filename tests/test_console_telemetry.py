"""控制台 API、状态构建、TokenAuth 与遥测管理器回归测试。"""

from __future__ import annotations

import asyncio
import copy
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrbot_plugin_proactive_chat.core.console import page_api as page_api_module
from astrbot_plugin_proactive_chat.core.console.api_routes import ApiRoutes
from astrbot_plugin_proactive_chat.core.console.page_api import PageApiRoutes
from astrbot_plugin_proactive_chat.core.console.state_builder import StateBuilder
from astrbot_plugin_proactive_chat.core.console.token_auth import TokenAuth
from astrbot_plugin_proactive_chat.core.remote.telemetry_manager import TelemetryManager
from astrbot_plugin_proactive_chat.core.services.conversation_memory_service import (
    ConversationMemoryService,
)
from astrbot_plugin_proactive_chat.core.services.conversation_state_service import (
    ConversationStateService,
)
from astrbot_plugin_proactive_chat.core.services.intensity_service import (
    IntensityService,
)
from astrbot_plugin_proactive_chat.core.services.persona_state_service import (
    PersonaStateService,
)
from astrbot_plugin_proactive_chat.core.services.rhythm_service import RhythmService

UMO = "qq_official:FriendMessage:CONS1"


class FakeConfig(dict):
    def __init__(self, password="中文密码123"):
        super().__init__(
            private_settings={
                "enable": True,
                "session_list": [UMO],
                "schedule_settings": {"max_unanswered_times": 4},
            },
            web_admin={"password": password, "enabled": True},
            notification_settings={"enabled": False},
            telemetry_config={"enabled": False},
        )
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakeSchedulerService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.cancelled = []
        self.rescheduled = []

    def is_session_paused(self, umo):
        return bool(self.plugin.session_data.get(umo, {}).get("proactive_paused"))

    async def cancel_session_schedule(self, umo):
        self.cancelled.append(umo)
        info = self.plugin.session_data.setdefault(umo, {})
        info.pop("next_trigger_time", None)
        info["schedule_cancelled"] = True
        return True

    async def schedule_next_chat_and_save(self, umo, reset_counter=False):
        self.rescheduled.append((umo, reset_counter))

    async def setup_auto_triggers_for_enabled_sessions(self):
        return None


def make_console_plugin(password="中文密码123"):
    plugin = SimpleNamespace(
        config=FakeConfig(password=password),
        session_data={UMO: {"unanswered_count": 4}},
        data_lock=asyncio.Lock(),
        plugin_start_time=time.time() - 120,
        timezone=None,
        scheduler=SimpleNamespace(
            running=True, get_jobs=lambda: [], remove_job=lambda sid: None
        ),
        auto_trigger_timers={},
        manual_trigger_sessions=set(),
        active_trigger_sessions=set(),
        _telemetry_tasks=set(),
        session_service=SimpleNamespace(
            normalize_session_id=lambda umo: umo,
            parse_session_id=lambda umo: ("qq_official", "FriendMessage", "CONS1"),
            get_session_name=lambda umo, cfg=None: "",
            get_session_display_name=lambda umo, cfg=None: umo,
            get_session_log_str=lambda umo, cfg=None: umo,
        ),
        config_service=SimpleNamespace(
            get_session_config=lambda umo: {
                "enable": True,
                "schedule_settings": {"max_unanswered_times": 4},
            },
            get_base_session_config=lambda umo: {
                "enable": True,
                "schedule_settings": {"max_unanswered_times": 4},
            },
            get_private_settings=lambda: {"enable": True, "session_list": [UMO]},
        ),
        session_override_manager=SimpleNamespace(
            list_sessions=lambda: [],
            get_override=lambda umo: {},
        ),
        storage_service=SimpleNamespace(save_data=lambda: asyncio.sleep(0)),
        telemetry=None,
        notification_center=None,
        web_admin_server=None,
        version="v2.5.0",
    )
    plugin.scheduler_service = FakeSchedulerService(plugin)
    plugin.orchestrator = SimpleNamespace(
        invalidate_calls=0,
        invalidate_runner=lambda: setattr(
            plugin.orchestrator,
            "invalidate_calls",
            plugin.orchestrator.invalidate_calls + 1,
        ),
    )
    plugin._track_task = lambda task: plugin._telemetry_tasks.add(task)
    return plugin


def test_api_routes_utf8_login_reschedule_limit_cancel_and_config_refresh():
    plugin = make_console_plugin(password="中文密码123")
    token_auth = TokenAuth(lambda: bool(plugin.config["web_admin"]["password"]))
    state_builder = StateBuilder(plugin, "v2.5.0")
    routes = ApiRoutes(
        plugin=plugin,
        state_builder=state_builder,
        realtime=SimpleNamespace(),
        token_auth=token_auth,
        config=plugin.config,
        is_docker_fn=lambda: False,
    )
    app = FastAPI()
    routes.register(app)
    client = TestClient(app)

    # UTF-8 中文密码不应触发 compare_digest TypeError 500。
    bad = client.post("/api/login", json={"password": "错误密码"})
    assert bad.status_code == 401
    ok = client.post("/api/login", json={"password": "中文密码123"})
    assert ok.status_code == 200
    assert ok.json()["token"]

    # unanswered_count >= max_unanswered_times 时即使未标记 paused，reschedule 也必须返回 400。
    resched = client.post(f"/api/jobs/{UMO}/reschedule")
    assert resched.status_code == 400
    assert plugin.scheduler_service.rescheduled == []

    # DELETE /api/jobs/{umo} 应走 cancel_session_schedule 并落盘 schedule_cancelled。
    cancelled = client.delete(f"/api/jobs/{UMO}")
    assert cancelled.status_code == 200
    assert cancelled.json()["removed"] is True
    assert plugin.scheduler_service.cancelled == [UMO]
    assert plugin.session_data[UMO]["schedule_cancelled"] is True

    # POST /api/config 应使 orchestrator runner 失效以便重建新配置流水线。
    cfg_resp = client.post(
        "/api/config",
        json={"private_settings": {"enable": True, "session_list": [UMO]}},
    )
    assert cfg_resp.status_code == 200
    assert plugin.orchestrator.invalidate_calls == 1


def test_page_api_reschedule_at_limit_and_cancel_job():
    plugin = make_console_plugin()
    page = PageApiRoutes(plugin)
    req = page_api_module.request
    req.method = "POST"
    req.query = {}
    req.json_body = {"umo": UMO}

    res = asyncio.run(page.reschedule_job())
    assert res["status"] == "error"
    assert res["status_code"] == 400

    cancelled = asyncio.run(page.cancel_job())
    assert cancelled["data"]["removed"] is True
    assert plugin.scheduler_service.cancelled == [UMO]


def test_state_builder_started_at_default_limit_4_and_read_only():
    now = 1_700_000_000.0
    plugin = make_console_plugin()
    plugin.plugin_start_time = now - 600
    read_only_flags = []

    class FakeIntensity:
        def evaluate(self, session_id, read_only=False):
            read_only_flags.append(read_only)
            return {"score": 50, "tier": "warm", "tier_label": "热络", "probability_percent": 60}

        def summary(self, sessions, read_only=False):
            read_only_flags.append(read_only)
            return {}

    plugin.intensity_service = FakeIntensity()
    plugin.config_service.get_session_config = lambda umo: {
        "enable": True,
        "auto_trigger_settings": {"auto_trigger_after_minutes": 10},
        "schedule_settings": {},
    }

    loop = SimpleNamespace(time=lambda: 1000.0)
    timer = SimpleNamespace(
        cancelled=lambda: False,
        when=lambda: 1300.0,  # remaining = 300s -> target = now + 300 -> started_at = now - 300
        _loop=loop,
    )
    plugin.auto_trigger_timers = {UMO: timer}

    builder = StateBuilder(plugin, "v2.5.0")
    cards = builder.collect_timer_cards(now)["auto_trigger_cards"]
    assert len(cards) == 1
    assert cards[0]["max_unanswered_times"] == 4
    assert cards[0]["started_at"] == now - 300
    assert read_only_flags == [True]


def test_state_builder_real_services_read_only_does_not_mutate_session_data():
    now = 1_700_000_000.0
    plugin = make_console_plugin()
    plugin.session_data = {
        UMO: {
            "unanswered_count": 1,
            "intensity": {
                "events": [{"t": now - 60, "n": 10, "q": 0, "e": 0, "m": 0}],
                "total_user_messages": 1,
                "last_user_ts": now - 60,
            },
            "rhythm": {"total_activity": 0, "seed": {}},
        }
    }
    plugin.intensity_service = IntensityService(plugin)
    plugin.rhythm_service = RhythmService(plugin)
    plugin.conversation_state_service = ConversationStateService(plugin)
    plugin.conversation_memory_service = ConversationMemoryService(plugin)
    plugin.persona_state_service = PersonaStateService(plugin)

    before = copy.deepcopy(plugin.session_data)
    builder = StateBuilder(plugin, "v2.5.0")
    summaries = builder.list_known_session_summaries()
    assert summaries
    assert builder.collect_intensity_sessions()
    assert builder.build_intensity_summary()
    assert builder.build_status_payload()
    assert plugin.session_data == before
    assert "conversation_state" not in plugin.session_data[UMO]
    assert "persona_state" not in plugin.session_data[UMO]
    assert plugin.session_data[UMO]["rhythm"] == {"total_activity": 0, "seed": {}}


def test_token_auth_256_cap_and_stop_cleanup():
    auth = TokenAuth(True, max_tokens=256)
    tokens = [auth.issue_token() for _ in range(260)]
    assert len(auth._tokens) == 256
    # 最早签发的 4 个应已被淘汰，最新的仍有效。
    assert auth.verify_token(tokens[0]) is False
    assert auth.verify_token(tokens[-1]) is True

    async def scenario():
        auth.start_cleanup()
        assert auth._token_cleanup_task is not None
        await auth.stop_cleanup()
        assert auth._token_cleanup_task is None

    asyncio.run(scenario())


def test_telemetry_timeout_requeue_and_close_lazy_flush():
    sent_batches = []

    class FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *, timeout_once=False):
            self.closed = False
            self.timeout_once = timeout_once

        def post(self, url, json=None, headers=None):
            if self.timeout_once:
                raise asyncio.TimeoutError()
            sent_batches.append(json["batch"])
            return FakeResp()

        async def close(self):
            self.closed = True

    async def scenario():
        mgr = TelemetryManager(
            config={"telemetry_config": {"enabled": True}},
            plugin_version="v2.5.0",
        )
        mgr._feature_buffer = [{"event": "feature", "data": {"feature": "x"}}]
        mgr._session = None

        timeout_session = FakeSession(timeout_once=True)

        async def get_timeout_session():
            return timeout_session

        mgr._get_session = get_timeout_session
        ok = await mgr._send_batch(
            [{"event": "feature", "data": {"feature": "timed_out"}}],
            requeue_on_failure=True,
        )
        assert ok is False
        assert len(mgr._backlog) == 1

        # close() 即使 _session 仍为 None，也应按需创建会话并 flush backlog + feature_buffer。
        ok_session = FakeSession(timeout_once=False)

        async def get_ok_session():
            mgr._session = ok_session
            return ok_session

        mgr._get_session = get_ok_session
        await mgr.close()
        assert len(sent_batches) == 1
        assert len(sent_batches[0]) == 2
        assert ok_session.closed is True

    asyncio.run(scenario())
