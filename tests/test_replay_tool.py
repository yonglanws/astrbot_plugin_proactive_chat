"""离线回放工具测试（临时 SQLite，不触碰真实数据）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tools import replay_humanization as replay


def _force_send_settings():
    settings = replay.load_settings("")
    settings["intensity_settings"]["probability_min"] = 100
    settings["intensity_settings"]["probability_max"] = 100
    return settings


def _make_db(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE conversations (user_id TEXT, content TEXT, updated_at REAL)"
    )
    base = datetime(2026, 1, 5, 10, 0)
    messages = []
    for index, text in enumerate(
        [
            "今天面试完了，感觉还行",
            "你在干嘛",
            "嗯",
            "明天还有一场面试",
            "别发了，在忙",
            "好吧，那晚点聊",
        ]
    ):
        stamp = base + timedelta(hours=index * 3)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"<system_reminder>Current datetime: "
                    f"{stamp.strftime('%Y-%m-%d %H:%M')}</system_reminder>{text}"
                ),
            }
        )
    connection.execute(
        "INSERT INTO conversations VALUES (?, ?, ?)",
        (
            "qq_official:FriendMessage:REPLAY1",
            json.dumps(messages, ensure_ascii=False),
            0,
        ),
    )
    connection.execute(
        "INSERT INTO conversations VALUES (?, ?, ?)",
        (
            "qq_official:GroupMessage:GROUP1",
            json.dumps(messages, ensure_ascii=False),
            0,
        ),
    )
    connection.commit()
    connection.close()


def test_replay_session_counts_quality():
    messages = [
        {"t": 1_700_000_000.0, "text": "今天面试完了，感觉还行"},
        {"t": 1_700_000_300.0, "text": "嗯"},
        {"t": 1_700_005_000.0, "text": "明天还有一场面试"},
        {"t": 1_700_010_000.0, "text": "别发了，在忙"},
    ]
    result = replay.replay_session(
        "qq_official:FriendMessage:R1",
        messages,
        settings=_force_send_settings(),
        tz=ZoneInfo("Asia/Shanghai"),
        seed=3,
    )
    assert result["user_messages"] == 4
    assert result["perfunctory_replies"] == 1
    assert result["negative_replies"] == 1
    assert result["attempts"] >= 1
    assert result["reply_rate"] is not None


def test_replay_database_reads_private_only(tmp_path):
    db = tmp_path / "data_v4.db"
    _make_db(db)
    report = replay.replay_database(
        str(db),
        tz=ZoneInfo("Asia/Shanghai"),
        settings=replay.load_settings(""),
        limit=10,
        min_messages=1,
        seed=1,
    )
    assert report["totals"]["sessions"] == 1
    assert report["sessions"][0]["session_id"].endswith("REPLAY1")
    assert report["totals"]["negative_replies"] == 1
    assert "发送率" in replay.format_summary(report)
