#!/usr/bin/env python3
"""离线回放拟人化策略。

用 AstrBot 数据库中的私聊消息时间线，模拟如果启用当前的：
- 互动强度概率；
- 作息/每日预算（简化）；
- 回复质量（敷衍/负反馈不计入回复率）；
- 连续未回复暂停；
会得到多少次主动消息、多少次概率未命中、多少次暂停，以及回复率变化。

数据库只读打开，不会修改原文件。

用法::

    python tools/replay_humanization.py --db /path/to/data_v4.db --report replay.json
    python tools/replay_humanization.py --db data_v4.db --session qq_official:FriendMessage:xxx
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TOOLS_DIR.parent
for path in (PLUGIN_ROOT, PLUGIN_ROOT.parent, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import import_astrbot_history as importer  # noqa: E402

from astrbot_plugin_proactive_chat.core.domain.humanization import (  # noqa: E402
    ReplyQuality,
    classify_reply,
)
from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey  # noqa: E402
from astrbot_plugin_proactive_chat.core.services.intensity_service import (  # noqa: E402
    IntensityService,
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "enable": True,
    "schedule_settings": {
        "min_interval_minutes": 30,
        "max_interval_minutes": 600,
        "max_unanswered_times": 4,
        "quiet_hours": "1-7",
    },
    "intensity_settings": {
        "enable": True,
        "probability_min": 15,
        "probability_max": 95,
        "response_window_hours": 6,
        "skip_retry_ratio": 20,
        "min_skip_retry_minutes": 5,
    },
    "humanization_settings": {
        "rhythm_settings": {"enable": True, "min_gap_minutes": 30},
        "feedback_settings": {
            "enable": True,
            "negative_cooldown_hours": 24,
            "perfunctory_no_reset": True,
        },
    },
}


class FakeSessionService:
    def parse_session_id(self, umo: str):
        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo: str) -> str:
        return umo

    def get_session_name(self, umo: str, config=None) -> str:
        return ""

    def get_session_display_name(self, umo: str, config=None) -> str:
        return umo

    def get_session_log_str(self, umo: str, config=None) -> str:
        return umo


class FakeConfigService:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def get_session_config(self, umo: str):
        return {**self.settings, "enable": True, "_session_type": "friend"}

    def get_private_settings(self):
        return self.settings


class FakePlugin:
    def __init__(self, settings: dict[str, Any], tz: ZoneInfo):
        self.timezone = tz
        self.session_data: dict[str, dict[str, Any]] = {}
        self.config = {"private_settings": settings}
        self.session_service = FakeSessionService()
        self.config_service = FakeConfigService(settings)
        self.intensity_service = IntensityService(self)
        # 离线回放不注册真实调度器，decide() 会自动跳过暂停检查。
        self.scheduler_service = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线回放拟人化主动消息策略")
    parser.add_argument("--db", required=True, help="AstrBot data_v4.db 路径")
    parser.add_argument("--platform", default="", help="只回放指定平台前缀")
    parser.add_argument("--session", default="", help="只回放指定 UMO")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="时区")
    parser.add_argument("--limit", type=int, default=50, help="最多回放多少个会话")
    parser.add_argument("--min-messages", type=int, default=10, help="最少用户消息数")
    parser.add_argument("--seed", type=int, default=7, help="随机种子")
    parser.add_argument("--report", default="replay_report.json", help="报告输出路径")
    parser.add_argument(
        "--settings",
        default="",
        help="可选：JSON 配置文件（覆盖默认 intensity/schedule/humanization 配置）",
    )
    return parser.parse_args(argv)


def load_settings(path: str) -> dict[str, Any]:
    if not path:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings 必须是 JSON 对象")
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _negative_cooldown_seconds(settings: dict[str, Any]) -> float:
    humanization = settings.get("humanization_settings", {})
    feedback = (
        humanization.get("feedback_settings", {})
        if isinstance(humanization, dict)
        else {}
    )
    try:
        hours = float(feedback.get("negative_cooldown_hours", 24) or 24)
    except (TypeError, ValueError):
        hours = 24.0
    return max(3600.0, hours * 3600.0)


def replay_session(
    umo: str,
    messages: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    tz: ZoneInfo,
    seed: int = 7,
    max_attempts: int = 500,
) -> dict[str, Any]:
    """回放一个私聊会话，返回可解释的统计。"""
    if not messages:
        return {"session_id": umo, "user_messages": 0}

    plugin = FakePlugin(settings, tz)
    service = plugin.intensity_service
    session_config = plugin.config_service.get_session_config(umo)
    rng = random.Random(seed)
    schedule_conf = session_config.get("schedule_settings", {})

    base_min_seconds = max(
        60, int(schedule_conf.get("min_interval_minutes", 30) or 30) * 60
    )
    max_unanswered = int(
        schedule_conf.get("max_unanswered_times", 4) or 0
    )
    cooldown_seconds = _negative_cooldown_seconds(settings)

    metrics = {
        "session_id": umo,
        "user_messages": len(messages),
        "attempts": 0,
        "sent": 0,
        "probability_miss": 0,
        "pauses": 0,
        "negative_replies": 0,
        "perfunctory_replies": 0,
        "substantive_replies": 0,
        "media_replies": 0,
        "cooldown_hits": 0,
        "sent_times": [],
        "attempt_hours": Counter(),
        "reply_quality": Counter(),
    }

    unanswered = 0
    paused = False
    cooldown_until = 0.0
    next_attempt_at = float(messages[0]["t"]) + base_min_seconds
    attempts = 0

    def simulate_before(timestamp: float) -> None:
        nonlocal next_attempt_at, unanswered, paused, attempts
        while next_attempt_at < timestamp and attempts < max_attempts:
            attempts += 1
            metrics["attempts"] += 1
            if paused:
                break
            if cooldown_until and next_attempt_at < cooldown_until:
                metrics["cooldown_hits"] += 1
                next_attempt_at = cooldown_until
                continue
            if max_unanswered > 0 and unanswered >= max_unanswered:
                paused = True
                metrics["pauses"] += 1
                break
            will_send, snapshot = service.decide(
                umo,
                session_config,
                trigger="schedule",
                now=next_attempt_at,
                rng=rng,
            )
            if will_send:
                service.record_proactive_sent(umo, now=next_attempt_at)
                unanswered += 1
                metrics["sent"] += 1
                metrics["sent_times"].append(round(next_attempt_at, 3))
                metrics["attempt_hours"][
                    datetime.fromtimestamp(next_attempt_at, tz).hour
                ] += 1
                interval = service.next_interval(
                    umo,
                    schedule_conf,
                    session_config=session_config,
                    now=next_attempt_at,
                    rng=rng,
                )
                next_attempt_at += max(60, int(interval["random_seconds"]))
            else:
                metrics["probability_miss"] += 1
                next_attempt_at += max(
                    60, int(snapshot.get("retry_after_seconds", 300) or 300)
                )

    for item in messages:
        timestamp = float(item.get("t", 0) or 0)
        if timestamp <= 0:
            continue
        simulate_before(timestamp)
        text = str(item.get("text") or "")
        quality = classify_reply(text)
        metrics["reply_quality"][quality.value] += 1
        if quality is ReplyQuality.NEGATIVE:
            metrics["negative_replies"] += 1
        elif quality is ReplyQuality.PERFUNCTORY:
            metrics["perfunctory_replies"] += 1
        elif quality is ReplyQuality.MEDIA:
            metrics["media_replies"] += 1
        else:
            metrics["substantive_replies"] += 1

        counts_as_reply = quality not in {
            ReplyQuality.NEGATIVE,
            ReplyQuality.PERFUNCTORY,
        }
        service.record_user_message(
            umo,
            text,
            now=timestamp,
            settings=service.get_settings(session_config),
            counts_as_reply=counts_as_reply,
        )

        if quality is ReplyQuality.NEGATIVE:
            paused = True
            cooldown_until = max(cooldown_until, timestamp + cooldown_seconds)
            next_attempt_at = max(next_attempt_at, cooldown_until)
        elif quality is ReplyQuality.PERFUNCTORY:
            # 敷衍不清零，继续累计未回复；达到阈值后暂停。
            if max_unanswered > 0 and unanswered >= max_unanswered:
                paused = True
                metrics["pauses"] += 1
        else:
            unanswered = 0
            if paused:
                paused = False
            next_attempt_at = max(
                next_attempt_at, timestamp + base_min_seconds
            )

    response_window_hours = float(
        session_config.get("intensity_settings", {}).get(
            "response_window_hours", 6
        )
        or 6
    )
    evaluation_time = messages[-1]["t"] + max(3600.0, response_window_hours * 3600)
    snapshot = service.evaluate(
        umo, session_config, now=evaluation_time
    )
    reply_items = [
        item
        for item in service.get_state(umo).get("proactive", [])
        if isinstance(item, dict)
    ]
    replied = [item for item in reply_items if item.get("replied_at") is not None]
    metrics["reply_rate"] = (
        round(len(replied) / len(reply_items), 3) if reply_items else None
    )
    metrics["intensity_score"] = snapshot.get("score")
    metrics["intensity_probability_percent"] = snapshot.get(
        "probability_percent"
    )
    metrics["effective_interval_minutes"] = snapshot.get(
        "effective_interval_minutes"
    )
    metrics["attempt_hours"] = dict(sorted(metrics["attempt_hours"].items()))
    metrics["reply_quality"] = dict(metrics["reply_quality"])
    metrics["sent_times"] = metrics["sent_times"][:200]
    return metrics


def replay_database(
    db_path: str,
    *,
    platform_filter: str = "",
    session_filter: str = "",
    tz: ZoneInfo | None = None,
    settings: dict[str, Any] | None = None,
    limit: int = 50,
    min_messages: int = 10,
    seed: int = 7,
) -> dict[str, Any]:
    """读取 AstrBot 数据库并回放多个私聊会话。"""
    timezone = tz or ZoneInfo("Asia/Shanghai")
    config = settings or json.loads(json.dumps(DEFAULT_SETTINGS))
    connection = importer.connect_readonly(db_path)
    try:
        users = importer.load_user_messages(
            connection,
            platform_filter=platform_filter,
            tz=timezone,
        )
    finally:
        connection.close()

    selected = []
    for umo, messages in users.items():
        if session_filter and umo != session_filter:
            continue
        if len(messages) < min_messages:
            continue
        selected.append((umo, messages))
    selected.sort(key=lambda item: len(item[1]), reverse=True)
    selected = selected[: max(1, limit)]

    results = [
        replay_session(
            umo,
            messages,
            settings=config,
            tz=timezone,
            seed=seed,
        )
        for umo, messages in selected
    ]
    totals = {
        "sessions": len(results),
        "user_messages": sum(item.get("user_messages", 0) for item in results),
        "attempts": sum(item.get("attempts", 0) for item in results),
        "sent": sum(item.get("sent", 0) for item in results),
        "probability_miss": sum(
            item.get("probability_miss", 0) for item in results
        ),
        "pauses": sum(item.get("pauses", 0) for item in results),
        "negative_replies": sum(
            item.get("negative_replies", 0) for item in results
        ),
        "perfunctory_replies": sum(
            item.get("perfunctory_replies", 0) for item in results
        ),
    }
    totals["send_rate"] = (
        round(totals["sent"] / totals["attempts"], 3)
        if totals["attempts"]
        else 0
    )
    return {
        "generated_at": datetime.now(timezone).isoformat(),
        "db": db_path,
        "totals": totals,
        "sessions": results,
    }


def format_summary(report: dict[str, Any], limit: int = 15) -> str:
    totals = report.get("totals", {})
    lines = [
        "离线拟人化回放",
        "=" * 48,
        f"会话数：{totals.get('sessions', 0)}",
        f"用户消息：{totals.get('user_messages', 0)}",
        f"主动尝试：{totals.get('attempts', 0)}",
        f"实际发送：{totals.get('sent', 0)}（发送率 {totals.get('send_rate', 0):.1%}）",
        f"概率未命中：{totals.get('probability_miss', 0)}",
        f"暂停次数：{totals.get('pauses', 0)}",
        f"负反馈：{totals.get('negative_replies', 0)}",
        f"敷衍回复：{totals.get('perfunctory_replies', 0)}",
        "",
        f"{'会话':<42} {'消息':>5} {'发送':>5} {'未命中':>6} {'暂停':>4} {'回复率':>7}",
        "-" * 78,
    ]
    for item in report.get("sessions", [])[:limit]:
        reply_rate = item.get("reply_rate")
        reply_text = "-" if reply_rate is None else f"{reply_rate:.0%}"
        lines.append(
            f"{item.get('session_id', '')[:42]:<42} "
            f"{item.get('user_messages', 0):>5} "
            f"{item.get('sent', 0):>5} "
            f"{item.get('probability_miss', 0):>6} "
            f"{item.get('pauses', 0):>4} "
            f"{reply_text:>7}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.settings)
    report = replay_database(
        args.db,
        platform_filter=args.platform,
        session_filter=args.session,
        tz=ZoneInfo(args.timezone),
        settings=settings,
        limit=args.limit,
        min_messages=args.min_messages,
        seed=args.seed,
    )
    print(format_summary(report))
    if args.report:
        report_path = Path(args.report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n报告已写入：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
