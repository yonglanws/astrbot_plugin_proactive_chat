"""数据库导入工具测试（使用合成数据库，不依赖真实用户数据）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.import_astrbot_history import (
    build_intensity_state,
    evaluate_states,
    finalize_daily,
    load_user_messages,
    write_seed,
)

PRIVATE_UMO = "qq_official:FriendMessage:PRIVATE1"
GROUP_UMO = "qq_official:GroupMessage:GROUP1"
TZ = ZoneInfo("Asia/Shanghai")


def make_content(messages: list[tuple[str, str]]) -> str:
    """messages: [(role, text)]，用户消息里带 system_reminder。"""
    payload = []
    for role, text in messages:
        payload.append(
            {
                "role": role,
                "content": [{"type": "text", "text": text}],
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE conversations (
            inner_conversation_id INTEGER PRIMARY KEY,
            conversation_id VARCHAR(36),
            platform_id VARCHAR,
            user_id VARCHAR,
            content JSON,
            created_at DATETIME,
            updated_at DATETIME,
            title VARCHAR(255),
            persona_id VARCHAR,
            token_usage INTEGER
        )
        """
    )
    private_messages = []
    day = 1
    for hour in range(20, 22):
        for minute in range(0, 60, 5):
            text = (
                f"今天发生了一件超有意思的事！"
                f"<system_reminder>Current datetime: 2026-10-{day:02d} "
                f"{hour:02d}:{minute:02d} (CST), Weekday: Wednesday</system_reminder>"
            )
            private_messages.append(("user", text))
            private_messages.append(("assistant", "哈哈真的吗！"))
    # 第二天继续聊
    for minute in range(0, 60, 10):
        text = (
            f"你在干嘛呀？"
            f"<system_reminder>Current datetime: 2026-10-{day + 1:02d} "
            f"20:{minute:02d} (CST), Weekday: Thursday</system_reminder>"
        )
        private_messages.append(("user", text))
        private_messages.append(("assistant", "我在等你说话呀~"))

    group_messages = [
        (
            "user",
            "群友消息"
            "<system_reminder>Current datetime: 2026-10-01 20:00 (CST), Weekday: Wednesday</system_reminder>",
        )
    ]

    connection.execute(
        "INSERT INTO conversations (conversation_id, platform_id, user_id, content, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("c1", "qq_official", PRIVATE_UMO, make_content(private_messages), "2026-10-01", "2026-10-02"),
    )
    connection.execute(
        "INSERT INTO conversations (conversation_id, platform_id, user_id, content, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("c2", "qq_official", GROUP_UMO, make_content(group_messages), "2026-10-01", "2026-10-01"),
    )
    connection.commit()
    connection.close()


def test_load_only_private_messages(tmp_path):
    db_path = tmp_path / "test.db"
    make_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    users = load_user_messages(connection, platform_filter="qq_official", tz=TZ)
    connection.close()

    assert PRIVATE_UMO in users
    assert GROUP_UMO not in users
    # 24 条 5 分钟间隔 + 6 条 10 分钟间隔 = 30 条
    assert len(users[PRIVATE_UMO]) == 30
    assert all("system_reminder" not in item["text"] for item in users[PRIVATE_UMO])


def test_build_and_evaluate(tmp_path):
    db_path = tmp_path / "test.db"
    make_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    users = load_user_messages(connection, platform_filter="qq_official", tz=TZ)
    connection.close()

    state = build_intensity_state(
        PRIVATE_UMO, users[PRIVATE_UMO], platform="qq_official"
    )
    finalize_daily(state, TZ)
    assert state["total_user_messages"] == 30
    assert len(state["events"]) == 30
    assert state["daily"]
    assert state["seed"]["source"] == "astrbot_db"

    now = datetime(2026, 10, 3, 0, 0, tzinfo=TZ).timestamp()
    results = evaluate_states(
        {PRIVATE_UMO: state},
        tz=TZ,
        now=now,
        settings_payload={"enable": True},
    )
    assert len(results) == 1
    snapshot = results[0]
    assert snapshot["score"] > 0
    assert snapshot["at_last"]["score"] > snapshot["score"]
    assert snapshot["metrics"]["total_user_messages"] == 30


def test_write_seed_backs_up(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    session_file = data_dir / "session_data.json"
    session_file.write_text(
        json.dumps({PRIVATE_UMO: {"unanswered_count": 2}}), encoding="utf-8"
    )

    state = {
        "version": 1,
        "events": [{"t": 1.0, "n": 3, "q": 0, "e": 0, "m": 0}],
        "daily": {"2026-10-01": 1},
        "proactive": [],
        "last_decision": {},
        "total_user_messages": 1,
        "first_seen": 1.0,
        "last_user_ts": 1.0,
        "seed": {"source": "astrbot_db", "imported_at": 1.0, "platform": "qq_official"},
    }
    results = [{"session_id": PRIVATE_UMO}]
    write_seed(results, {PRIVATE_UMO: state}, data_dir)

    assert (data_dir / "session_data.json.bak").exists()
    merged = json.loads(session_file.read_text(encoding="utf-8"))
    assert merged[PRIVATE_UMO]["unanswered_count"] == 2
    assert merged[PRIVATE_UMO]["intensity"]["total_user_messages"] == 1
