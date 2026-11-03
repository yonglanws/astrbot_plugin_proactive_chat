"""时间工具函数模块。"""

from __future__ import annotations

import zoneinfo
from datetime import datetime

# 中文星期名称，索引与 datetime.weekday() 对齐（0=周一）
_WEEKDAY_NAMES_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo | None) -> bool:
    """检查当前时间是否处于免打扰时段。"""
    try:
        # 解析开始与结束小时
        start_str, end_str = quiet_hours_str.split("-")
        start_hour, end_hour = int(start_str), int(end_str)
        # 若未提供时区则使用系统本地时间
        now = datetime.now(tz) if tz else datetime.now()
        # 处理跨天区间
        if start_hour <= end_hour:
            # 同日区间，例如 1-7
            return start_hour <= now.hour < end_hour
        # 跨日区间，例如 23-6
        return now.hour >= start_hour or now.hour < end_hour
    except (ValueError, TypeError):
        # 配置非法时按“非免打扰”处理，避免误阻断主动消息
        return False


def format_current_time(tz: zoneinfo.ZoneInfo | None = None) -> str:
    """返回带中文星期的时间字符串，例如 2026年05月12日 周二 09:53。

    星期信息直接由 datetime 计算得出，避免模型自行由日期反推星期几而算错。
    """
    now = datetime.now(tz) if tz else datetime.now()
    weekday = _WEEKDAY_NAMES_ZH[now.weekday()]
    return f"{now.strftime('%Y年%m月%d日')} {weekday} {now.strftime('%H:%M')}"


def format_relative_gap(
    seconds: float | int | None,
    *,
    empty: str = "暂无记录",
) -> str:
    """把时间差格式化为适合写进提示词的中文相对时间。"""
    if seconds is None:
        return empty
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return empty

    if value < 60:
        return "刚刚"
    if value < 3600:
        return f"{int(value // 60)} 分钟前"
    if value < 86400:
        return f"{int(value // 3600)} 小时前"
    days = int(value // 86400)
    if days < 30:
        return f"{days} 天前"
    months = days // 30
    if months < 12:
        return f"{months} 个月前"
    return f"{days // 365} 年前"


def describe_day_part(hour: int) -> str:
    """把小时映射为一天中的时段描述。"""
    hour = int(hour) % 24
    if hour < 5:
        return "深夜"
    if hour < 8:
        return "清晨"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"
