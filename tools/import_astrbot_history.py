#!/usr/bin/env python3
"""从 AstrBot 数据库导入私聊历史，生成互动强度报告并可选写入插件数据。

用法
----
只生成报告（推荐先这样预览）::

    python tools/import_astrbot_history.py \
        --db /path/to/data_v4.db \
        --report ./intensity_report.json

把强度种子写入插件数据目录（会修改 session_data.json）::

    python tools/import_astrbot_history.py \
        --db /path/to/data_v4.db \
        --data-dir /path/to/AstrBot/data/plugin_data/astrbot_plugin_proactive_chat \
        --write

说明
----
- 数据库以只读模式打开，不会修改原文件；
- 只处理 ``FriendMessage`` 会话，群聊会话会被忽略；
- 消息时间来自 AstrBot 写入的 ``<system_reminder>Current datetime: ...``；
- 导入后的状态仍会在运行时被新消息继续更新。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import types
from collections import Counter, defaultdict
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
for path in (PLUGIN_ROOT, PLUGIN_ROOT.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _install_astrbot_stub_if_needed() -> None:
    """允许在未安装 AstrBot 的离线环境里运行本工具。"""
    try:
        import astrbot.api  # noqa: F401
        return
    except Exception:
        pass

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")
    api.__path__ = []  # type: ignore[attr-defined]

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        info = warning = error = exception = debug

    api.logger = _Logger()
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    astrbot.api = api  # type: ignore[attr-defined]


_install_astrbot_stub_if_needed()

from astrbot_plugin_proactive_chat.core.services.intensity_service import (  # noqa: E402
    IntensityService,
)

DATETIME_RE = re.compile(
    r"Current datetime:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
)
SYSTEM_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]{1,200}>")
MEDIA_RE = re.compile(
    r"\[(?:Image|历史图片|图片|语音|视频|文件|Quoted Message|历史工具结果)[^\]]*\]",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 AstrBot data_v4.db 导入私聊互动强度"
    )
    parser.add_argument("--db", required=True, help="AstrBot 数据库路径（data_v4.db）")
    parser.add_argument(
        "--platform",
        default="",
        help="只导入指定平台前缀（如 qq_official / qq_offficial），默认全部",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="数据库时间所在时区（默认 Asia/Shanghai）",
    )
    parser.add_argument(
        "--report",
        default="intensity_report.json",
        help="报告输出路径；设为空字符串则不写报告",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="插件数据目录；指定后可与 --write 一起写入 session_data.json",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="把强度种子写入 <data-dir>/session_data.json（会先备份）",
    )
    parser.add_argument(
        "--min-messages",
        type=int,
        default=1,
        help="只输出至少有 N 条用户消息的会话（默认 1）",
    )
    parser.add_argument(
        "--now",
        default="",
        help="用于评分的基准时间（ISO 格式），默认当前时间",
    )
    parser.add_argument(
        "--probability-min",
        type=int,
        default=15,
        help="评分时使用的概率下限（百分数，默认 15）",
    )
    parser.add_argument(
        "--probability-max",
        type=int,
        default=95,
        help="评分时使用的概率上限（百分数，默认 95）",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 数据库读取
# ---------------------------------------------------------------------------


def connect_readonly(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def message_text(content: Any) -> str:
    """把 AstrBot 的结构化 content 还原为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("content", "message", "text"):
            value = content.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                content = value
                break
        else:
            return ""
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").lower()
        if part_type in {"think", "tool", "tool_call", "tool_result"}:
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
        elif part_type in {"image", "image_url", "record", "audio", "video"}:
            parts.append("[媒体]")
    return "\n".join(parts)


def clean_user_text(text: str) -> str:
    cleaned = SYSTEM_REMINDER_RE.sub("", text)
    cleaned = TAG_RE.sub("", cleaned)
    cleaned = MEDIA_RE.sub("", cleaned)
    cleaned = cleaned.replace("\u200b", "")
    return WHITESPACE_RE.sub(" ", cleaned).strip()


def extract_timestamp(text: str, tz: ZoneInfo) -> float | None:
    match = DATETIME_RE.search(text)
    if not match:
        return None
    try:
        local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except ValueError:
        return None
    return local.timestamp()


def load_user_messages(
    connection: sqlite3.Connection,
    *,
    platform_filter: str,
    tz: ZoneInfo,
) -> dict[str, list[dict[str, Any]]]:
    """按 UMO 聚合私聊用户消息。

    只保留 ``FriendMessage`` / ``PrivateMessage`` 会话。
    """
    cursor = connection.execute(
        "SELECT user_id, content, updated_at FROM conversations "
        "WHERE user_id LIKE '%FriendMessage%' "
        "OR user_id LIKE '%PrivateMessage%'"
    )

    users: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, float, str]] = set()

    for row in cursor:
        umo = str(row["user_id"] or "")
        if not umo:
            continue
        platform = umo.split(":", 1)[0]
        if platform_filter and platform != platform_filter:
            continue

        try:
            messages = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(messages, list):
            continue

        for item in messages:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            raw = message_text(item.get("content"))
            if not raw.strip():
                continue
            timestamp = extract_timestamp(raw, tz)
            if timestamp is None:
                continue
            cleaned = clean_user_text(raw)
            if not cleaned:
                continue
            key = (umo, timestamp, cleaned[:80])
            if key in seen:
                continue
            seen.add(key)
            users[umo].append({"t": timestamp, "text": cleaned})

    for items in users.values():
        items.sort(key=lambda item: item["t"])
    return users


# ---------------------------------------------------------------------------
# 状态构建与评分
# ---------------------------------------------------------------------------


class _SessionService:
    def parse_session_id(self, umo: str):
        from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey

        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    def normalize_session_id(self, umo: str) -> str:
        return umo

    def get_session_name(self, umo: str, config=None) -> str:
        return ""

    def get_session_display_name(self, umo: str, config=None) -> str:
        return umo


class _ConfigService:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def get_session_config(self, umo: str):
        return {**self.settings, "enable": True, "_session_type": "friend"}

    def get_private_settings(self):
        return self.settings


class _EvaluationPlugin:
    def __init__(self, settings: dict[str, Any], tz: ZoneInfo):
        self.config = {"private_settings": settings}
        self.session_data: dict[str, Any] = {}
        self.timezone = tz
        self.session_service = _SessionService()
        self.config_service = _ConfigService(settings)
        self.intensity_service = IntensityService(self)


def build_intensity_state(
    umo: str,
    messages: Iterable[dict[str, Any]],
    *,
    platform: str,
) -> dict[str, Any]:
    """把消息时间线压缩成插件使用的强度状态。"""
    from astrbot_plugin_proactive_chat.core.services.intensity_service import (
        MAX_EVENTS,
    )

    ordered = sorted(messages, key=lambda item: item["t"])
    if not ordered:
        return {}

    events = []

    for item in ordered:
        text = str(item.get("text") or "")
        feature = IntensityService.analyze_text(text)
        events.append(
            {
                "t": round(float(item["t"]), 3),
                "n": int(feature["n"]),
                "q": int(feature["q"]),
                "e": int(feature["e"]),
                "m": int(feature["m"]),
            }
        )

    return {
        "version": 1,
        "events": events[-MAX_EVENTS:],
        "daily": {},
        # 日键由调用方按真实时区补充
        "_all_timestamps": [item["t"] for item in ordered],
        "proactive": [],
        "last_decision": {},
        "total_user_messages": len(ordered),
        "first_seen": ordered[0]["t"],
        "last_user_ts": ordered[-1]["t"],
        "seed": {
            "source": "astrbot_db",
            "imported_at": datetime.now(dt_timezone.utc).timestamp(),
            "platform": platform,
        },
    }


def finalize_daily(
    state: dict[str, Any], tz: ZoneInfo, *, keep_days: int = 45
) -> dict[str, Any]:
    timestamps = state.pop("_all_timestamps", [])
    daily: Counter[str] = Counter()
    for timestamp in timestamps:
        key = datetime.fromtimestamp(timestamp, tz=tz).strftime("%Y-%m-%d")
        daily[key] += 1
    # 只保留最近 keep_days 天。
    ordered_keys = sorted(daily)
    state["daily"] = {key: daily[key] for key in ordered_keys[-keep_days:]}
    return state


def evaluate_states(
    states: dict[str, dict[str, Any]],
    *,
    tz: ZoneInfo,
    now: float,
    settings_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    settings = {
        "enable": True,
        "session_list": list(states),
        "schedule_settings": {
            "min_interval_minutes": 30,
            "max_interval_minutes": 600,
        },
        "intensity_settings": settings_payload,
    }
    plugin = _EvaluationPlugin(settings, tz)
    plugin.session_data = {
        umo: {"intensity": state} for umo, state in states.items()
    }

    results: list[dict[str, Any]] = []
    for umo, state in states.items():
        snapshot = plugin.intensity_service.evaluate(umo, now=now)
        snapshot["session_name"] = ""
        snapshot["session_display_name"] = umo

        # 额外按“最后一条用户消息时刻”评分，展示该用户活跃期的峰值强度。
        last_ts = float(state.get("last_user_ts", 0) or 0)
        if last_ts > 0 and last_ts < now - 60:
            peak = plugin.intensity_service.evaluate(umo, now=last_ts)
            snapshot["at_last"] = {
                "at": datetime.fromtimestamp(last_ts, tz=tz).isoformat(),
                "score": peak["score"],
                "tier": peak["tier"],
                "tier_label": peak["tier_label"],
                "probability_percent": peak["probability_percent"],
                "effective_interval_minutes": peak["effective_interval_minutes"],
            }
        else:
            snapshot["at_last"] = {
                "at": snapshot["metrics"].get("last_user_at"),
                "score": snapshot["score"],
                "tier": snapshot["tier"],
                "tier_label": snapshot["tier_label"],
                "probability_percent": snapshot["probability_percent"],
                "effective_interval_minutes": snapshot["effective_interval_minutes"],
            }
        results.append(snapshot)
    results.sort(key=lambda item: (-item["at_last"]["score"], item["session_id"]))
    return results


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def build_report(
    results: list[dict[str, Any]],
    *,
    db_path: str,
    platform_filter: str,
    timezone_name: str,
    now: float,
    settings_payload: dict[str, Any],
) -> dict[str, Any]:
    tier_keys = ("dormant", "steady", "warm", "close", "blazing")
    tiers = {key: 0 for key in tier_keys}
    peak_tiers = {key: 0 for key in tier_keys}
    for item in results:
        tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
        peak_tier = item.get("at_last", {}).get("tier", item["tier"])
        peak_tiers[peak_tier] = peak_tiers.get(peak_tier, 0) + 1

    return {
        "generated_at": datetime.now(dt_timezone.utc).isoformat(),
        "benchmark_time": datetime.fromtimestamp(now, tz=dt_timezone.utc).isoformat(),
        "db": str(Path(db_path).expanduser().resolve()),
        "platform_filter": platform_filter or "all",
        "timezone": timezone_name,
        "settings": settings_payload,
        "summary": {
            "users": len(results),
            "total_user_messages": sum(
                item["metrics"]["total_user_messages"] for item in results
            ),
            "average_score": round(
                sum(item["score"] for item in results) / len(results), 1
            )
            if results
            else 0,
            "average_peak_score": round(
                sum(item["at_last"]["score"] for item in results) / len(results), 1
            )
            if results
            else 0,
            "tiers": tiers,
            "peak_tiers": peak_tiers,
        },
        "sessions": results,
    }


def write_seed(
    results: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    data_dir: Path,
) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    session_file = data_dir / "session_data.json"
    if session_file.exists():
        backup = session_file.with_suffix(".json.bak")
        backup.write_text(session_file.read_text(encoding="utf-8"), encoding="utf-8")

    try:
        existing = json.loads(session_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    for item in results:
        umo = item["session_id"]
        state = states.get(umo)
        if not state:
            continue
        payload = existing.get(umo)
        if not isinstance(payload, dict):
            payload = {}
        payload["intensity"] = state
        existing[umo] = payload

    temp_file = session_file.with_suffix(".json.tmp")
    temp_file.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_file.replace(session_file)
    return session_file


def format_table(results: list[dict[str, Any]], limit: int = 20) -> str:
    lines = [
        f"{'#':>3}  {'当前':>5}  {'峰值':>5}  {'峰值等级':<4}  {'概率':>4}  "
        f"{'间隔(分钟)':<12}  {'24h':>4} {'7d':>5} {'活跃天':>5} {'中位间隔':>8}  会话"
    ]
    for index, item in enumerate(results[:limit], start=1):
        metrics = item["metrics"]
        peak = item["at_last"]
        gap = metrics["median_gap_minutes"]
        interval = peak["effective_interval_minutes"]
        lines.append(
            f"{index:>3}  {item['score']:>5.1f}  {peak['score']:>5.1f}  "
            f"{peak['tier_label']:<4}  "
            f"{peak['probability_percent']:>3}%  "
            f"{interval[0]:>5.1f}-{interval[1]:<6.1f}  "
            f"{metrics['messages_24h']:>4} {metrics['messages_7d']:>5} "
            f"{metrics['active_days_14']:>5} "
            f"{(str(gap) if gap is not None else '--'):>8}  "
            f"{item['session_id']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tz = ZoneInfo(args.timezone)
    except Exception as e:
        print(f"时区无效: {args.timezone} ({e})", file=sys.stderr)
        return 2

    if args.write and not args.data_dir:
        print("--write 需要同时指定 --data-dir", file=sys.stderr)
        return 2

    connection = connect_readonly(args.db)
    try:
        users = load_user_messages(
            connection,
            platform_filter=args.platform,
            tz=tz,
        )
    finally:
        connection.close()

    if not users:
        print("没有在数据库中找到可导入的私聊消息。", file=sys.stderr)
        return 1

    now = (
        datetime.fromisoformat(args.now).timestamp()
        if args.now
        else datetime.now(tz=dt_timezone.utc).timestamp()
    )
    settings_payload = {
        "enable": True,
        "probability_min": args.probability_min,
        "probability_max": args.probability_max,
    }

    states: dict[str, dict[str, Any]] = {}
    for umo, messages in users.items():
        if len(messages) < max(1, args.min_messages):
            continue
        platform = umo.split(":", 1)[0]
        state = build_intensity_state(umo, messages, platform=platform)
        if not state:
            continue
        finalize_daily(state, tz)
        states[umo] = state

    results = evaluate_states(
        states,
        tz=tz,
        now=now,
        settings_payload=settings_payload,
    )

    report = build_report(
        results,
        db_path=args.db,
        platform_filter=args.platform,
        timezone_name=args.timezone,
        now=now,
        settings_payload=settings_payload,
    )

    if args.report:
        report_path = Path(args.report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告已写入: {report_path.resolve()}")

    summary = report["summary"]
    print(
        f"导入会话: {summary['users']} 个，用户消息: {summary['total_user_messages']} 条，"
        f"平均强度: {summary['average_score']}，等级分布: {summary['tiers']}"
    )
    print()
    print(format_table(results))

    if args.write:
        session_file = write_seed(results, states, Path(args.data_dir).expanduser())
        print(f"\n强度种子已写入: {session_file.resolve()}（原文件已备份为 .json.bak）")

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
