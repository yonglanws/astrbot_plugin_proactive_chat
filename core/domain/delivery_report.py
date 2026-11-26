"""发送结果数据模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 永久性投递失败模式（无好友关系、无权限、被拉黑/拒收等）：重试不会恢复，
# 若不暂停会话会导致定时任务无限重调度并持续消耗 LLM Token。
_PERMANENT_DELIVERY_ERROR_RE = re.compile(
    r"(无好友关系|不是好友|非好友|已删除好友|不存在好友|"
    r"无权限|权限不足|禁止私聊|已拉黑|被拉黑|对方拒收|拒绝接收|拒收|"
    r"账号已注销|用户不存在|不存在该用户|"
    r"no\s+friend\s+relation|not\s+(?:a\s+)?friend|permission\s+denied|"
    r"forbidden|blocked\s+by\s+user|user\s+not\s+found|"
    r"retcode\s*[:=]\s*1200|errcode\s*[:=]\s*(?:11292|11253))",
    re.IGNORECASE,
)


def is_permanent_delivery_error(error_message: str) -> bool:
    """判断投递异常文本是否属于永久性失败（不应继续自动重试）。"""
    text = str(error_message or "").strip()
    if not text:
        return False
    return bool(_PERMANENT_DELIVERY_ERROR_RE.search(text))


@dataclass
class DeliveryReport:
    """一次主动消息发送的汇总结果。"""

    sent: bool = False
    # 是否成功发出 TTS 语音。
    voice_sent: bool = False
    # 是否执行了分段。
    segmented: bool = False
    # 实际发送的分段数量。
    segment_count: int = 0
    # 装饰钩子是否终止了发送。
    stopped_by_hook: bool = False
    # 是否在气泡等待/发送中途因用户新消息或关闭而中止。
    aborted_midway: bool = False
    # 发送失败时的原始错误摘要（用于识别永久性失败并暂停会话）。
    error_message: str = ""
    # 是否命中永久性投递失败（如无好友关系/无权限）。
    permanent_failure: bool = False
    # 实际送出的气泡文本列表。
    delivered_bubbles: list[str] = field(default_factory=list)
    # 扩展信息。
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def any_sent(self) -> bool:
        return bool(self.sent or self.voice_sent or self.delivered_bubbles)

    @property
    def delivered_text(self) -> str:
        return "\n".join(b for b in self.delivered_bubbles if b)

    def __bool__(self) -> bool:
        return self.any_sent
