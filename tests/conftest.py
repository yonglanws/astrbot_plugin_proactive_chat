"""测试环境准备：为纯逻辑测试提供最小 astrbot 桩模块。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 插件的相对导入（如 ...utils）以 "astrbot_plugin_proactive_chat" 为顶层包，
# 因此测试需要把它的父目录加入 sys.path。
PLUGIN_PARENT = PROJECT_ROOT.parent
for path in (PROJECT_ROOT, PLUGIN_PARENT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _install_astrbot_stubs() -> None:
    """core 下的服务只依赖 astrbot.api.logger，测试无需真实框架。"""
    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot.__path__ = []  # type: ignore[attr-defined]
        sys.modules["astrbot"] = astrbot
    if "astrbot.api" not in sys.modules:
        api = types.ModuleType("astrbot.api")
        api.__path__ = []  # type: ignore[attr-defined]

        class _Logger:
            def debug(self, *args, **kwargs):
                pass

            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                pass

            def error(self, *args, **kwargs):
                pass

            def exception(self, *args, **kwargs):
                pass

        api.logger = _Logger()
        sys.modules["astrbot.api"] = api
        sys.modules["astrbot"].api = api  # type: ignore[attr-defined]

        # 插件页面 API 依赖 astrbot.api.web；测试用轻量代理模拟请求与响应。
        web = types.ModuleType("astrbot.api.web")
        web.__path__ = []  # type: ignore[attr-defined]

        class _FakeRequest:
            method = "GET"
            query: dict = {}
            json_body: dict | None = None

            async def json(self, default=None):
                return self.json_body if self.json_body is not None else default

        web.request = _FakeRequest()
        web.json_response = lambda data=None, **kwargs: {
            "status": "ok",
            "data": data,
        }
        web.error_response = lambda message, status_code=400, **kwargs: {
            "status": "error",
            "message": message,
            "status_code": status_code,
        }
        api.web = web  # type: ignore[attr-defined]
        sys.modules["astrbot.api.web"] = web

        # 遥测模块在导入期需要 StarTools.get_data_dir；测试不触碰真实数据目录。
        star_api = types.ModuleType("astrbot.api.star")
        star_api.__path__ = []  # type: ignore[attr-defined]

        class StarTools:
            @staticmethod
            def get_data_dir(*args, **kwargs):
                from pathlib import Path
                import tempfile

                return Path(tempfile.gettempdir()) / "astrbot_plugin_proactive_chat_test_data"

        class Star:
            def __init__(self, context=None):
                self.context = context

        class Context:
            registered_web_apis = []

        star_api.StarTools = StarTools
        star_api.Star = Star
        star_api.Context = Context
        api.star = star_api  # type: ignore[attr-defined]
        sys.modules["astrbot.api.star"] = star_api

        # main.py 的类型注解需要 AstrBotConfig；用可读写字典模拟。
        config_module = types.ModuleType("astrbot.core.config.astrbot_config")
        config_module.__path__ = []  # type: ignore[attr-defined]

        class AstrBotConfig(dict):
            def save_config(self):
                return None

        config_module.AstrBotConfig = AstrBotConfig
        sys.modules["astrbot.core.config"] = types.ModuleType("astrbot.core.config")
        sys.modules["astrbot.core.config"].__path__ = []  # type: ignore[attr-defined]
        sys.modules["astrbot.core.config.astrbot_config"] = config_module

        # astrbot_provider 在导入期需要 ProviderRequest；测试只做导入与准入，
        # 不会真正构造 Provider 请求。
        provider = types.ModuleType("astrbot.api.provider")
        provider.__path__ = []  # type: ignore[attr-defined]

        class ProviderRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        provider.ProviderRequest = ProviderRequest
        api.provider = provider  # type: ignore[attr-defined]
        sys.modules["astrbot.api.provider"] = provider

        event_module = types.ModuleType("astrbot.api.event")
        event_module.__path__ = []  # type: ignore[attr-defined]

        class AstrMessageEvent:
            pass

        class EventMessageType:
            PRIVATE_MESSAGE = "private_message"

        class _Filter:
            @staticmethod
            def on_astrbot_loaded(*args, **kwargs):
                def decorator(func):
                    return func

                return decorator

            @staticmethod
            def event_message_type(*args, **kwargs):
                def decorator(func):
                    return func

                return decorator

            @staticmethod
            def command(*args, **kwargs):
                def decorator(func):
                    return func

                return decorator

        _Filter.EventMessageType = EventMessageType
        event_module.AstrMessageEvent = AstrMessageEvent
        event_module.filter = _Filter()
        api.event = event_module  # type: ignore[attr-defined]
        sys.modules["astrbot.api.event"] = event_module

    _install_core_stubs()


def _ensure_package(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module
    return module


def _install_core_stubs() -> None:
    """为仅需导入的服务提供 astrbot.core 下的最小类型桩。"""
    core = _ensure_package("astrbot.core")
    _ensure_package("astrbot.core.message")
    _ensure_package("astrbot.core.platform")
    _ensure_package("astrbot.core.star")
    _ensure_package("astrbot.core.agent")

    if "astrbot.core.message.message_event_result" not in sys.modules:
        module = types.ModuleType("astrbot.core.message.message_event_result")

        class MessageChain(list):
            def __init__(self, chain=None):
                super().__init__(chain or [])

        module.MessageChain = MessageChain

        class ResultContentType:
            LLM_RESULT = "llm_result"

        class MessageEventResult:
            def __init__(self):
                self.chain = []
                self.result_content_type = None

            def set_result_content_type(self, value):
                self.result_content_type = value

        module.ResultContentType = ResultContentType
        module.MessageEventResult = MessageEventResult
        sys.modules["astrbot.core.message.message_event_result"] = module

    if "astrbot.core.message.components" not in sys.modules:
        module = types.ModuleType("astrbot.core.message.components")

        class Plain:
            def __init__(self, text=""):
                self.text = text

        class Record:
            def __init__(self, file=""):
                self.file = file

        module.Plain = Plain
        module.Record = Record
        sys.modules["astrbot.core.message.components"] = module

    if "astrbot.core.platform.astrbot_message" not in sys.modules:
        module = types.ModuleType("astrbot.core.platform.astrbot_message")

        class AstrBotMessage:
            pass

        class MessageMember:
            def __init__(self, user_id="", nickname=None, **kwargs):
                self.user_id = user_id
                self.nickname = nickname

        module.AstrBotMessage = AstrBotMessage
        module.MessageMember = MessageMember
        sys.modules["astrbot.core.platform.astrbot_message"] = module

    if "astrbot.core.platform.message_type" not in sys.modules:
        module = types.ModuleType("astrbot.core.platform.message_type")

        class MessageType:
            FRIEND_MESSAGE = "FriendMessage"
            GROUP_MESSAGE = "GroupMessage"
            GUILD_MESSAGE = "GuildMessage"

        module.MessageType = MessageType
        sys.modules["astrbot.core.platform.message_type"] = module

    if "astrbot.core.platform.platform" not in sys.modules:
        module = types.ModuleType("astrbot.core.platform.platform")

        class PlatformStatus:
            RUNNING = "running"

        module.PlatformStatus = PlatformStatus
        sys.modules["astrbot.core.platform.platform"] = module

    if "astrbot.core.platform.astr_message_event" not in sys.modules:
        module = types.ModuleType("astrbot.core.platform.astr_message_event")

        class AstrMessageEvent:
            pass

        class MessageSession:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        module.AstrMessageEvent = AstrMessageEvent
        module.MessageSession = MessageSession
        sys.modules["astrbot.core.platform.astr_message_event"] = module

    if "astrbot.core.star.star_handler" not in sys.modules:
        module = types.ModuleType("astrbot.core.star.star_handler")

        class EventType:
            OnWaitingLLMRequestEvent = "on_waiting_llm_request"
            OnLLMRequestEvent = "on_llm_request"
            OnLLMResponseEvent = "on_llm_response"
            OnAfterMessageSentEvent = "on_after_message_sent"

        class _HandlerRegistry:
            @staticmethod
            def get_handlers_by_event_type(*args, **kwargs):
                return []

        module.EventType = EventType
        module.star_handlers_registry = _HandlerRegistry()
        sys.modules["astrbot.core.star.star_handler"] = module

    if "astrbot.core.agent.message" not in sys.modules:
        module = types.ModuleType("astrbot.core.agent.message")

        class TextPart:
            def __init__(self, text=""):
                self.text = text
                self._no_save = False

            def mark_as_temp(self):
                self._no_save = True
                return self

        class _MessageSegment:
            role = "user"

            def __init__(self, content=None):
                self.content = content or []

            def model_dump(self):
                return {
                    "role": self.role,
                    "content": [
                        getattr(part, "text", str(part)) for part in self.content
                    ],
                }

        class UserMessageSegment(_MessageSegment):
            role = "user"

        class AssistantMessageSegment(_MessageSegment):
            role = "assistant"

        module.TextPart = TextPart
        module.UserMessageSegment = UserMessageSegment
        module.AssistantMessageSegment = AssistantMessageSegment
        sys.modules["astrbot.core.agent.message"] = module

    core.__path__ = []  # type: ignore[attr-defined]


def _install_optional_stubs() -> None:
    """为只测试纯合并逻辑的模块提供 aiofiles 桩（仅在未安装真实 aiofiles 时启用）。"""
    if "aiofiles" in sys.modules:
        return
    try:
        import aiofiles  # noqa: F401
        import aiofiles.os  # noqa: F401
        return
    except ImportError:
        pass
    aiofiles = types.ModuleType("aiofiles")
    aiofiles.__path__ = []  # type: ignore[attr-defined]

    def _open(*args, **kwargs):
        raise RuntimeError("aiofiles stub 不支持真实文件操作")

    aiofiles.open = _open
    aio_os = types.ModuleType("aiofiles.os")
    aio_os.path = types.SimpleNamespace(
        exists=lambda *args, **kwargs: False,
    )
    aio_os.makedirs = lambda *args, **kwargs: None
    aiofiles.os = aio_os  # type: ignore[attr-defined]
    sys.modules["aiofiles"] = aiofiles
    sys.modules["aiofiles.os"] = aio_os


_install_astrbot_stubs()
_install_optional_stubs()
