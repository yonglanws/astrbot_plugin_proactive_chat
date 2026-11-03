"""领域服务层：承载业务逻辑，通过容器注入共享状态。

为了避免测试或工具仅导入单个服务时被迫加载 AstrBot / apscheduler 等
运行时依赖，本包改用惰性导出：首次访问某个服务名时才导入对应模块。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ConfigService": "config_service",
    "ContextService": "context_service",
    "ConversationMemoryService": "conversation_memory_service",
    "ConversationStateService": "conversation_state_service",
    "CriticService": "generation_service",
    "GuardService": "guard_service",
    "IntensityService": "intensity_service",
    "LifecycleService": "lifecycle_service",
    "LlmService": "llm_service",
    "MemoryBridge": "memory_bridge",
    "PersonaStateService": "persona_state_service",
    "PlannerService": "generation_service",
    "RhythmService": "rhythm_service",
    "SchedulerService": "scheduler_service",
    "SenderService": "sender_service",
    "SessionService": "session_service",
    "StorageService": "storage_service",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
