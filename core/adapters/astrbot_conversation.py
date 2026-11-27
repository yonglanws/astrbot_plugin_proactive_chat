"""AstrBot 对话历史适配器：读取、存档与回读校验。

由旧 core/chat_flow.py 与 core/llm_adapter.py 中与
conversation_manager 交互的逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Any

from astrbot.api import logger

from ..domain.content_text import reduce_record_text, strip_sticker_tags

_CONV_LOCKS: OrderedDict[str, asyncio.Lock] = OrderedDict()
_MAX_CONV_LOCKS = 512


def _get_conv_lock(conv_id: str) -> asyncio.Lock:
    key = str(conv_id)
    lock = _CONV_LOCKS.get(key)
    if lock is not None:
        _CONV_LOCKS.move_to_end(key)
        return lock
    lock = asyncio.Lock()
    _CONV_LOCKS[key] = lock
    if len(_CONV_LOCKS) > _MAX_CONV_LOCKS:
        for old_key in list(_CONV_LOCKS.keys()):
            if old_key != key and not _CONV_LOCKS[old_key].locked():
                _CONV_LOCKS.pop(old_key, None)
                if len(_CONV_LOCKS) <= _MAX_CONV_LOCKS:
                    break
    return lock

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        TextPart,
        UserMessageSegment,
    )
except ImportError:  # pragma: no cover
    AssistantMessageSegment = None  # type: ignore[assignment]
    TextPart = None  # type: ignore[assignment]
    UserMessageSegment = None  # type: ignore[assignment]


def extract_record_text(content: Any) -> str:
    """从对话历史记录的 content 字段中提取纯文本。

    直接复用 domain 层的文本归约实现，保持全项目语义一致。
    """
    return reduce_record_text(content)


async def verify_message_persisted(
    conversation_manager: Any,
    session_id: str,
    conv_id: str,
    assistant_response: str,
) -> bool:
    """回读对话记录，校验本次主动消息是否真正落盘。

    从历史末尾向前查找最近一条 assistant 记录并做包含匹配；
    任何异常或未命中都返回 False。
    """
    target = strip_sticker_tags(assistant_response)
    if not target:
        return False
    try:
        conversation = await conversation_manager.get_conversation(session_id, conv_id)
        if not conversation or not conversation.history:
            return False
        history = conversation.history
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list):
            return False
        for record in reversed(history):
            if not isinstance(record, dict) or record.get("role") != "assistant":
                continue
            extracted = reduce_record_text(record.get("content", ""))
            if not extracted.strip():
                continue
            return target in extracted
        return False
    except Exception as e:
        logger.debug(f"[主动消息] 回读校验对话历史失败喵: {e}")
        return False


async def add_message_pair(
    conversation_manager: Any,
    conv_id: str,
    user_prompt: str,
    assistant_response: str,
) -> None:
    """存档一对 user/assistant 消息到对话历史。

    依赖 AstrBot 的消息段 API；旧版本缺失时抛出运行时错误。
    """
    if UserMessageSegment is None or TextPart is None:
        raise RuntimeError("当前 AstrBot 版本缺少消息段 API")
    clean_response = strip_sticker_tags(assistant_response)
    if not clean_response:
        return
    user_msg_obj = UserMessageSegment(content=[TextPart(text=user_prompt)])
    assistant_msg_obj = AssistantMessageSegment(
        content=[TextPart(text=clean_response)]
    )
    await conversation_manager.add_message_pair(
        cid=conv_id,
        user_message=user_msg_obj,
        assistant_message=assistant_msg_obj,
    )


async def add_assistant_message(
    conversation_manager: Any,
    conv_id: str,
    assistant_response: str,
) -> None:
    """只把主动消息作为 assistant 记录存档，不再伪造 user 消息。

    旧实现会把替换后的完整主动提示词当作 user 消息写入 AstrBot 对话历史，
    既污染 Dashboard 与后续上下文，也浪费大量 token。优先直接追加 assistant
    记录；极旧 AstrBot 缺少 DB 接口时才退回“短占位 user + assistant”的兼容写法。
    """
    if AssistantMessageSegment is None or TextPart is None:
        raise RuntimeError("当前 AstrBot 版本缺少消息段 API")

    clean_response = strip_sticker_tags(assistant_response)
    if not clean_response:
        return

    assistant_msg_obj = AssistantMessageSegment(
        content=[TextPart(text=clean_response)]
    )
    assistant_msg_dict = assistant_msg_obj.model_dump()

    db = getattr(conversation_manager, "db", None)
    get_conversation = getattr(db, "get_conversation_by_id", None)
    update_conversation = getattr(db, "update_conversation", None)
    if callable(get_conversation) and callable(update_conversation):
        async with _get_conv_lock(conv_id):
            conversation = await get_conversation(cid=conv_id)
            if not conversation:
                raise RuntimeError(f"Conversation with id {conv_id} not found")
            history = getattr(conversation, "content", None) or []
            if isinstance(history, str):
                try:
                    history = await asyncio.to_thread(json.loads, history)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("[主动消息] 存档前解析对话历史 JSON 失败，将重置为列表喵。")
                    history = []
            if not isinstance(history, list):
                history = []
            else:
                history = list(history)
            history.append(assistant_msg_dict)
            await update_conversation(cid=conv_id, content=history)
        return

    # 兼容兜底：使用极短占位符，绝不写入完整系统提示词。
    await add_message_pair(
        conversation_manager,
        conv_id,
        "[主动消息]",
        clean_response,
    )


async def load_conversation_history(conversation: Any) -> list:
    """从 conversation 对象解析对话历史列表。

    兼容 history 为字符串（JSON）或已反序列化列表两种形态；
    解析失败或格式异常时一律回退为空列表。
    """
    pure_history_messages: list = []
    if conversation and conversation.history:
        try:
            if isinstance(conversation.history, str):
                pure_history_messages = await asyncio.to_thread(
                    json.loads, conversation.history
                )
            else:
                pure_history_messages = conversation.history
        except (json.JSONDecodeError, TypeError):
            logger.warning("[主动消息] 解析历史记录失败，使用空历史喵。")

    if not isinstance(pure_history_messages, list):
        logger.warning("[主动消息] 历史记录格式异常（非列表），已回退为空历史喵。")
        pure_history_messages = []
    return pure_history_messages
