#!/usr/bin/env python3
"""离线演示互动强度如何影响触发概率与间隔。

不需要 AstrBot 运行时，只使用核心模型：

    python tools/simulate_intensity.py
    python tools/simulate_intensity.py --pattern hot --days 3
"""

from __future__ import annotations

import argparse
import random
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
for path in (PLUGIN_ROOT, PLUGIN_ROOT.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _install_stub() -> None:
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


_install_stub()

from astrbot_plugin_proactive_chat.core.services.intensity_service import (  # noqa: E402
    IntensityService,
)

UMO = "qq_official:FriendMessage:SIMULATED"


class _SessionService:
    def parse_session_id(self, umo):
        from astrbot_plugin_proactive_chat.core.domain.session_key import SessionKey

        key = SessionKey.parse(umo)
        return (key.platform, key.message_type, key.target_id) if key else None

    normalize_session_id = staticmethod(lambda umo: umo)
    get_session_name = staticmethod(lambda umo, config=None: "")
    get_session_display_name = staticmethod(lambda umo, config=None: umo)
    get_session_log_str = staticmethod(lambda umo, config=None: umo)


class _ConfigService:
    def __init__(self, settings):
        self.settings = settings

    def get_session_config(self, umo):
        return {**self.settings, "enable": True, "_session_type": "friend"}

    def get_private_settings(self):
        return self.settings


class _Plugin:
    def __init__(self, settings):
        self.config = {"private_settings": settings}
        self.session_data = {}
        self.timezone = ZoneInfo("Asia/Shanghai")
        self.session_service = _SessionService()
        self.config_service = _ConfigService(settings)
        self.intensity_service = IntensityService(self)


PATTERNS = {
    "hot": {"messages_per_day": 60, "gap_minutes": (1, 3), "days": 4},
    "steady": {"messages_per_day": 10, "gap_minutes": (15, 40), "days": 5},
    "cold": {"messages_per_day": 1, "gap_minutes": (300, 500), "days": 2},
}


def run(pattern: str, days: int | None, seed: int) -> int:
    rng = random.Random(seed)
    preset = dict(PATTERNS[pattern])
    if days:
        preset["days"] = days

    settings = {
        "enable": True,
        "session_list": [UMO],
        "schedule_settings": {
            "min_interval_minutes": 30,
            "max_interval_minutes": 600,
        },
        "intensity_settings": {
            "enable": True,
            "probability_min": 15,
            "probability_max": 95,
        },
    }
    plugin = _Plugin(settings)
    service = plugin.intensity_service

    start = datetime(2026, 10, 1, 20, 0, tzinfo=plugin.timezone).timestamp()
    timestamp = start
    for day in range(preset["days"]):
        count = preset["messages_per_day"]
        for index in range(count):
            text = rng.choice(
                [
                    "今天发生了一件超有意思的事！",
                    "你在干嘛呀？",
                    "哈哈哈哈这也太好玩了吧",
                    "我突然想到你上次说的那句话",
                    "有点想你了",
                    "等等等等，先别走！",
                ]
            )
            service.record_user_message(UMO, text, now=timestamp)
            low, high = preset["gap_minutes"]
            timestamp += rng.randint(low, high) * 60
        # 每天之间隔 8 小时，模拟睡眠。
        timestamp += 8 * 3600

    now = timestamp
    snapshot = service.evaluate(UMO, now=now)
    will_send, decision = service.decide(UMO, now=now, rng=rng)
    interval = service.next_interval(UMO, now=now, rng=rng)

    print(f"场景: {pattern}  模拟 {preset['days']} 天 / {preset['messages_per_day']} 条/天")
    print(f"评分: {snapshot['score']}/100  等级: {snapshot['tier_label']}")
    print(
        "消息量: 24h={} 7d={} 活跃天={} 中位间隔={} 分钟".format(
            snapshot["metrics"]["messages_24h"],
            snapshot["metrics"]["messages_7d"],
            snapshot["metrics"]["active_days_14"],
            snapshot["metrics"]["median_gap_minutes"],
        )
    )
    print(
        "主动概率: {}%  本轮判定: {}  有效间隔: {}-{} 分钟（本次随机 {} 分钟）".format(
            snapshot["probability_percent"],
            "命中" if will_send else "未命中",
            round(interval["min_seconds"] / 60, 1),
            round(interval["max_seconds"] / 60, 1),
            round(interval["random_seconds"] / 60, 1),
        )
    )
    if not will_send:
        print(f"未命中后的重试等待: {decision['retry_after_seconds'] // 60} 分钟")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="互动强度调度模拟")
    parser.add_argument(
        "--pattern", choices=sorted(PATTERNS), default="hot", help="模拟场景"
    )
    parser.add_argument("--days", type=int, default=0, help="覆盖模拟天数")
    parser.add_argument("--seed", type=int, default=7, help="随机种子")
    parser.add_argument(
        "--all", action="store_true", help="依次运行 hot / steady / cold"
    )
    args = parser.parse_args(argv)

    patterns = sorted(PATTERNS) if args.all else [args.pattern]
    for pattern in patterns:
        run(pattern, args.days or None, args.seed)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
