"""会话数据持久化与键规范化。

由旧 core/data_storage.py 等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from typing import Any

import aiofiles
import aiofiles.os as aio_os

from astrbot.api import logger


_SNAPSHOT_CHUNK_SIZE = 64
_MISSING = object()


def _clone_json_value(value: Any) -> Any:
    """快速浅树拷贝 JSON 值，避免全量 copy.deepcopy 的 dispatch/memo 开销。"""
    value_type = type(value)
    if value_type is dict:
        return {k: _clone_json_value(v) for k, v in value.items()}
    if value_type is list:
        return [_clone_json_value(item) for item in value]
    if value_type in (str, int, float, bool) or value is None:
        return value
    return copy.deepcopy(value)


async def _snapshot_session_data(session_data: dict) -> dict:
    """分片合作式快照：每个会话原子克隆，片间让出事件循环以免阻塞心跳与网关。"""
    if not isinstance(session_data, dict):
        return _clone_json_value(session_data)
    keys = list(session_data.keys())
    snapshot: dict = {}
    for index, key in enumerate(keys):
        value = session_data.get(key, _MISSING)
        if value is not _MISSING:
            snapshot[key] = _clone_json_value(value)
        if (index + 1) % _SNAPSHOT_CHUNK_SIZE == 0:
            await asyncio.sleep(0)
    return snapshot


class SessionStore:
    """负责 session_data.json 的读写、键规范化与无效数据清理。"""

    def __init__(self, data_dir: Any, filename: str = "session_data.json") -> None:
        self.data_dir = data_dir
        self.session_data_file = data_dir / filename

    async def load(self) -> dict:
        """从文件加载会话数据。"""
        if await aio_os.path.exists(self.session_data_file):
            try:
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    loaded_data = await asyncio.to_thread(json.loads, content)
                    if isinstance(loaded_data, dict):
                        return loaded_data
                    logger.warning(
                        "[主动消息] 会话数据文件结构无效喵，根节点应为对象，将使用空数据启动喵。"
                    )
                    return {}
            except (OSError, json.JSONDecodeError) as e:
                logger.error(
                    f"[主动消息] 加载会话数据失败喵: {e}，将使用空数据启动喵。"
                )
                return {}
        return {}

    async def save(self, session_data: dict) -> bool:
        """将会话数据原子保存到文件，返回是否成功。

        先写入同目录临时文件，再用 ``os.replace`` 原子替换目标文件。
        这样即使进程在写入过程中崩溃/断电，也不会留下半截 JSON 把
        全部会话计数、排期与强度状态一起丢掉。
        """
        temp_file = self.session_data_file.with_name(
            f"{self.session_data_file.name}.{uuid.uuid4().hex}.tmp"
        )
        # 分片克隆每条会话后交由线程池序列化与原子落盘，既不阻塞事件循环，
        # 也不在 json.dumps 期间受到并发变更的 RuntimeError 干扰。
        snapshot = await _snapshot_session_data(session_data)

        def _write_atomic() -> None:
            """同步写临时文件并原子替换；在线程池中执行避免阻塞事件循环。"""
            self.data_dir.mkdir(parents=True, exist_ok=True)
            content_to_write = json.dumps(
                snapshot, indent=4, ensure_ascii=False
            )
            with temp_file.open("w", encoding="utf-8") as f:
                f.write(content_to_write)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    # 某些文件系统不支持 fsync，不影响原子替换语义。
                    pass
            os.replace(temp_file, self.session_data_file)

        try:
            await asyncio.to_thread(_write_atomic)
            return True
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败喵: {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except OSError:
                pass
            return False

    # ------------------------------------------------------------------
    # 合并与规范化（保留旧逻辑以兼容历史数据）
    # ------------------------------------------------------------------
    @staticmethod
    def merge_session_info(base: dict, incoming: dict) -> dict:
        """合并两份会话数据，避免重复任务与计数错乱。

        不同字段采用不同合并策略。计数类字段取较大值，避免已发送次数被回退。
        时间类字段取较晚值，保证调度语义向前推进。
        间隔三元组以 last_scheduled_at 较晚的一方为准。
        """
        merged = base.copy()
        base_paused_at = (
            float(base.get("paused_at", 0) or 0)
            if isinstance(base.get("paused_at"), (int, float))
            else 0.0
        )
        incoming_paused_at = (
            float(incoming.get("paused_at", 0) or 0)
            if isinstance(incoming.get("paused_at"), (int, float))
            else 0.0
        )
        base_msg_ts = (
            float(base.get("last_message_time", 0) or 0)
            if isinstance(base.get("last_message_time"), (int, float))
            else 0.0
        )
        incoming_msg_ts = (
            float(incoming.get("last_message_time", 0) or 0)
            if isinstance(incoming.get("last_message_time"), (int, float))
            else 0.0
        )
        base_scheduled_at = base.get("last_scheduled_at")
        incoming_scheduled_at = incoming.get("last_scheduled_at")
        base_sched_ts = (
            float(base_scheduled_at)
            if isinstance(base_scheduled_at, (int, float))
            else 0.0
        )
        incoming_sched_ts = (
            float(incoming_scheduled_at)
            if isinstance(incoming_scheduled_at, (int, float))
            else 0.0
        )

        for key in [
            "self_id",
            "last_sender_id",
            "last_sender_name",
            "session_name",
            "last_message_time",
            "unanswered_count",
            "next_trigger_time",
            "last_scheduled_at",
            "last_schedule_min_interval_seconds",
            "last_schedule_max_interval_seconds",
            "last_schedule_random_interval_seconds",
            "last_schedule_intensity_score",
            "schedule_cancelled",
            "intensity",
            "rhythm",
            "conversation_state",
            "conversation_memory",
            "persona_state",
            "cooldown_until",
            "last_proactive_texts",
            "last_reply_quality",
            "last_guard",
            "last_critic",
            "proactive_paused",
            "paused_at",
            "pause_reason",
            "paused_unanswered_count",
            "paused_threshold",
            "last_topic_judge",
        ]:
            if key not in incoming:
                continue
            if key not in merged:
                merged[key] = incoming[key]
                continue

            # 暂停状态只要任一侧存在就保持暂停，避免合并旧键时意外恢复。
            if key == "proactive_paused":
                merged[key] = bool(merged.get(key) or incoming.get(key))
                continue

            if key in {"paused_at"}:
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            if key == "pause_reason":
                if incoming.get(key) and (
                    not merged.get(key) or incoming_paused_at >= base_paused_at
                ):
                    merged[key] = incoming[key]
                continue

            if key in {"paused_unanswered_count", "paused_threshold"}:
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            if key == "schedule_cancelled":
                if incoming_sched_ts > base_sched_ts and not incoming.get(key):
                    merged[key] = False
                elif base_sched_ts > incoming_sched_ts and not merged.get(key):
                    merged[key] = False
                else:
                    merged[key] = bool(merged.get(key) or incoming.get(key))
                continue

            if key in {"last_sender_id", "last_sender_name"}:
                if incoming.get(key) and (
                    not merged.get(key) or incoming_msg_ts >= base_msg_ts
                ):
                    merged[key] = incoming[key]
                continue

            if key == "session_name":
                if incoming.get(key) and not merged.get(key):
                    merged[key] = incoming[key]
                continue

            if key == "last_topic_judge":
                base_at = (
                    merged[key].get("at", 0)
                    if isinstance(merged[key], dict)
                    else 0
                )
                incoming_at = (
                    incoming[key].get("at", 0)
                    if isinstance(incoming[key], dict)
                    else 0
                )
                if not isinstance(merged[key], dict) or incoming_at >= base_at:
                    merged[key] = incoming[key]
                continue

            # 未回复计数取较大值，防止计数被历史数据覆盖变小。
            if key == "unanswered_count":
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            # 时间字段取较晚者，确保调度时序单调向前。
            if key in {"last_message_time", "next_trigger_time", "last_scheduled_at"}:
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            # 强度状态以 last_user_ts 较晚的一方为准，避免合并旧键时丢失最新样本。
            if key == "intensity":
                base_intensity = merged.get("intensity")
                incoming_intensity = incoming.get("intensity")
                if not isinstance(base_intensity, dict):
                    merged["intensity"] = incoming_intensity
                    continue
                if not isinstance(incoming_intensity, dict):
                    continue
                base_ts = float(base_intensity.get("last_user_ts", 0) or 0)
                incoming_ts = float(incoming_intensity.get("last_user_ts", 0) or 0)
                if incoming_ts >= base_ts:
                    merged["intensity"] = incoming_intensity
                continue

            # 作息状态以最近更新时间较晚的一方为准。
            if key == "rhythm":
                base_rhythm = merged.get("rhythm")
                incoming_rhythm = incoming.get("rhythm")
                if not isinstance(base_rhythm, dict):
                    merged["rhythm"] = incoming_rhythm
                    continue
                if not isinstance(incoming_rhythm, dict):
                    continue
                if float(incoming_rhythm.get("last_update_ts", 0) or 0) >= float(
                    base_rhythm.get("last_update_ts", 0) or 0
                ):
                    merged["rhythm"] = incoming_rhythm
                continue

            # 关系状态取最近事件更晚的一方。
            if key == "conversation_state":
                base_state = merged.get("conversation_state")
                incoming_state = incoming.get("conversation_state")
                if not isinstance(base_state, dict):
                    merged["conversation_state"] = incoming_state
                    continue
                if not isinstance(incoming_state, dict):
                    continue
                base_ts = max(
                    float(base_state.get("last_user_at", 0) or 0),
                    float(base_state.get("last_proactive_at", 0) or 0),
                )
                incoming_ts = max(
                    float(incoming_state.get("last_user_at", 0) or 0),
                    float(incoming_state.get("last_proactive_at", 0) or 0),
                )
                if incoming_ts >= base_ts:
                    merged["conversation_state"] = incoming_state
                continue

            # 人格状态以 updated_at 较晚的一方为准。
            if key == "persona_state":
                base_persona = merged.get("persona_state")
                incoming_persona = incoming.get("persona_state")
                if not isinstance(base_persona, dict):
                    merged["persona_state"] = incoming_persona
                    continue
                if not isinstance(incoming_persona, dict):
                    continue
                if float(incoming_persona.get("updated_at", 0) or 0) >= float(
                    base_persona.get("updated_at", 0) or 0
                ):
                    merged["persona_state"] = incoming_persona
                continue

            # 短期工作记忆取最近更新时间更晚的一方。
            if key == "conversation_memory":
                base_memory = merged.get("conversation_memory")
                incoming_memory = incoming.get("conversation_memory")
                if not isinstance(base_memory, dict):
                    merged["conversation_memory"] = incoming_memory
                    continue
                if not isinstance(incoming_memory, dict):
                    continue
                if float(incoming_memory.get("last_updated", 0) or 0) >= float(
                    base_memory.get("last_updated", 0) or 0
                ):
                    merged["conversation_memory"] = incoming_memory
                continue

            # 负反馈冷却取更晚的截止时间，避免合并时被旧值缩短。
            if key == "cooldown_until":
                if not isinstance(merged[key], (int, float)):
                    merged[key] = incoming[key]
                elif isinstance(incoming[key], (int, float)):
                    merged[key] = max(merged[key], incoming[key])
                continue

            if key == "last_guard":
                base_guard = merged.get("last_guard")
                incoming_guard = incoming.get("last_guard")
                if not isinstance(base_guard, dict):
                    merged["last_guard"] = incoming_guard
                    continue
                if not isinstance(incoming_guard, dict):
                    continue
                if float(incoming_guard.get("at", 0) or 0) >= float(
                    base_guard.get("at", 0) or 0
                ):
                    merged["last_guard"] = incoming_guard
                continue

            if key == "last_critic":
                base_critic = merged.get("last_critic")
                incoming_critic = incoming.get("last_critic")
                if not isinstance(base_critic, dict):
                    merged["last_critic"] = incoming_critic
                    continue
                if not isinstance(incoming_critic, dict):
                    continue
                if float(incoming_critic.get("at", 0) or 0) >= float(
                    base_critic.get("at", 0) or 0
                ):
                    merged["last_critic"] = incoming_critic
                continue

            # 最近主动文本保留更长的一份，避免合并时丢失去重历史。
            if key == "last_proactive_texts":
                base_texts = merged.get("last_proactive_texts")
                incoming_texts = incoming.get("last_proactive_texts")
                if not isinstance(base_texts, list):
                    merged["last_proactive_texts"] = incoming_texts
                elif isinstance(incoming_texts, list) and len(incoming_texts) >= len(
                    base_texts
                ):
                    merged["last_proactive_texts"] = incoming_texts
                continue

            if key == "last_reply_quality":
                if incoming.get(key) not in (None, "") and (
                    merged.get(key) in (None, "") or incoming_msg_ts >= base_msg_ts
                ):
                    merged[key] = incoming[key]
                continue

            # 间隔三元组与 last_scheduled_at 绑定，采用调度时间较晚的一方。
            if key in {
                "last_schedule_min_interval_seconds",
                "last_schedule_max_interval_seconds",
                "last_schedule_random_interval_seconds",
                "last_schedule_intensity_score",
            }:
                if isinstance(base_scheduled_at, (int, float)) and isinstance(
                    incoming_scheduled_at, (int, float)
                ):
                    if incoming_scheduled_at >= base_scheduled_at:
                        merged[key] = incoming[key]
                elif key not in merged or not merged.get(key):
                    merged[key] = incoming[key]
                continue

            if merged[key] is None:
                merged[key] = incoming[key]
        return merged
