"""真实 AstrBot 源码集成探针。

由 pytest 以子进程方式运行，避免 tests/conftest.py 的轻量桩污染真实模块。
它使用 AstrBot 4.29.4 源码中的：
- ``star_handler.StarHandlerRegistry``（真实 plugin_set 过滤逻辑）
- ``star.context.Context.register_web_api``（真实页面 API 注册逻辑）
- ``api.web.PluginRequest`` / ``bind_request_context``（真实插件请求对象）
并真实导入本插件的 ``main.py``。

源码位置优先级：ASTRBOT_SRC 环境变量 → 本地已知路径。找不到时退出码 77。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PARENT = PLUGIN_ROOT.parent
sys.path.insert(0, str(PLUGIN_PARENT))


def _find_astrbot_src() -> Path | None:
    candidates = []
    env = os.environ.get("ASTRBOT_SRC")
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            Path("C:/Users/21557/AppData/Local/Temp/astrbot_src"),
            Path("Z:/astrbot/astrbot_src"),
            PLUGIN_PARENT.parent / "astrbot",
            PLUGIN_PARENT.parent / "AstrBot",
        ]
    )
    for candidate in candidates:
        if (candidate / "astrbot" / "core" / "star" / "star_handler.py").exists():
            return candidate
    return None


def _package(name: str, path: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    module.__getattr__ = lambda attr: _Dummy  # type: ignore[attr-defined]
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    if parent_name and parent_name in sys.modules:
        setattr(sys.modules[parent_name], child, module)
    return module


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass


def _stub_module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is not None:
        return module
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    module.__getattr__ = lambda attr: _Dummy  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _bootstrap(src: Path) -> None:
    """建立 AstrBot 包代理与必要的轻量类型桩。"""
    for name, path in [
        ("astrbot", src / "astrbot"),
        ("astrbot.core", src / "astrbot" / "core"),
        ("astrbot.api", src / "astrbot" / "api"),
        ("astrbot.core.platform", src / "astrbot" / "core" / "platform"),
        ("astrbot.core.agent", src / "astrbot" / "core" / "agent"),
        ("astrbot.core.star", src / "astrbot" / "core" / "star"),
        ("astrbot.core.utils", src / "astrbot" / "core" / "utils"),
    ]:
        _package(name, path)

    astrbot = sys.modules["astrbot"]
    astrbot.__version__ = "4.29.4"
    astrbot.logger = logging.getLogger("astrbot")
    api = sys.modules["astrbot.api"]
    api.logger = logging.getLogger("astrbot")

    event_api = types.ModuleType("astrbot.api.event")
    event_api.__path__ = []  # type: ignore[attr-defined]

    class EventMessageType:
        PRIVATE_MESSAGE = "private_message"

    class _Filter:
        @staticmethod
        def on_astrbot_loaded(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

    _Filter.EventMessageType = EventMessageType
    event_api.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_api.filter = _Filter()
    sys.modules["astrbot.api.event"] = event_api
    api.event = event_api

    star_api = types.ModuleType("astrbot.api.star")
    star_api.__path__ = []  # type: ignore[attr-defined]

    class Star:
        def __init__(self, context=None):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(*args, **kwargs):
            return Path(os.environ.get("TEMP", "/tmp")) / "proactive_real_it"

    star_api.Star = Star
    star_api.StarTools = StarTools
    sys.modules["astrbot.api.star"] = star_api
    api.star = star_api

    provider_api = types.ModuleType("astrbot.api.provider")
    provider_api.__path__ = []  # type: ignore[attr-defined]

    class ProviderRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    provider_api.ProviderRequest = ProviderRequest
    sys.modules["astrbot.api.provider"] = provider_api
    api.provider = provider_api

    config_pkg = _package("astrbot.core.config", src / "astrbot" / "core" / "config")
    config_module = types.ModuleType("astrbot.core.config.astrbot_config")
    config_module.__path__ = []  # type: ignore[attr-defined]

    class AstrBotConfig(dict):
        pass

    config_module.AstrBotConfig = AstrBotConfig
    sys.modules["astrbot.core.config.astrbot_config"] = config_module
    config_pkg.astrbot_config = config_module

    components = types.ModuleType("astrbot.core.message.components")
    components.__path__ = []  # type: ignore[attr-defined]

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Record:
        def __init__(self, file=""):
            self.file = file

    components.Plain = Plain
    components.Record = Record
    sys.modules["astrbot.core.message.components"] = components

    result_module = types.ModuleType("astrbot.core.message.message_event_result")
    result_module.__path__ = []  # type: ignore[attr-defined]

    class MessageChain(list):
        def __init__(self, chain=None):
            super().__init__(chain or [])

    class MessageEventResult:
        def __init__(self):
            self.chain = []
            self.result_content_type = None

        def set_result_content_type(self, value):
            self.result_content_type = value

    class ResultContentType:
        LLM_RESULT = "llm_result"

    result_module.MessageChain = MessageChain
    result_module.MessageEventResult = MessageEventResult
    result_module.ResultContentType = ResultContentType
    sys.modules["astrbot.core.message.message_event_result"] = result_module

    platform_message = types.ModuleType("astrbot.core.platform.astrbot_message")

    class AstrBotMessage:
        pass

    class MessageMember:
        def __init__(self, user_id="", nickname=None, **kwargs):
            self.user_id = user_id
            self.nickname = nickname

    platform_message.AstrBotMessage = AstrBotMessage
    platform_message.MessageMember = MessageMember
    sys.modules["astrbot.core.platform.astrbot_message"] = platform_message

    message_type = types.ModuleType("astrbot.core.platform.message_type")

    class MessageType:
        FRIEND_MESSAGE = "FriendMessage"
        GROUP_MESSAGE = "GroupMessage"
        GUILD_MESSAGE = "GuildMessage"

    message_type.MessageType = MessageType
    sys.modules["astrbot.core.platform.message_type"] = message_type

    platform_module = types.ModuleType("astrbot.core.platform.platform")

    class PlatformStatus:
        RUNNING = "running"

    platform_module.PlatformStatus = PlatformStatus
    sys.modules["astrbot.core.platform.platform"] = platform_module

    message_event = types.ModuleType("astrbot.core.platform.astr_message_event")
    message_event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    class MessageSession:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    message_event.MessageSession = MessageSession
    message_event.MessageSesion = MessageSession
    message_event.current_message_event = None
    message_event.streaming_response_event = None
    message_event.__getattr__ = lambda attr: _Dummy  # type: ignore[attr-defined]
    sys.modules["astrbot.core.platform.astr_message_event"] = message_event

    agent_message = types.ModuleType("astrbot.core.agent.message")

    class TextPart:
        def __init__(self, text=""):
            self.text = text

    class _Segment:
        role = "user"

        def __init__(self, content=None):
            self.content = content or []

        def model_dump(self):
            return {
                "role": self.role,
                "content": [getattr(part, "text", str(part)) for part in self.content],
            }

    class UserMessageSegment(_Segment):
        role = "user"

    class AssistantMessageSegment(_Segment):
        role = "assistant"

    agent_message.TextPart = TextPart
    agent_message.UserMessageSegment = UserMessageSegment
    agent_message.AssistantMessageSegment = AssistantMessageSegment
    agent_message.__getattr__ = lambda attr: _Dummy  # type: ignore[attr-defined]
    sys.modules["astrbot.core.agent.message"] = agent_message

    upload = types.ModuleType("astrbot.core.utils.upload")

    async def save_upload_stream(*args, **kwargs):
        return None

    upload.save_upload_stream = save_upload_stream
    sys.modules["astrbot.core.utils.upload"] = upload

    # 真实 star_handler 只依赖这两个轻量类型；保留其真实过滤逻辑。
    filter_module = types.ModuleType("astrbot.core.star.filter")
    filter_module.__path__ = []  # type: ignore[attr-defined]
    filter_module.HandlerFilter = type("HandlerFilter", (), {})
    sys.modules["astrbot.core.star.filter"] = filter_module

    star_module = types.ModuleType("astrbot.core.star.star")
    star_module.__path__ = []  # type: ignore[attr-defined]
    star_module.star_map = {}
    star_module.star_registry = []
    star_module.StarMetadata = type("StarMetadata", (), {})
    sys.modules["astrbot.core.star.star"] = star_module


def _load_real_context(src: Path):
    """加载真实 Context 类；仅缺失的依赖模块用鸭子类型桩补齐。"""
    context_path = src / "astrbot" / "core" / "star" / "context.py"
    tree = ast.parse(context_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0 and node.module and node.module.startswith("astrbot"):
            _stub_module(node.module)
        elif node.level == 1 and node.module:
            _stub_module(f"astrbot.core.star.{node.module}")
        elif node.level == 2 and node.module:
            _stub_module(f"astrbot.core.{node.module}")

    # 真实 star_handler / star_map 已在上一步加载，不要覆盖。
    spec = importlib.util.spec_from_file_location(
        "astrbot.core.star.context_real", context_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    src = _find_astrbot_src()
    if src is None:
        print("ASTRBOT_SRC_NOT_FOUND")
        return 77

    _bootstrap(src)

    plugin_main = __import__(
        "astrbot_plugin_proactive_chat.main", fromlist=["ProactiveChatPlugin"]
    )
    star_handler = __import__(
        "astrbot.core.star.star_handler", fromlist=["StarHandlerRegistry"]
    )
    real_web = __import__(
        "astrbot.api.web", fromlist=["PluginRequest", "bind_request_context"]
    )
    context_module = _load_real_context(src)
    Context = context_module.Context

    # ---------------------------------------------------------------
    # 1) 真实 StarHandlerRegistry 的 plugin_set 过滤
    # ---------------------------------------------------------------
    star_map = sys.modules["astrbot.core.star.star"].star_map
    star_map.clear()
    star_map["astrbot_plugin_proactive_chat.main"] = SimpleNamespace(
        name="astrbot_plugin_proactive_chat",
        activated=True,
        reserved=False,
    )

    handler = star_handler.StarHandlerMetadata(
        event_type=star_handler.EventType.AdapterMessageEvent,
        handler_full_name=(
            "astrbot_plugin_proactive_chat.main_ProactiveChatPlugin_on_friend_message"
        ),
        handler_name="on_friend_message",
        handler_module_path="astrbot_plugin_proactive_chat.main",
        handler=plugin_main.ProactiveChatPlugin.on_friend_message,
        event_filters=[],
        extras_configs={"priority": 999},
    )
    loaded_handler = star_handler.StarHandlerMetadata(
        event_type=star_handler.EventType.OnAstrBotLoadedEvent,
        handler_full_name=(
            "astrbot_plugin_proactive_chat.main_ProactiveChatPlugin_on_astrbot_loaded"
        ),
        handler_name="on_astrbot_loaded",
        handler_module_path="astrbot_plugin_proactive_chat.main",
        handler=plugin_main.ProactiveChatPlugin.on_astrbot_loaded,
        event_filters=[],
        extras_configs={},
    )

    registry = star_handler.StarHandlerRegistry()
    registry.append(handler)
    registry.append(loaded_handler)

    filtered_out = registry.get_handlers_by_event_type(
        star_handler.EventType.AdapterMessageEvent,
        plugins_name=["some_other_plugin"],
    )
    filtered_in = registry.get_handlers_by_event_type(
        star_handler.EventType.AdapterMessageEvent,
        plugins_name=["astrbot_plugin_proactive_chat"],
    )
    loaded_exempt = registry.get_handlers_by_event_type(
        star_handler.EventType.OnAstrBotLoadedEvent,
        plugins_name=["some_other_plugin"],
    )

    assert filtered_out == []
    assert filtered_in == [handler]
    assert loaded_exempt == [loaded_handler]

    # ---------------------------------------------------------------
    # 2) 真实 Context.register_web_api + PluginRequest 路由
    # ---------------------------------------------------------------
    from astrbot_plugin_proactive_chat.core.console.page_api import PageApiRoutes

    context_obj = Context.__new__(Context)
    context_obj.registered_web_apis = []

    def register_web_api(route, view_handler, methods, desc):
        return Context.register_web_api(
            context_obj, route, view_handler, methods, desc
        )

    fake_plugin = SimpleNamespace(
        config={
            "private_settings": {"enable": True, "session_list": ["u1"]},
            "web_admin": {"host": "127.0.0.1", "port": 4100},
            "notification_settings": {},
            "telemetry_config": {},
        },
        timezone=__import__("datetime").timezone.utc,
        version="real-it",
        context=SimpleNamespace(
            register_web_api=register_web_api,
            registered_web_apis=context_obj.registered_web_apis,
        ),
        config_service=SimpleNamespace(
            list_chat_providers=lambda: [
                {"id": "p1", "label": "p1 · model", "model": "model", "type": "x"}
            ]
        ),
    )
    routes = PageApiRoutes(fake_plugin)
    assert routes.register() is True
    route_names = [item[0] for item in context_obj.registered_web_apis]
    assert "/astrbot_plugin_proactive_chat/providers" in route_names
    assert "/astrbot_plugin_proactive_chat/jobs/trigger" in route_names
    route_methods = {
        (item[0], tuple(item[2])) for item in context_obj.registered_web_apis
    }
    assert len(route_methods) == len(context_obj.registered_web_apis)

    # 真实 PluginRequest + bind_request_context 调用页面处理器
    from starlette.requests import Request

    def make_request(method: str, path: str, body: bytes = b""):
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "root_path": "",
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return real_web.PluginRequest(
            Request(scope, receive), plugin_name="astrbot_plugin_proactive_chat"
        )

    with real_web.bind_request_context(
        make_request("GET", "/astrbot_plugin_proactive_chat/config")
    ):
        config_response = asyncio.run(routes.get_config())
    config_payload = json.loads(config_response.body)
    assert config_payload["displayTimezone"] == "UTC"
    assert config_payload["private_settings"]["enable"] is True

    with real_web.bind_request_context(
        make_request("GET", "/astrbot_plugin_proactive_chat/providers")
    ):
        providers_response = asyncio.run(routes.list_providers())
    assert json.loads(providers_response.body)["providers"][0]["id"] == "p1"

    routes.unregister()
    assert context_obj.registered_web_apis == []

    # ---------------------------------------------------------------
    # 3) 同一会话并发触发互斥
    # ---------------------------------------------------------------
    async def concurrency_check():
        plugin = SimpleNamespace(
            _terminating=False,
            manual_trigger_sessions=set(),
            active_trigger_sessions=set(),
            session_service=SimpleNamespace(
                normalize_session_id=lambda umo: umo,
                parse_session_id=lambda umo: ("qq_official", "FriendMessage", "u1"),
            ),
            web_admin_server=None,
        )
        orchestrator = plugin_main.FlowOrchestrator(plugin)
        calls = []

        async def fake_run_locked(normalized_session_id, trigger):
            calls.append((normalized_session_id, trigger))
            await asyncio.sleep(0.05)

        orchestrator._run_locked = fake_run_locked  # type: ignore[method-assign]
        await asyncio.gather(
            orchestrator.run("qq_official:FriendMessage:u1"),
            orchestrator.run("qq_official:FriendMessage:u1"),
        )
        return calls

    calls = asyncio.run(concurrency_check())
    assert len(calls) == 1, calls

    print(
        json.dumps(
            {
                "astrbot_src": str(src),
                "plugin_set_filtered_out": len(filtered_out),
                "plugin_set_filtered_in": len(filtered_in),
                "loaded_event_exempt": len(loaded_exempt),
                "page_routes": len(route_names),
                "concurrent_runs": len(calls),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
