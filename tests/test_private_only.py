"""静态审计：确保群聊主动消息路径已被彻底移除。"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOKENS = (
    "group_settings",
    "group_idle",
    "GROUP_IDLE",
    "group_timers",
    "on_group_message",
    "reset_group_silence_timer",
    "handle_group_silence_callback",
)


def iter_python_files():
    for path in PROJECT_ROOT.rglob("*.py"):
        if ".git" in path.parts or "tests" in path.parts or "tools" in path.parts:
            continue
        yield path


def test_no_group_proactive_tokens_in_code():
    offenders = []
    for path in iter_python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append((str(path.relative_to(PROJECT_ROOT)), token))
    assert offenders == [], offenders


def test_schema_is_private_only():
    schema = json.loads(
        (PROJECT_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert set(schema) == {
        "private_settings",
        "web_admin",
        "notification_settings",
        "telemetry_config",
    }
    assert "intensity_settings" in schema["private_settings"]["items"]
    assert "pause_settings" in schema["private_settings"]["items"]


def test_metadata_is_private_only():
    text = (PROJECT_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert "群聊" not in text


def test_admin_ui_has_no_group_entries():
    # 前端现在作为 AstrBot 插件页面放在 views/proactive-chat，独立 Web 管理端复用同一目录。
    admin_root = PROJECT_ROOT / "views" / "proactive-chat"
    offenders = []
    for path in admin_root.rglob("*"):
        if path.suffix not in {".js", ".jsx", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in ("group_settings", "group_silence", "group_timers", "群聊"):
            if token in text:
                offenders.append((str(path.relative_to(PROJECT_ROOT)), token))
    assert offenders == [], offenders
