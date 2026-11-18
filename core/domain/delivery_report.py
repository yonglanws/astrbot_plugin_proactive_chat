"""发送结果数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
