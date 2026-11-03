"""完整流水线集成测试：拟人化阶段协同工作。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from astrbot_plugin_proactive_chat.core.domain.domain_enums import TriggerSource
from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey
from astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator import FlowOrchestrator
from astrbot_plugin_proactive_chat.core.services.conversation_state_service import (
    ConversationStateService,
)
from astrbot_plugin_proactive_chat.core.services.guard_service import GuardService
from astrbot_plugin_proactive_chat.core.services.intensity_service import IntensityService
from astrbot_plugin_proactive_chat.core.services.llm_service import LlmService
from astrbot_plugin_proactive_chat.core.services.persona_state_service import (
    PersonaStateService,
)
from astrbot_plugin_proactive_chat.core.services.rhythm_service import RhythmService

UMO = "qq_official:FriendMessage:PIPE1"
SETTINGS = {
    "enable": True,
    "proactive_prompt": "请自然地主动说一句话。当前时间 {{current_time}}。",
    "schedule_settings": {
        "min_interval_minutes": 30,
        "max_interval_minutes": 120,
        "quiet_hours": "0-0",
        "max_unanswered_times": 4,
    },
    "intensity_settings": {
        "enable": True,
        "probability_min": 100,
        "probability_max": 100,
    },
    "tts_settings": {"enable_tts": False},
    "segmented_reply_settings": {"enable": False},
    "humanization_settings": {
        "state_settings": {
            "enable": True,
            "silent_intent_enabled": False,
        },
        "feedback_settings": {"enable": True},
        "guard_settings": {"enable": True, "rewrite_on_fail": False},
        "delivery_plan_settings": {
            "enable": True,
            "max_bubbles": 2,
            "bubble_length_threshold": 20,
        },
        "persona_settings": {
            "enable": True,
            "low_energy_probability_reduction": 0,
        },
        "rhythm_settings": {
            "enable": True,
            "learn_active_hours": False,
            "min_gap_minutes": 0,
            "daily_proactive_limit": 6,
        },
        "memory_integration_settings": {"enable": True, "top_k": 2},
    },
}


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
        return {**SETTINGS, "_session_type": "friend"}


class FakeStorage:
    def __init__(self):
        self.saves = 0

    async def save_data(self):
        self.saves += 1


class FakeSchedulerService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.jobs = []

    def is_session_paused(self, session_id):
        return False

    def compute_next_trigger(self, schedule_conf, session_id=None, forced_interval_seconds=None, **kwargs):
        now = datetime.now(timezone.utc).timestamp()
        return (
            datetime.fromtimestamp(now + 1800, timezone.utc),
            now,
            1800,
            3600,
            1800,
            now + 1800,
            50.0,
        )

    def add_chat_job(self, session_id, run_date, trigger=None):
        self.jobs.append((session_id, run_date, trigger))

    async def schedule_next_chat_and_save(self, session_id, reset_counter=False, forced_interval_seconds=None):
        return None

    async def pause_session(self, *args, **kwargs):
        return True

    def clear_session_schedule_state(self, *args, **kwargs):
        return False


class FakeContextService:
    async def prepare_llm_request(self, session_id):
        return {
            "conv_id": "conv1",
            "history": [
                {"role": "user", "content": "我最近在准备面试"},
                {"role": "assistant", "content": "那你准备得怎么样"},
            ],
            "system_prompt": "你是一个自然、有边界感的人。",
            "platform_context": "",
            "session_id": session_id,
            "conversation": SimpleNamespace(persona_id="p1"),
        }

    @staticmethod
    def sanitize_history_content(history):
        return history

    @staticmethod
    def make_temp_text_part(text):
        return text


class FakeSender:
    def __init__(self):
        self.sent = []

    def build_proactive_event(self, session_id):
        return SimpleNamespace(session_id=session_id)

    async def send_proactive_message(self, session_id, text, event=None, initial_chain=None, delivery_plan=None):
        self.sent.append(
            {
                "session_id": session_id,
                "text": text,
                "delivery_plan": delivery_plan,
            }
        )
        return True


class FakeMemoryBridge:
    def __init__(self):
        self.stored = []

    async def recall(self, session_id, query, persona_id=None):
        return [
            {
                "content": "对方下周三有面试",
                "importance": 0.8,
                "score": 0.9,
            }
        ]

    @staticmethod
    def format_memories(memories):
        return "<long_term_memory>对方下周三有面试</long_term_memory>"

    async def store_proactive(self, session_id, text, session_config=None, now=None):
        self.stored.append((session_id, text))
        return True


class FakePlugin:
    timezone = timezone.utc

    def __init__(self):
        self.config = {"private_settings": SETTINGS}
        self.session_data = {UMO: {"unanswered_count": 0}}
        self.data_lock = asyncio.Lock()
        self.last_message_times = {}
        self.plugin_start_time = 0
        self.manual_trigger_sessions = set()
        self.web_admin_server = None
        self._terminating = False
        self.telemetry = None
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService()
        self.storage_service = FakeStorage()
        self.context = SimpleNamespace(conversation_manager=SimpleNamespace())
        self.scheduler_service = FakeSchedulerService(self)
        self.context_service = FakeContextService()
        self.sender_service = FakeSender()
        self.memory_bridge = FakeMemoryBridge()
        self.intensity_service = IntensityService(self)
        self.rhythm_service = RhythmService(self)
        self.conversation_state_service = ConversationStateService(self)
        self.persona_state_service = PersonaStateService(self)
        self.guard_service = GuardService(self)
        self.llm_service = LlmService(self)
        self.orchestrator = FlowOrchestrator(self)


class FakeProvider:
    async def text_chat(self, **kwargs):
        return SimpleNamespace(
            completion_text="刚下班，路过那家店，突然想起你上次说想吃。要不要给你带一份？"
        )


async def _fake_resolve_provider(self, session_id):
    return FakeProvider()


async def _fake_invoke_provider(provider, req):
    return SimpleNamespace(
        completion_text="刚下班，路过那家店，突然想起你上次说想吃。要不要给你带一份？",
        result_chain=None,
    )


async def _noop_add_message_pair(*args, **kwargs):
    return None


async def _true_verify(*args, **kwargs):
    return True


def test_full_pipeline_runs_humanization(monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.services.llm_service.invoke_provider",
        _fake_invoke_provider,
    )
    monkeypatch.setattr(LlmService, "resolve_chat_provider", _fake_resolve_provider)
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator.add_message_pair",
        _noop_add_message_pair,
    )
    monkeypatch.setattr(
        "astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator.verify_message_persisted",
        _true_verify,
    )

    plugin = FakePlugin()
    asyncio.run(plugin.orchestrator.run(UMO, TriggerSource.MANUAL))

    assert plugin.sender_service.sent, "deliver 阶段应发送一条消息"
    sent = plugin.sender_service.sent[0]
    assert sent["text"]
    assert sent["delivery_plan"]["bubbles"]
    assert plugin.memory_bridge.stored
    assert plugin.scheduler_service.jobs

    payload = plugin.session_data[UMO]
    assert payload["last_proactive_texts"] == [sent["text"]]
    assert payload["rhythm"]["daily_proactive"][next(iter(payload["rhythm"]["daily_proactive"]))] == 1
    assert payload["conversation_state"]["last_intent"]
    assert payload["persona_state"]["energy"] > 0
    assert payload["last_guard"]["allowed"] is True
    assert payload["next_trigger_time"]
    assert payload["unanswered_count"] == 1
