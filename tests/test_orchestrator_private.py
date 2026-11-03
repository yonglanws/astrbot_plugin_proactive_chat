"""流水线编排器的私聊守卫测试。"""

from __future__ import annotations

import asyncio

from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator import (
    FlowOrchestrator,
)

PRIVATE_UMO = "qq_official:FriendMessage:ORCH1"
GROUP_UMO = "qq_official:GroupMessage:ORCHG"


class FakeSessionService:
    def parse_session_id(self, umo):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo):
        return umo

    def build_session_key(self, umo):
        return SessionKey.parse(umo)

    def get_session_log_str(self, umo, config=None):
        return umo


class FakeConfigService:
    def get_session_config(self, umo):
        parsed = SessionKey.parse(umo)
        if not parsed or "friend" not in parsed.message_type.lower():
            return None
        return {
            "enable": True,
            # 不使用固定免打扰时段，避免测试结果随运行时刻变化。
            "schedule_settings": {"quiet_hours": ""},
        }


class FakePlugin:
    timezone = None

    def __init__(self):
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService()
        self.session_data = {}
        self.config = {}
        self.data_lock = asyncio.Lock()
        self.manual_trigger_sessions = set()
        self.web_admin_server = None

    async def check_and_chat(self, session_id, trigger=None):
        return None


def test_private_allowed():
    orchestrator = FlowOrchestrator(FakePlugin())
    allowed, reason = asyncio.run(orchestrator.is_chat_allowed(PRIVATE_UMO))
    assert allowed is True
    assert reason == "allowed"


def test_group_rejected():
    orchestrator = FlowOrchestrator(FakePlugin())
    allowed, reason = asyncio.run(orchestrator.is_chat_allowed(GROUP_UMO))
    assert allowed is False
    assert reason == "not_private"


def test_group_run_is_noop():
    plugin = FakePlugin()
    orchestrator = FlowOrchestrator(plugin)
    asyncio.run(orchestrator.run(GROUP_UMO))
    assert plugin.session_data == {}
    assert orchestrator._runner is None
