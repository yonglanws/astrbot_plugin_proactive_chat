"""真实 AstrBot 集成测试入口。

1. 子进程运行 ``tests/integration/astrbot_real_harness.py``：真实导入
   AstrBot 4.29.4 的 star_handler / Context.register_web_api / PluginRequest，
   并真实导入本插件 ``main.py``。
2. Node 运行 ``tests/integration/bridge_protocol_test.js``：加载真实
   ``bridge.js``，验证 iframe bridge 的完整路径映射。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "integration" / "astrbot_real_harness.py"
BRIDGE_TEST = ROOT / "tests" / "integration" / "bridge_protocol_test.js"


def test_main_module_imports_with_private_handler():
    from astrbot_plugin_proactive_chat import main

    assert hasattr(main.ProactiveChatPlugin, "on_friend_message")
    assert hasattr(main.ProactiveChatPlugin, "on_astrbot_loaded")
    assert hasattr(main.ProactiveChatPlugin, "cmd_enable_proactive")
    assert hasattr(main.ProactiveChatPlugin, "cmd_disable_proactive")
    assert hasattr(main.ProactiveChatPlugin, "cmd_proactive_status")


def test_real_astrbot_plugin_set_filter_and_page_routes():
    env = dict(os.environ)
    # 允许显式指定 AstrBot 源码；未指定时探针会自动搜索本地 4.29.4 源码。
    result = subprocess.run(
        [sys.executable, "-I", str(HARNESS)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 77:
        pytest.skip(
            "未找到 AstrBot 源码（可设置 ASTRBOT_SRC 指向 AstrBot 4.29.4 仓库）"
        )
    assert result.returncode == 0, result.stderr[-4000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["plugin_set_filtered_out"] == 0
    assert payload["plugin_set_filtered_in"] == 1
    assert payload["loaded_event_exempt"] == 1
    assert payload["page_routes"] >= 20
    assert payload["concurrent_runs"] == 1


def test_frontend_bridge_protocol():
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 Node.js，跳过 iframe bridge 协议测试")
    result = subprocess.run(
        [node, str(BRIDGE_TEST)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["bridge_calls"] == 22
