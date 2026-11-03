"""框架适配层：隔离 AstrBot 框架细节。

本层负责与 AstrBot 的事件、平台、对话、Provider 等 API 交互，
向上层 services / features 暴露稳定接口，避免框架细节侵入领域逻辑。

为便于离线工具与测试，导入单个适配器模块时不应强制加载其他适配器，
因此这里使用惰性导出。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ProactiveMessageEvent": ("astrbot_event", "ProactiveMessageEvent"),
    "build_proactive_event_for_session": (
        "astrbot_event",
        "build_proactive_event_for_session",
    ),
    "dispatch_event_hook": ("astrbot_event", "dispatch_event_hook"),
    "find_platform_by_id": ("astrbot_platform", "find_platform_by_id"),
    "iter_active_platform_instances": (
        "astrbot_platform",
        "iter_active_platform_instances",
    ),
    "resolve_platform_instance": ("astrbot_platform", "resolve_platform_instance"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attribute)
    globals()[name] = value
    return value
